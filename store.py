"""SQLite storage: who is registered, and each player's monthly results.

Every function is synchronous and short; call them through asyncio.to_thread from
the bot. A player is identified by (site, username), username case-insensitively.
"""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass

import settings

# Where the database lives (settings.py: beside the code unless PLAYMOREBLITZ_DB in .env says otherwise).
DB_PATH = settings.DB_PATH
DB_LOCK_TIMEOUT = settings.DB_LOCK_TIMEOUT  # seconds to retry if another thread is mid-write, before giving up

ADDED = "added"
REACTIVATED = "reactivated"
EXISTS = "exists"
LIMIT = "limit"  # the owner already has a different active account on that site
CHANGED = "changed"  # set_owner: the account now belongs to someone else
UNCHANGED = "unchanged"  # set_owner: it already belonged to them
MISSING = "missing"  # set_owner: there is no such active account

JOINED = "joined"  # opted in to 100GOB just now
ALREADY = "already"  # was already in it this month
NO_ROW = "no_row"  # no open row for that month, so nothing to opt in

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    site      TEXT NOT NULL,
    username  TEXT NOT NULL COLLATE NOCASE,
    added_by  INTEGER NOT NULL,           -- Discord user ID of the owner
    added_at  TEXT NOT NULL DEFAULT (datetime('now')),
    active    INTEGER NOT NULL DEFAULT 1, -- 0 after !remove; history is kept
    PRIMARY KEY (site, username)
);

CREATE TABLE IF NOT EXISTS monthly_results (
    site         TEXT NOT NULL,
    username     TEXT NOT NULL COLLATE NOCASE,
    month        TEXT NOT NULL,           -- "YYYY-MM", a UTC calendar month
    start_rating INTEGER NOT NULL,
    end_rating   INTEGER NOT NULL,        -- the latest rating while the month is open
    games        INTEGER NOT NULL DEFAULT 0,  -- running totals while open, final at close
    wins         INTEGER NOT NULL DEFAULT 0,
    draws        INTEGER NOT NULL DEFAULT 0,
    losses       INTEGER NOT NULL DEFAULT 0,
    in_100gob    INTEGER NOT NULL DEFAULT 0,
    last_game_at TEXT,                    -- end of the last game counted: the watermark
    closed_at    TEXT,                    -- set when the month has been closed
    refreshed_at  TEXT,                   -- the last successful refresh
    refresh_error TEXT,                   -- why the latest refresh failed; NULL once one succeeds
    PRIMARY KEY (site, username, month),
    FOREIGN KEY (site, username) REFERENCES players (site, username)
);

-- Games to be analysed by the worker, and what came of it: one row per game, both sides. Times are UTC epoch
-- seconds. The queue functions are in analysis_queue.py; the moves are never stored.
CREATE TABLE IF NOT EXISTS game_analysis (
    site      TEXT NOT NULL,
    game_id   TEXT NOT NULL,
    month     TEXT NOT NULL,               -- "YYYY-MM" of ended_at, UTC
    ended_at  INTEGER NOT NULL,
    time_control TEXT,                     -- "180+0"
    result    TEXT NOT NULL CHECK (result IN ('white', 'black', 'draw')),
    ending    TEXT,                        -- resigned, checkmated, timeout ...
    opening_site TEXT,                     -- the site's own opening name and ECO code
    eco_site  TEXT,
    white_username TEXT NOT NULL COLLATE NOCASE,
    black_username TEXT NOT NULL COLLATE NOCASE,
    white_rating INTEGER, black_rating INTEGER,
    white_rating_change INTEGER, black_rating_change INTEGER,   -- NULL where the site does not say

    status    TEXT NOT NULL CHECK (status IN ('pending', 'claimed', 'done', 'skipped', 'failed')),
    skip_reason TEXT,                      -- not_standard_start, over_monthly_limit, unavailable, too_short
    priority  INTEGER NOT NULL DEFAULT 0,  -- -1 urgent (a member asked for its review), 0 normal, 1 low: a member's games past the full-priority number
    attempts  INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    queued_at INTEGER NOT NULL,
    claimed_at INTEGER,
    claimed_by TEXT,
    analysed_at INTEGER,

    engine TEXT, nodes INTEGER, method_version INTEGER, plies INTEGER,

    white_accuracy REAL, black_accuracy REAL,
    white_acc_opening REAL, black_acc_opening REAL,
    white_acc_middle REAL, black_acc_middle REAL,
    white_acc_end REAL, black_acc_end REAL,
    white_inaccuracies INTEGER, black_inaccuracies INTEGER,
    white_mistakes INTEGER, black_mistakes INTEGER,
    white_blunders INTEGER, black_blunders INTEGER,
    white_acpl INTEGER, black_acpl INTEGER,

    middle_ply INTEGER, end_ply INTEGER,   -- where the middlegame and endgame start; NULL if never
    eval_ply20 INTEGER,                    -- centipawns, White's point of view, after 10 moves each
    evals BLOB,                            -- the evaluation after every ply, packed (analysis.pack_evals)
    moments TEXT,                          -- the flagged moves as JSON [[ply, "i"|"m"|"b", points lost], ...] (analysis.moments_to_json)
    shape TEXT,                            -- our own game-shape label; filled in later

    site_white_accuracy REAL, site_black_accuracy REAL,   -- the site's own figures, kept apart from ours

    PRIMARY KEY (site, game_id)
);
CREATE INDEX IF NOT EXISTS game_analysis_queue ON game_analysis (status, priority, ended_at DESC);
CREATE INDEX IF NOT EXISTS game_analysis_month ON game_analysis (site, month);
CREATE INDEX IF NOT EXISTS game_analysis_white ON game_analysis (site, white_username, month);
CREATE INDEX IF NOT EXISTS game_analysis_black ON game_analysis (site, black_username, month);

-- Reviews (!obit) asked for and not yet sent by direct message. A row goes when the review is sent, or given up on.
CREATE TABLE IF NOT EXISTS obit_requests (
    user_id      INTEGER NOT NULL,         -- Discord user ID of whoever asked
    site         TEXT NOT NULL,
    game_id      TEXT NOT NULL,
    username     TEXT NOT NULL,            -- their account that played the game
    channel_id   INTEGER,                  -- where they asked, in case the direct message can't be delivered
    requested_at INTEGER NOT NULL,         -- UTC epoch seconds
    PRIMARY KEY (user_id, site, game_id)
);

-- When each analysis worker last asked for work, so the bot can tell whether one is alive.
CREATE TABLE IF NOT EXISTS analysis_workers (
    name      TEXT PRIMARY KEY,
    last_seen INTEGER NOT NULL
);

-- Things the bot has posted on a schedule, so a restart never posts one twice.
CREATE TABLE IF NOT EXISTS announcements (
    kind      TEXT NOT NULL,
    month     TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    PRIMARY KEY (kind, month)
);

-- Sign-ups for a month whose row doesn't exist yet (next month, or a new month before
-- its row is created). Applied, and removed, when the row is created.
CREATE TABLE IF NOT EXISTS gob_signups (
    site     TEXT NOT NULL,
    username TEXT NOT NULL COLLATE NOCASE,
    month    TEXT NOT NULL,
    PRIMARY KEY (site, username, month),
    FOREIGN KEY (site, username) REFERENCES players (site, username)
);
"""


def check_location():
    """Exit with a clear message if the database's folder doesn't exist.

    The usual cause is a database kept on a disk that hasn't been mounted yet. Failing here
    means the service restarts and tries again, instead of the bot carrying on with no data.
    (SQLite never creates a missing folder, so it can't quietly start a new empty database on
    the wrong disk as long as the database sits in a folder of its own on that disk.)
    """
    if not DB_PATH.parent.is_dir():
        raise SystemExit(f"The database folder {DB_PATH.parent} does not exist. Is its disk mounted? (PLAYMOREBLITZ_DB={DB_PATH})")


@dataclass(frozen=True)
class Player:
    site: str
    username: str
    added_by: int
    active: bool


@dataclass(frozen=True)
class ResultRow:
    """One active player's line for a month, as !results shows it."""

    site: str
    username: str
    has_row: bool  # False if the player has no row for this month yet
    start_rating: int | None
    end_rating: int | None
    games: int
    wins: int
    draws: int
    losses: int
    in_100gob: bool
    refreshed_at: str | None  # ISO time of the last successful refresh; None if never
    refresh_error: str | None  # why the latest refresh failed, if it did


# Columns added to monthly_results after the first version. A database made by an
# earlier version gets them on its next connection.
_ADDED_COLUMNS = ("refreshed_at", "refresh_error")


def _migrate(conn):
    have = {row["name"] for row in conn.execute("PRAGMA table_info(monthly_results)")}
    for column in _ADDED_COLUMNS:
        if column not in have:
            conn.execute(f"ALTER TABLE monthly_results ADD COLUMN {column} TEXT")
    have = {row["name"] for row in conn.execute("PRAGMA table_info(game_analysis)")}
    if "moments" not in have:  # a table made before flagged moves were kept
        conn.execute("ALTER TABLE game_analysis ADD COLUMN moments TEXT")


@contextmanager
def _transaction():
    """One connection, committed on success and rolled back on error, always closed."""
    conn = sqlite3.connect(DB_PATH, timeout=DB_LOCK_TIMEOUT)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        with conn:
            _migrate(conn)
            yield conn
    finally:
        conn.close()


transaction = _transaction  # for the modules that keep their own tables here (analysis_queue.py)


def _player(row):
    return Player(row["site"], row["username"], row["added_by"], bool(row["active"]))


def get_player(site, username):
    """The player, active or not, or None."""
    with _transaction() as conn:
        row = conn.execute("SELECT * FROM players WHERE site = ? AND username = ?", (site, username)).fetchone()
    return _player(row) if row else None


def find_active(username, site=None):
    """Active players with this username, on every site or just `site`."""
    query = "SELECT * FROM players WHERE username = ? AND active = 1"
    args = [username]
    if site is not None:
        query += " AND site = ?"
        args.append(site)
    with _transaction() as conn:
        rows = conn.execute(query + " ORDER BY site", args).fetchall()
    return [_player(r) for r in rows]


def active_players():
    with _transaction() as conn:
        rows = conn.execute("SELECT * FROM players WHERE active = 1 ORDER BY username, site").fetchall()
    return [_player(r) for r in rows]


def accounts_of(owner):
    """The active players registered by (owned by) this Discord user."""
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM players WHERE added_by = ? AND active = 1 ORDER BY username, site", (owner,)
        ).fetchall()
    return [_player(r) for r in rows]


def join_100gob(site, username, month):
    """Put an active player into the 100GOB challenge for `month`. Returns JOINED, ALREADY or NO_ROW.

    If the month's row exists the flag is set on it. If it doesn't yet (next month, or
    a new month before its row is created) the sign-up is remembered and applied when
    the row is created. A new month's row otherwise starts with the flag off, so
    players opt in afresh each month. NO_ROW means the player isn't registered, or the
    month is already closed.
    """
    key = (site, username)
    with _transaction() as conn:
        if not conn.execute("SELECT 1 FROM players WHERE site = ? AND username = ? AND active = 1", key).fetchone():
            return NO_ROW
        row = conn.execute(
            "SELECT in_100gob, closed_at FROM monthly_results WHERE site = ? AND username = ? AND month = ?", (*key, month)
        ).fetchone()
        if row is not None:
            if row["closed_at"]:
                return NO_ROW
            if row["in_100gob"]:
                return ALREADY
            conn.execute("UPDATE monthly_results SET in_100gob = 1 WHERE site = ? AND username = ? AND month = ?", (*key, month))
            return JOINED
        cur = conn.execute("INSERT OR IGNORE INTO gob_signups (site, username, month) VALUES (?, ?, ?)", (*key, month))
        return JOINED if cur.rowcount else ALREADY


def signups(month):
    """Usernames of active players signed up for `month` before its row exists, in name order."""
    with _transaction() as conn:
        rows = conn.execute(
            """
            SELECT p.username FROM gob_signups s
            JOIN players p ON p.site = s.site AND p.username = s.username
            WHERE s.month = ? AND p.active = 1 ORDER BY p.username COLLATE NOCASE, p.site
            """,
            (month,),
        ).fetchall()
    return [r["username"] for r in rows]


def unclosed_months(before):
    """Months earlier than `before` ("YYYY-MM") in which an active player still has an open
    row, oldest first: the months that have ended but not been closed."""
    with _transaction() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT m.month FROM monthly_results m
            JOIN players p ON p.site = m.site AND p.username = m.username
            WHERE m.closed_at IS NULL AND p.active = 1 AND m.month < ? ORDER BY m.month
            """,
            (before,),
        ).fetchall()
    return [r["month"] for r in rows]


def open_players(month):
    """Active players who have an open (unclosed) row for `month`, in name order."""
    with _transaction() as conn:
        rows = conn.execute(
            """
            SELECT p.* FROM players p
            JOIN monthly_results m ON m.site = p.site AND m.username = p.username
            WHERE m.month = ? AND m.closed_at IS NULL AND p.active = 1 ORDER BY p.username COLLATE NOCASE, p.site
            """,
            (month,),
        ).fetchall()
    return [_player(r) for r in rows]


def closed_months_since(cutoff):
    """Months closed at or after `cutoff` (an ISO time), oldest first."""
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT DISTINCT month FROM monthly_results WHERE closed_at IS NOT NULL AND closed_at >= ? ORDER BY month", (cutoff,)
        ).fetchall()
    return [r["month"] for r in rows]


def close_month(month, next_month, finals, now):
    """Close `month` for every player in `finals`, and open `next_month` for them, in one step.

    Each entry of `finals` is a dict with site, username, games, wins, draws, losses,
    end_rating and last_game_at, the authoritative figures for the whole month. Their
    rows are overwritten with them and marked closed; each player's `next_month` row is
    created with the closing rating as its start rating (a sign-up for that month is
    applied). Removed players' open rows for the month are marked closed as they stand.
    An already closed row is left alone. Returns how many rows were closed. Everything
    happens together or not at all.
    """
    closed = 0
    with _transaction() as conn:
        for f in finals:
            cur = conn.execute(
                """
                UPDATE monthly_results
                SET games = ?, wins = ?, draws = ?, losses = ?, end_rating = ?, last_game_at = ?,
                    closed_at = ?, refreshed_at = ?, refresh_error = NULL
                WHERE site = ? AND username = ? AND month = ? AND closed_at IS NULL
                """,
                (f["games"], f["wins"], f["draws"], f["losses"], f["end_rating"], f["last_game_at"], now, now,
                 f["site"], f["username"], month),
            )
            if cur.rowcount:
                closed += 1
                _open_month(conn, f["site"], f["username"], next_month, f["end_rating"])
        conn.execute(
            """
            UPDATE monthly_results SET closed_at = ?
            WHERE month = ? AND closed_at IS NULL
              AND EXISTS (SELECT 1 FROM players p WHERE p.site = monthly_results.site
                          AND p.username = monthly_results.username AND p.active = 0)
            """,
            (now, month),
        )
    return closed


def claim_announcement(kind, month, now):
    """Reserve the right to post `kind` for `month`. True if this call got it, False if it was already posted."""
    with _transaction() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO announcements (kind, month, posted_at) VALUES (?, ?, ?)", (kind, month, now))
        return cur.rowcount == 1


def release_announcement(kind, month):
    """Give the claim back (the post failed), so the next attempt can try again."""
    with _transaction() as conn:
        conn.execute("DELETE FROM announcements WHERE kind = ? AND month = ?", (kind, month))


def _open_month(conn, site, username, month, start_rating):
    """Create a player's row for a month, unless it exists. A sign-up for that month is
    applied (the flag starts on) and used up. True if a row was created."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO monthly_results (site, username, month, start_rating, end_rating, in_100gob)
        VALUES (?, ?, ?, ?, ?, EXISTS (SELECT 1 FROM gob_signups WHERE site = ? AND username = ? AND month = ?))
        """,
        (site, username, month, start_rating, start_rating, site, username, month),
    )
    if cur.rowcount:
        conn.execute("DELETE FROM gob_signups WHERE site = ? AND username = ? AND month = ?", (site, username, month))
    return cur.rowcount == 1


def add_player(site, username, owner, month, start_rating, *, one_per_site=False):
    """Register a player, or bring a removed one back, and make sure `month` has a row.

    Returns ADDED, REACTIVATED, EXISTS (already active: nothing changes) or LIMIT.
    With `one_per_site`, an owner who already has a different active account on that
    site gets LIMIT and nothing is written. The check and the write happen under one
    write lock, so two simultaneous adds by the same owner can't both get through. A
    reactivated player belongs to whoever is adding them now. A month row that already
    exists is left alone, so removing and re-adding within a month keeps that month's
    totals.
    """
    with _transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")  # take the write lock before reading, so the checks below can't go stale
        existing = conn.execute(
            "SELECT active FROM players WHERE site = ? AND username = ?", (site, username)
        ).fetchone()
        if existing is not None and existing["active"]:
            return EXISTS
        # (An active account with this name has already returned EXISTS above, so anything
        # counted here is a different account of the owner's.)
        if one_per_site and conn.execute(
            "SELECT 1 FROM players WHERE added_by = ? AND site = ? AND active = 1", (owner, site)
        ).fetchone():
            return LIMIT

        if existing is None:
            try:
                conn.execute("INSERT INTO players (site, username, added_by) VALUES (?, ?, ?)", (site, username, owner))
            except sqlite3.IntegrityError:  # someone else added the same player at the same moment
                return EXISTS
            outcome = ADDED
        else:
            # username is set too, so a re-add refreshes the spelling; it matches the
            # row case-insensitively either way, so the month rows stay attached.
            conn.execute(
                "UPDATE players SET active = 1, added_by = ?, added_at = datetime('now'), username = ? WHERE site = ? AND username = ?",
                (owner, username, site, username),
            )
            outcome = REACTIVATED

        _open_month(conn, site, username, month, start_rating)
    return outcome


def set_owner(site, username, owner, *, one_per_site=False):
    """Make `owner` (a Discord user ID) the owner of an active player: CHANGED, UNCHANGED (already theirs), MISSING (no
    such active player) or LIMIT (with `one_per_site`, they already own a different active account on that site). The
    check and the write happen under one write lock, like add_player's."""
    with _transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT added_by FROM players WHERE site = ? AND username = ? AND active = 1", (site, username)).fetchone()
        if row is None:
            return MISSING
        if row["added_by"] == owner:
            return UNCHANGED
        if one_per_site and conn.execute("SELECT 1 FROM players WHERE added_by = ? AND site = ? AND active = 1 AND username != ?",
                                         (owner, site, username)).fetchone():
            return LIMIT
        conn.execute("UPDATE players SET added_by = ? WHERE site = ? AND username = ?", (owner, site, username))
    return CHANGED


def remove_player(site, username):
    """Mark a player inactive. History is kept. True if they were active."""
    with _transaction() as conn:
        cur = conn.execute("UPDATE players SET active = 0 WHERE site = ? AND username = ? AND active = 1", (site, username))
        return cur.rowcount > 0


def results(month, require_row=False):
    """One ResultRow per active player for `month`, in no particular order.

    A player with no row for that month still appears, with has_row False, unless
    `require_row` is set (a closed month's final table lists only players who were
    in it, not someone who registered afterwards).
    """
    with _transaction() as conn:
        rows = conn.execute(
            """
            SELECT p.site, p.username, m.site IS NOT NULL AS has_row, m.start_rating, m.end_rating,
                   COALESCE(m.games, 0) AS games, COALESCE(m.wins, 0) AS wins,
                   COALESCE(m.draws, 0) AS draws, COALESCE(m.losses, 0) AS losses,
                   COALESCE(m.in_100gob, 0) AS in_100gob, m.refreshed_at, m.refresh_error
            FROM players p
            LEFT JOIN monthly_results m ON m.site = p.site AND m.username = p.username AND m.month = ?
            WHERE p.active = 1 AND (? = 0 OR m.site IS NOT NULL)
            """,
            (month, 1 if require_row else 0),
        ).fetchall()
    return [
        ResultRow(r["site"], r["username"], bool(r["has_row"]), r["start_rating"], r["end_rating"], r["games"], r["wins"],
                  r["draws"], r["losses"], bool(r["in_100gob"]), r["refreshed_at"], r["refresh_error"])
        for r in rows
    ]


def apply_refresh(site, username, month, *, expected_watermark, games, wins, draws, losses, end_rating, last_game_at, now):
    """Add a refresh's new games to a month's running totals, in one step.

    `expected_watermark` is the last_game_at the caller read before fetching. If the
    row has moved on since (another refresh got there first) nothing is written and
    this returns False, so the same games can never be counted twice. A closed month
    is never touched. On success the last error is cleared.
    """
    with _transaction() as conn:
        cur = conn.execute(
            """
            UPDATE monthly_results
            SET games = games + ?, wins = wins + ?, draws = draws + ?, losses = losses + ?,
                end_rating = ?, last_game_at = ?, refreshed_at = ?, refresh_error = NULL
            WHERE site = ? AND username = ? AND month = ? AND closed_at IS NULL AND last_game_at IS ?
            """,
            (games, wins, draws, losses, end_rating, last_game_at, now, site, username, month, expected_watermark),
        )
        return cur.rowcount == 1


def record_refresh_error(site, username, month, message):
    """Note why the latest refresh failed. The totals and last-success time stay as they were."""
    with _transaction() as conn:
        conn.execute(
            "UPDATE monthly_results SET refresh_error = ? WHERE site = ? AND username = ? AND month = ? AND closed_at IS NULL",
            (message[:200], site, username, month),
        )


def earliest_month():
    """The earliest month anyone has results for, or None if nothing has been recorded yet."""
    with _transaction() as conn:
        return conn.execute("SELECT MIN(month) AS month FROM monthly_results").fetchone()["month"]


def player_history(site, username):
    """A player's monthly results, newest first, as dicts (month, start_rating, end_rating, games, wins, draws,
    losses, in_100gob, closed_at). Only months the player has a row for: their first is the month they registered."""
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT month, start_rating, end_rating, games, wins, draws, losses, in_100gob, closed_at FROM monthly_results "
            "WHERE site = ? AND username = ? ORDER BY month DESC", (site, username)).fetchall()
    return [dict(r) for r in rows]


def month_row(site, username, month):
    """The monthly_results row as a dict, or None."""
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM monthly_results WHERE site = ? AND username = ? AND month = ?", (site, username, month)
        ).fetchone()
    return dict(row) if row else None

"""SQLite storage: who is registered, and each player's monthly results.

Every function is synchronous and short; call them through asyncio.to_thread from
the bot. A player is identified by (site, username), username case-insensitively.
"""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

DB_PATH = Path(__file__).with_name("playmoreblitz.db")
DB_LOCK_TIMEOUT = 5.0  # seconds to retry if another thread is mid-write, before giving up

ADDED = "added"
REACTIVATED = "reactivated"
EXISTS = "exists"

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
    PRIMARY KEY (site, username, month),
    FOREIGN KEY (site, username) REFERENCES players (site, username)
);
"""


@dataclass(frozen=True)
class Player:
    site: str
    username: str
    added_by: int
    active: bool


@contextmanager
def _transaction():
    """One connection, committed on success and rolled back on error, always closed."""
    conn = sqlite3.connect(DB_PATH, timeout=DB_LOCK_TIMEOUT)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        with conn:
            yield conn
    finally:
        conn.close()


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


def add_player(site, username, owner, month, start_rating):
    """Register a player, or bring a removed one back, and make sure `month` has a row.

    Returns ADDED, REACTIVATED or EXISTS (already active: nothing changes). A
    reactivated player belongs to whoever is adding them now. A month row that
    already exists is left alone, so removing and re-adding within a month keeps
    that month's totals.
    """
    with _transaction() as conn:
        existing = conn.execute(
            "SELECT active FROM players WHERE site = ? AND username = ?", (site, username)
        ).fetchone()

        if existing is None:
            try:
                conn.execute("INSERT INTO players (site, username, added_by) VALUES (?, ?, ?)", (site, username, owner))
            except sqlite3.IntegrityError:  # someone else added the same player at the same moment
                return EXISTS
            outcome = ADDED
        elif existing["active"]:
            return EXISTS
        else:
            # username is set too, so a re-add refreshes the spelling; it matches the
            # row case-insensitively either way, so the month rows stay attached.
            conn.execute(
                "UPDATE players SET active = 1, added_by = ?, added_at = datetime('now'), username = ? WHERE site = ? AND username = ?",
                (owner, username, site, username),
            )
            outcome = REACTIVATED

        conn.execute(
            "INSERT OR IGNORE INTO monthly_results (site, username, month, start_rating, end_rating) VALUES (?, ?, ?, ?, ?)",
            (site, username, month, start_rating, start_rating),
        )
    return outcome


def remove_player(site, username):
    """Mark a player inactive. History is kept. True if they were active."""
    with _transaction() as conn:
        cur = conn.execute("UPDATE players SET active = 0 WHERE site = ? AND username = ? AND active = 1", (site, username))
        return cur.rowcount > 0


def month_row(site, username, month):
    """The monthly_results row as a dict, or None."""
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM monthly_results WHERE site = ? AND username = ? AND month = ?", (site, username, month)
        ).fetchone()
    return dict(row) if row else None

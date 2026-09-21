"""Reading the analysis table for the commands that show it. Nothing here writes, and nothing calls a chess site.

A player's own side of a game is whichever side carries their username (matched ignoring case).
"""

from dataclasses import dataclass

import analysis_queue as q
import store


def _side(row, username):
    return "white" if row["white_username"].lower() == username.lower() else "black"


def player_games(site, username, limit=1, offset=0):
    """The player's most recent analysed games, newest first: each a dict of the table's row plus `side`, the colour
    the player had."""
    with store.transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM game_analysis WHERE site = ? AND status = ? AND (white_username = ? OR black_username = ?) "
            "ORDER BY ended_at DESC, game_id LIMIT ? OFFSET ?", (site, q.DONE, username, username, max(0, limit), max(0, offset))).fetchall()
    return [{**dict(r), "side": _side(r, username)} for r in rows]


def waiting_count(site, username):
    """How many of the player's games are waiting to be analysed (queued, or with the worker right now)."""
    with store.transaction() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM game_analysis WHERE site = ? AND status IN (?, ?) AND (white_username = ? OR black_username = ?)",
            (site, q.PENDING, q.CLAIMED, username, username)).fetchone()[0]


@dataclass(frozen=True)
class MonthSummary:
    total: int  # games of the player's in the queue table this month, whatever became of them
    analysed: int
    waiting: int
    over_limit: int  # not analysed because of the monthly limit
    skipped: int  # not analysed for another reason (a variant, too short, not available)
    failed: int
    accuracy: float | None  # averages over the analysed games, for the player's own side
    opening: float | None
    middlegame: float | None
    endgame: float | None
    acpl: float | None
    inaccuracies: float | None  # per analysed game
    mistakes: float | None
    blunders: float | None


def _mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def month_summary(site, username, month):
    """The analysis of one player's month: what became of their games and the averages of their own side."""
    with store.transaction() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM game_analysis WHERE site = ? AND month = ? AND (white_username = ? OR black_username = ?)",
            (site, month, username, username))]
    done = [r for r in rows if r["status"] == q.DONE]

    def own(r, name):
        return r[f"{_side(r, username)}_{name}"]

    def average(name):
        return _mean([own(r, name) for r in done])

    status = [r["status"] for r in rows]
    skipped = [r for r in rows if r["status"] == q.SKIPPED]
    over = sum(r["skip_reason"] == q.OVER_MONTHLY_LIMIT for r in skipped)
    return MonthSummary(
        total=len(rows), analysed=len(done), waiting=status.count(q.PENDING) + status.count(q.CLAIMED), over_limit=over,
        skipped=len(skipped) - over, failed=status.count(q.FAILED),
        accuracy=average("accuracy"), opening=average("acc_opening"), middlegame=average("acc_middle"), endgame=average("acc_end"),
        acpl=average("acpl"), inaccuracies=average("inaccuracies"), mistakes=average("mistakes"), blunders=average("blunders"))


def monthly_accuracy(site, username):
    """{month: (games analysed, average accuracy of the player's own side)} for every month with an analysed game."""
    with store.transaction() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT month, white_username, white_accuracy, black_accuracy FROM game_analysis WHERE site = ? AND status = ? "
            "AND (white_username = ? OR black_username = ?)", (site, q.DONE, username, username))]
    by_month = {}
    for r in rows:
        own = r["white_accuracy"] if r["white_username"].lower() == username.lower() else r["black_accuracy"]
        if own is not None:
            by_month.setdefault(r["month"], []).append(own)
    return {month: (len(values), sum(values) / len(values)) for month, values in by_month.items()}

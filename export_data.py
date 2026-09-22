"""!export and /export: a member's own games as CSV files for a spreadsheet.

The unit is an account (a username on a site): each gets its own file, one row per game the bot holds for the period,
oldest first, analysed or not (the analysis columns are blank until a game has been analysed). A summary file has one row
per account for the period. Pure functions on the analysis table's rows: no Discord and no network.
"""

import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import analysis
import analysis_queue as q
import game_records
import monthargs
import render_analysis
import render_obit
import store
from openings import opening_family

WEEK_SECONDS = 7 * 24 * 3600
MAX_BYTES = 7_000_000  # what one file may hold: well inside Discord's limit for an attachment

GAME_HEADERS = [
    "ended_utc", "site", "account", "game_link", "colour", "result", "ending", "time_control", "opening", "opening_family", "eco",
    "my_rating", "rating_change", "opponent", "opponent_rating", "analysis", "my_accuracy", "my_opening_accuracy",
    "my_middlegame_accuracy", "my_endgame_accuracy", "my_inaccuracies", "my_mistakes", "my_blunders", "my_acpl",
    "opponent_accuracy", "opponent_inaccuracies", "opponent_mistakes", "opponent_blunders", "site_accuracy",
    "engine_score_after_10_moves", "worst_moments",
]
SUMMARY_HEADERS = [
    "period", "site", "account", "games", "analysed", "wins", "draws", "losses", "win_percent", "rating_start", "rating_end",
    "rating_net", "avg_accuracy", "avg_opening_accuracy", "avg_middlegame_accuracy", "avg_endgame_accuracy",
    "inaccuracies_per_game", "mistakes_per_game", "blunders_per_game", "avg_acpl", "lost_on_time",
]


@dataclass(frozen=True)
class Period:
    kind: str  # "month", "week" (the last seven days) or "all"
    label: str  # for file names: "2026-09", "last-7-days", "all"
    month: str = None
    since: int = None  # epoch seconds, for a week


def parse_period(text, current, now):
    """The Period `text` means, or None if it isn't one. Nothing (or "this") is the current month `current`; "week" is the
    last seven days up to `now` (epoch seconds); "all" is every game held; anything monthargs reads is that month."""
    if text is None or not str(text).strip():
        text = "this"
    word = str(text).strip().lower()
    if word in ("week", "7days", "7d"):
        return Period("week", "last-7-days", since=int(now) - WEEK_SECONDS)
    if word in ("all", "everything"):
        return Period("all", "all")
    month = monthargs.parse_month(word, current)
    return Period("month", month, month=month) if month else None


def describe(period):
    """The period in words."""
    if period.kind == "week":
        return "the last 7 days"
    if period.kind == "all":
        return "all the games held"
    return datetime.strptime(period.month, "%Y-%m").strftime("%B %Y")


def games_for(site, username, period):
    """The game_analysis rows (dicts) `username` played on `site` in the period, oldest first."""
    query = "SELECT * FROM game_analysis WHERE site = ? AND (white_username = ? OR black_username = ?)"
    args = [site, username, username]
    if period.kind == "month":
        query += " AND month = ?"
        args.append(period.month)
    elif period.kind == "week":
        query += " AND ended_at >= ?"
        args.append(period.since)
    with store.transaction() as conn:
        rows = conn.execute(query + " ORDER BY ended_at, game_id", args).fetchall()
    return [dict(r) for r in rows]


def _side(row, username):
    return "white" if row["white_username"].lower() == username.lower() else "black"


def _text(value):
    """A text cell, changed if need be so that a spreadsheet can't take it for a formula."""
    value = "" if value is None else str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def _number(value, places=1):
    if value is None:
        return ""
    return f"{value:.{places}f}"


def has_analysis(row):
    """True if the game has the engine's figures. A game being analysed again keeps its old figures until the new ones
    replace them, so this asks for the figures, not for the status."""
    return row["analysed_at"] is not None


def _analysis(row):
    status = row["status"]
    if has_analysis(row):
        return "analysed"
    if status in (q.PENDING, q.CLAIMED):
        return "waiting"
    if status == q.SKIPPED:
        return f"skipped: {row['skip_reason'] or 'no reason given'}"
    return status  # failed


def _result(row, side):
    if row["result"] == "draw":
        return "draw"
    return "win" if row["result"] == side else "loss"


def game_cells(site, username, row):
    """The cells of one game's line in an account's file, in GAME_HEADERS order."""
    side = _side(row, username)
    theirs = "black" if side == "white" else "white"
    when = datetime.fromtimestamp(row["ended_at"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def mine(key, places=1):
        return _number(row[f"{side}_{key}"], places)

    def theirs_(key, places):
        return _number(row[f"{theirs}_{key}"], places)

    moments = analysis.moments_from_json(row["moments"])
    worst = "; ".join(f"{render_obit.move_label(m.ply)} {m.verdict} -{m.lost:.0f}%" for m in moments if (m.ply % 2 == 1) == (side == "white"))
    score = ""
    if row["eval_ply20"] is not None:
        score = _number((row["eval_ply20"] if side == "white" else -row["eval_ply20"]) / 100, 2)
    opening = row["opening_site"]
    return [
        when, site, username, game_records.game_url(site, row["game_id"]), side, _result(row, side), _text(row["ending"]),
        render_analysis.time_control_label(row["time_control"]), _text(opening), _text(opening_family(opening)) if opening else "",
        _text(row["eco_site"]), _number(row[f"{side}_rating"], 0), _number(row[f"{side}_rating_change"], 0), _text(row[f"{theirs}_username"]),
        _number(row[f"{theirs}_rating"], 0), _analysis(row), mine("accuracy"), mine("acc_opening"), mine("acc_middle"), mine("acc_end"),
        mine("inaccuracies", 0), mine("mistakes", 0), mine("blunders", 0), mine("acpl", 0), theirs_("accuracy", 1),
        theirs_("inaccuracies", 0), theirs_("mistakes", 0), theirs_("blunders", 0), _number(row[f"site_{side}_accuracy"], 1), score, worst,
    ]


def _csv(headers, lines):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(headers)
    writer.writerows(lines)
    return out.getvalue().encode("utf-8-sig")  # the mark lets Excel read accented names properly


def games_csv(site, username, rows):
    """The bytes of an account's file: a header line and a line per game in `rows`."""
    return _csv(GAME_HEADERS, [game_cells(site, username, r) for r in rows])


def _mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def summary_cells(site, username, period, rows):
    """The cells of an account's line in the summary file, in SUMMARY_HEADERS order."""
    sides = [(r, _side(r, username)) for r in rows]
    results = [_result(r, s) for r, s in sides]
    done = [(r, s) for r, s in sides if has_analysis(r)]
    wins, draws, losses = results.count("win"), results.count("draw"), results.count("loss")
    first = next((r[f"{s}_rating"] for r, s in sides if r[f"{s}_rating"] is not None), None)
    last = next(((r[f"{s}_rating"] + r[f"{s}_rating_change"]) for r, s in reversed(sides)
                 if r[f"{s}_rating"] is not None and r[f"{s}_rating_change"] is not None), None)

    def average(key, places=1):
        return _number(_mean([r[f"{s}_{key}"] for r, s in done]), places)

    def per_game(key):
        return _number(sum(r[f"{s}_{key}"] or 0 for r, s in done) / len(done), 2) if done else ""

    timeouts = sum(1 for (r, s), result in zip(sides, results) if result == "loss" and r["ending"] == "timeout")
    return [
        period.label, site, username, len(rows), len(done), wins, draws, losses, _number(100 * wins / len(rows), 1) if rows else "",
        _number(first, 0), _number(last, 0), _number(last - first, 0) if first is not None and last is not None else "",
        average("accuracy"), average("acc_opening"), average("acc_middle"), average("acc_end"),
        per_game("inaccuracies"), per_game("mistakes"), per_game("blunders"), average("acpl", 0), timeouts,
    ]


def summary_csv(lines):
    """The bytes of a summary file from a list of summary_cells lines."""
    return _csv(SUMMARY_HEADERS, lines)


def monthly_summaries(site, username):
    """Every month `game_analysis` holds for this account, newest first, as raw dicts (month, games, analysed, wins,
    draws, losses, rating_start, rating_end, avg_accuracy - the last three None where there is nothing to compute).

    Independent of `store.player_history`/`!history`, which reads `monthly_results` instead: this can hold a month
    that table does not (one pulled in by `!backfill`), and its figures can differ, since the two tables are filled
    separately. For !myhistory."""
    with store.transaction() as conn:
        months = [r["month"] for r in conn.execute(
            "SELECT DISTINCT month FROM game_analysis WHERE site = ? AND (white_username = ? OR black_username = ?) ORDER BY month DESC",
            (site, username, username))]
    out = []
    for month in months:
        rows = games_for(site, username, Period("month", month, month=month))
        sides = [(r, _side(r, username)) for r in rows]
        results = [_result(r, s) for r, s in sides]
        done = [(r, s) for r, s in sides if has_analysis(r)]
        first = next((r[f"{s}_rating"] for r, s in sides if r[f"{s}_rating"] is not None), None)
        last = next(((r[f"{s}_rating"] + r[f"{s}_rating_change"]) for r, s in reversed(sides)
                     if r[f"{s}_rating"] is not None and r[f"{s}_rating_change"] is not None), None)
        out.append({
            "month": month, "games": len(rows), "analysed": len(done),
            "wins": results.count("win"), "draws": results.count("draw"), "losses": results.count("loss"),
            "rating_start": first, "rating_end": last, "avg_accuracy": _mean([r[f"{s}_accuracy"] for r, s in done]),
        })
    return out


def file_name(site, username, period, what="games"):
    """A safe file name: "lichess_pawn_storm_2026-09.csv"."""
    clean = re.sub(r"[^A-Za-z0-9._-]", "_", f"{site}_{username}_{what + '_' if what != 'games' else ''}{period.label}")
    return f"{clean}.csv"

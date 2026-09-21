"""The queue of games waiting to be analysed, and the results that come back.

The bot puts a game in the queue when it sees a registered player has played it. The analysis worker on
another machine asks for a batch (`claim`), analyses them, and reports each back (`submit`, or `release` if it
could not). Nothing here talks to the network: the worker reaches these functions through worker_gateway.py.

Every function is synchronous and short, and does its reading and writing under one write lock, so two
workers, or a worker and the refresher, can never claim or count the same game twice.

Times are UTC epoch seconds. A game is identified by (site, game_id) and belongs to the table's one row for it,
which holds both sides. A registered player is a member; a game is queued if either side is one.
"""

import json
from collections import Counter

import analysis
import settings
import store

PENDING, CLAIMED, DONE, SKIPPED, FAILED = "pending", "claimed", "done", "skipped", "failed"

NOT_STANDARD_START = "not_standard_start"
OVER_MONTHLY_LIMIT = "over_monthly_limit"
UNAVAILABLE = "unavailable"
TOO_SHORT = "too_short"
WORKER_SKIP_REASONS = (NOT_STANDARD_START, UNAVAILABLE, TOO_SHORT)  # the reasons a worker may give for skipping a game

URGENT, NORMAL, LOW = -1, 0, 1  # priorities; URGENT is a game whose review a member asked for (!obit)

# What queue_games reports about each game it was given.
QUEUED, QUEUED_LOW, OVER_LIMIT, ALREADY_QUEUED, NOT_A_MEMBER = "queued", "queued_low", "over_limit", "already_queued", "not_a_member"

# What submit and release report about each game.
ACCEPTED, ALREADY_DONE, REJECTED = "accepted", "already_done", "rejected"


def _tier(number):
    """0, 1 or 2 for a member's `number`th game of the month: normal, low priority, or not analysed."""
    if number <= settings.ANALYSIS_FULL_PRIORITY_GAMES:
        return 0
    return 1 if number <= settings.ANALYSIS_MAX_GAMES else 2


def queue_games(games, now):
    """Put games in the queue. Returns a Counter of what happened to them (QUEUED, QUEUED_LOW, OVER_LIMIT,
    ALREADY_QUEUED, NOT_A_MEMBER).

    Each game is a dict with site, game_id, month, ended_at, result ("white", "black" or "draw"), white_username,
    black_username, and optionally time_control, ending, opening_site, eco_site, white_rating, black_rating,
    white_rating_change and black_rating_change. Games are taken oldest first, so a member's games are numbered
    in the order played: the first ANALYSIS_FULL_PRIORITY_GAMES of a month are normal, the next up to
    ANALYSIS_MAX_GAMES are low priority, and the rest are recorded as skipped (over_monthly_limit). A game
    between two members takes the better of their two priorities, and is skipped only if both are over. A game
    already in the table is left as it is, so calling this again with the same games changes nothing.
    """
    outcome = Counter()
    with store.transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for g in sorted(games, key=lambda g: g["ended_at"]):
            members = [r["username"] for r in conn.execute(
                "SELECT username FROM players WHERE site = ? AND active = 1 AND username IN (?, ?)",
                (g["site"], g["white_username"], g["black_username"]))]
            if not members:
                outcome[NOT_A_MEMBER] += 1
                continue
            if conn.execute("SELECT 1 FROM game_analysis WHERE site = ? AND game_id = ?", (g["site"], g["game_id"])).fetchone():
                outcome[ALREADY_QUEUED] += 1
                continue
            tiers = []
            for username in members:
                played = conn.execute(
                    "SELECT COUNT(*) FROM game_analysis WHERE site = ? AND month = ? AND (white_username = ? OR black_username = ?)",
                    (g["site"], g["month"], username, username)).fetchone()[0]
                tiers.append(_tier(played + 1))
            tier = min(tiers)
            status, reason, priority = (SKIPPED, OVER_MONTHLY_LIMIT, LOW) if tier == 2 else (PENDING, None, tier)
            conn.execute(
                """
                INSERT INTO game_analysis (site, game_id, month, ended_at, time_control, result, ending, opening_site, eco_site,
                    white_username, black_username, white_rating, black_rating, white_rating_change, black_rating_change,
                    status, skip_reason, priority, queued_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (g["site"], g["game_id"], g["month"], g["ended_at"], g.get("time_control"), g["result"], g.get("ending"),
                 g.get("opening_site"), g.get("eco_site"), g["white_username"], g["black_username"], g.get("white_rating"),
                 g.get("black_rating"), g.get("white_rating_change"), g.get("black_rating_change"), status, reason, priority, now))
            outcome[OVER_LIMIT if tier == 2 else QUEUED_LOW if tier == 1 else QUEUED] += 1
    return outcome


def claim(worker, limit, now, rerun_below=None):
    """Hand `worker` up to `limit` games to analyse: normal priority before low, and newest first within each.

    With `rerun_below` (the current method version), once nothing else is waiting the games already analysed by an
    older method version are handed out too, newest first, to be analysed again; their old figures stay in place
    until the new ones replace them.

    The games are marked claimed and each try is counted. A game claimed earlier and not reported back within
    ANALYSIS_CLAIM_MINUTES (the worker died, or the machine went off) is put back in the queue first, or marked
    failed if it has already had ANALYSIS_MAX_ATTEMPTS tries. Returns a list of dicts with site, game_id, month,
    ended_at, white_username, black_username and attempts (this try's number).
    """
    expired = now - settings.ANALYSIS_CLAIM_MINUTES * 60
    with store.transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO analysis_workers (name, last_seen) VALUES (?, ?) "
                     "ON CONFLICT (name) DO UPDATE SET last_seen = excluded.last_seen", (worker, now))
        conn.execute(
            "UPDATE game_analysis SET status = ?, last_error = 'claimed too many times without a result', claimed_by = NULL "
            "WHERE status = ? AND claimed_at <= ? AND attempts >= ?", (FAILED, CLAIMED, expired, settings.ANALYSIS_MAX_ATTEMPTS))
        conn.execute("UPDATE game_analysis SET status = ?, claimed_by = NULL, claimed_at = NULL WHERE status = ? AND claimed_at <= ?",
                     (PENDING, CLAIMED, expired))
        rows = conn.execute(
            "SELECT site, game_id, month, ended_at, white_username, black_username, attempts FROM game_analysis "
            "WHERE status = ? ORDER BY priority, ended_at DESC, game_id LIMIT ?", (PENDING, max(0, limit))).fetchall()
        if rerun_below is not None and len(rows) < limit:
            rows += conn.execute(
                "SELECT site, game_id, month, ended_at, white_username, black_username, attempts FROM game_analysis "
                "WHERE status = ? AND (method_version IS NULL OR method_version < ?) ORDER BY ended_at DESC, game_id LIMIT ?",
                (DONE, rerun_below, limit - len(rows))).fetchall()
        for r in rows:
            conn.execute("UPDATE game_analysis SET status = ?, claimed_by = ?, claimed_at = ?, attempts = attempts + 1 "
                         "WHERE site = ? AND game_id = ?", (CLAIMED, worker, now, r["site"], r["game_id"]))
    return [{**dict(r), "attempts": r["attempts"] + 1} for r in rows]


_SIDE_FIELDS = ("accuracy", "acc_opening", "acc_middle", "acc_end", "inaccuracies", "mistakes", "blunders", "acpl")


def _number(value, low, high, integer=False):
    if isinstance(value, bool) or not isinstance(value, int if integer else (int, float)):
        return False
    return low <= value <= high


def _moments_problem(moments, plies, white, black):
    """Why a list of flagged moves can't be stored, or None. Each is [ply, "i" | "m" | "b", points lost]; the ply
    is odd for White and even for Black, and the numbers of each kind must agree with the side's own counts."""
    if not isinstance(moments, list) or len(moments) > plies:
        return "moments must be a list with no more entries than plies"
    found = {"white": {"i": 0, "m": 0, "b": 0}, "black": {"i": 0, "m": 0, "b": 0}}
    seen = set()
    for item in moments:
        if not isinstance(item, list) or len(item) != 3:
            return "each moment must be [ply, code, points lost]"
        ply, code, lost = item
        if not _number(ply, 1, plies, integer=True) or code not in ("i", "m", "b") or not _number(lost, 0, 100):
            return "a moment has a ply, code or loss out of range"
        if ply in seen:
            return "a ply appears twice in the moments"
        seen.add(ply)
        found["white" if ply % 2 else "black"][code] += 1
    for colour, side in (("white", white), ("black", black)):
        if (found[colour]["i"], found[colour]["m"], found[colour]["b"]) != (side["inaccuracies"], side["mistakes"], side["blunders"]):
            return f"the {colour} moments do not match its counts"
    return None


def _problem(r):
    """Why a result can't be stored, or None if it looks sound."""
    try:
        plies = r["plies"]
        if not _number(plies, 1, 1000, integer=True):
            return "plies must be a whole number from 1 to 1000"
        if not _number(r["method_version"], 1, 10 ** 6, integer=True) or not _number(r["nodes"], 1, 10 ** 12, integer=True):
            return "method_version and nodes must be positive whole numbers"
        if not isinstance(r["engine"], str) or not 0 < len(r["engine"]) <= 100:
            return "engine must be a short name"
        for key in ("middle_ply", "end_ply"):
            if r[key] is not None and not _number(r[key], 0, plies, integer=True):
                return f"{key} must be a ply of the game or null"
        if r["middle_ply"] is not None and r["end_ply"] is not None and r["end_ply"] <= r["middle_ply"]:
            return "the endgame must start after the middlegame"
        if r["end_ply"] is not None and r["middle_ply"] is None:
            return "there is no endgame without a middlegame"
        if r["eval_ply20"] is not None and not _number(r["eval_ply20"], -analysis.CAP, analysis.CAP, integer=True):
            return "eval_ply20 must be centipawns within the cap or null"
        evals = r["evals"]
        if not isinstance(evals, (bytes, bytearray)) or len(evals) != 2 * plies:
            return "evals must be two bytes for each ply"
        for colour in ("white", "black"):
            side = r[colour]
            for field in _SIDE_FIELDS:
                value = side[field]
                if value is None:
                    if field in ("inaccuracies", "mistakes", "blunders"):
                        return f"{colour} {field} is missing"
                    continue
                integer = field in ("inaccuracies", "mistakes", "blunders", "acpl")
                high = plies if field in ("inaccuracies", "mistakes", "blunders") else 2 * analysis.CAP if field == "acpl" else 100
                if not _number(value, 0, high, integer=integer):
                    return f"{colour} {field} is out of range"
        bad = _moments_problem(r["moments"], plies, r["white"], r["black"])
        if bad:
            return bad
        for key in ("site_white_accuracy", "site_black_accuracy"):
            if r.get(key) is not None and not _number(r[key], 0, 100):
                return f"{key} is out of range"
    except (KeyError, TypeError) as missing:
        return f"the result is incomplete or malformed ({missing!r})"
    return None


def submit(worker, results, now):
    """Store finished analyses from `worker`. Returns a list with one (site, game_id, outcome, detail) per result.

    Each result is a dict with site, game_id, method_version, engine, nodes, plies, middle_ply, end_ply, eval_ply20,
    evals (bytes: analysis.pack_evals), white and black (dicts of accuracy, acc_opening, acc_middle, acc_end,
    inaccuracies, mistakes, blunders and acpl) and optionally site_white_accuracy and site_black_accuracy.

    ACCEPTED: stored, and the game is done. ALREADY_DONE: the game was already done by the same or a newer method
    version, so nothing changed. REJECTED (with the reason): the game isn't claimed by this worker, or the result
    fails its checks; the game stays claimed so the worker can release it. A game already done by an older
    method version is overwritten by a newer one.
    """
    reports = []
    with store.transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for r in results:
            if not isinstance(r, dict):
                reports.append((None, None, REJECTED, "a result must be an object"))
                continue
            site, game_id = r.get("site"), r.get("game_id")
            row = conn.execute("SELECT status, claimed_by, method_version FROM game_analysis WHERE site = ? AND game_id = ?",
                               (site, game_id)).fetchone()
            problem = _problem(r)
            if row is None:
                reports.append((site, game_id, REJECTED, "no such game"))
            elif problem:
                reports.append((site, game_id, REJECTED, problem))
            elif row["status"] == DONE and (row["method_version"] or 0) >= r["method_version"]:
                reports.append((site, game_id, ALREADY_DONE, None))
            elif row["status"] == CLAIMED and row["claimed_by"] == worker:
                _store_result(conn, r, now)
                reports.append((site, game_id, ACCEPTED, None))
            elif row["status"] == DONE:  # an older result being replaced by a newer method's
                _store_result(conn, r, now)
                reports.append((site, game_id, ACCEPTED, None))
            else:
                reports.append((site, game_id, REJECTED, f"the game is {row['status']}, not claimed by {worker}"))
    return reports


def _store_result(conn, r, now):
    w, b = r["white"], r["black"]
    conn.execute(
        """
        UPDATE game_analysis SET status = ?, skip_reason = NULL, last_error = NULL, claimed_by = NULL, analysed_at = ?,
            engine = ?, nodes = ?, method_version = ?, plies = ?, middle_ply = ?, end_ply = ?, eval_ply20 = ?, evals = ?, moments = ?,
            white_accuracy = ?, black_accuracy = ?, white_acc_opening = ?, black_acc_opening = ?,
            white_acc_middle = ?, black_acc_middle = ?, white_acc_end = ?, black_acc_end = ?,
            white_inaccuracies = ?, black_inaccuracies = ?, white_mistakes = ?, black_mistakes = ?,
            white_blunders = ?, black_blunders = ?, white_acpl = ?, black_acpl = ?,
            site_white_accuracy = ?, site_black_accuracy = ?
        WHERE site = ? AND game_id = ?
        """,
        (DONE, now, r["engine"], r["nodes"], r["method_version"], r["plies"], r["middle_ply"], r["end_ply"], r["eval_ply20"],
         bytes(r["evals"]), json.dumps(sorted(r["moments"]), separators=(",", ":")),
         w["accuracy"], b["accuracy"], w["acc_opening"], b["acc_opening"], w["acc_middle"], b["acc_middle"], w["acc_end"], b["acc_end"],
         w["inaccuracies"], b["inaccuracies"], w["mistakes"], b["mistakes"], w["blunders"], b["blunders"], w["acpl"], b["acpl"],
         r.get("site_white_accuracy"), r.get("site_black_accuracy"), r["site"], r["game_id"]))


def release(worker, site, game_id, *, skip_reason=None, error=None):
    """Give back a game `worker` has claimed and could not analyse. True if it was theirs to give back.

    With `skip_reason` (one of WORKER_SKIP_REASONS) the game is skipped for good. Otherwise it goes back in the
    queue to be tried again, with `error` noted, unless it has already had ANALYSIS_MAX_ATTEMPTS tries, when it
    is marked failed.
    """
    if skip_reason is not None and skip_reason not in WORKER_SKIP_REASONS:
        raise ValueError(f"a worker can skip a game for {WORKER_SKIP_REASONS}, not {skip_reason!r}")
    with store.transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT attempts FROM game_analysis WHERE site = ? AND game_id = ? AND status = ? AND claimed_by = ?",
                           (site, game_id, CLAIMED, worker)).fetchone()
        if row is None:
            return False
        if skip_reason is not None:
            status, reason = SKIPPED, skip_reason
        else:
            status, reason = (FAILED if row["attempts"] >= settings.ANALYSIS_MAX_ATTEMPTS else PENDING), None
        conn.execute("UPDATE game_analysis SET status = ?, skip_reason = ?, last_error = ?, claimed_by = NULL, claimed_at = NULL "
                     "WHERE site = ? AND game_id = ?", (status, reason, (error or "")[:200] or None, site, game_id))
    return True


def prioritise(site, game_id):
    """Move a game to the front of the queue because a member asked for its review (!obit).

    A queued game jumps ahead of the rest. A game skipped only for being over the monthly limit, or one that failed,
    is queued again: an explicit request overrides the limit. A game already claimed or done, or skipped for a reason
    that can't change (not_standard_start, unavailable, too_short), is left as it is. Returns (status, skip_reason) as
    they are afterwards, or None if there is no such game.
    """
    with store.transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status, skip_reason FROM game_analysis WHERE site = ? AND game_id = ?", (site, game_id)).fetchone()
        if row is None:
            return None
        state, reason = row["status"], row["skip_reason"]
        if state == PENDING:
            conn.execute("UPDATE game_analysis SET priority = ? WHERE site = ? AND game_id = ?", (URGENT, site, game_id))
        elif state == FAILED or (state == SKIPPED and reason == OVER_MONTHLY_LIMIT):
            conn.execute("UPDATE game_analysis SET status = ?, skip_reason = NULL, last_error = NULL, attempts = 0, priority = ?, "
                         "claimed_by = NULL, claimed_at = NULL WHERE site = ? AND game_id = ?", (PENDING, URGENT, site, game_id))
            state, reason = PENDING, None
    return state, reason


def status(now, method_version=None):
    """A picture of the queue for an admin: counts by status, the low-priority backlog, how long the oldest pending
    game has waited (seconds, or None), and each worker's name and how long ago it last asked for work."""
    with store.transaction() as conn:
        counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM game_analysis GROUP BY status")}
        low = conn.execute("SELECT COUNT(*) FROM game_analysis WHERE status = ? AND priority = ?", (PENDING, LOW)).fetchone()[0]
        oldest = conn.execute("SELECT MIN(queued_at) FROM game_analysis WHERE status = ?", (PENDING,)).fetchone()[0]
        workers = conn.execute("SELECT name, last_seen FROM analysis_workers ORDER BY last_seen DESC").fetchall()
        over = conn.execute("SELECT COUNT(*) FROM game_analysis WHERE status = ? AND skip_reason = ?", (SKIPPED, OVER_MONTHLY_LIMIT)).fetchone()[0]
        older = 0 if method_version is None else conn.execute(
            "SELECT COUNT(*) FROM game_analysis WHERE status = ? AND (method_version IS NULL OR method_version < ?)", (DONE, method_version)).fetchone()[0]
    return {
        "counts": {s: counts.get(s, 0) for s in (PENDING, CLAIMED, DONE, SKIPPED, FAILED)},
        "low_priority_pending": low,
        "over_limit": over,
        "older_method": older,
        "oldest_pending_seconds": None if oldest is None else now - oldest,
        "workers": [(w["name"], now - w["last_seen"]) for w in workers],
    }

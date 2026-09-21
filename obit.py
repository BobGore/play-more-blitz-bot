"""!obit: a private review of one of a member's own games, sent to them by direct message.

This module holds the parts that need no Discord: reading a game link or id, finding the member's own game in the
analysis table, and the list of reviews asked for and not yet sent. Turning a game into text is render_obit.py, and
the commands and the sending are in bot.py.

A member can only ask about games played by an account they registered. A request for a game that isn't analysed
yet moves it to the front of the analysis queue (analysis_queue.prioritise) and waits in obit_requests until the
result arrives.
"""

import re

import analysis_queue as q
import store

MAX_WAITING = 3  # reviews one member can have waiting at once
GIVE_UP_SECONDS = 24 * 3600  # a request still unanswered after this long is dropped

SEND, WAIT, CANT, LATE = "send", "wait", "cant", "late"  # what to do with a request: see action_for

_LICHESS_LINK = re.compile(r"(?:https?://)?(?:www\.)?lichess\.org/(?:embed/)?([A-Za-z0-9]{8})(?:[A-Za-z0-9]{4})?(?:[/#?].*)?$")
_CHESSCOM_LINK = re.compile(r"(?:https?://)?(?:www\.)?chess\.com/game/((?:live|daily)/[0-9]+)(?:[/#?].*)?$")
_BARE_LICHESS = re.compile(r"[A-Za-z0-9]{8}")
_BARE_CHESSCOM = re.compile(r"[0-9]{6,15}")


def candidates(text):
    """The (site, game_id) pairs a game link or id could mean, best first; empty if it isn't one.

    A link says which site it is. A bare id doesn't: eight letters and digits is a Lichess id, and digits alone are a
    Chess.com id (a live game or a daily one), so a bare id may give several pairs and the caller looks for the one
    among the member's own games.
    """
    text = (text or "").strip().strip("<>").strip()
    if not text or len(text) > 200:
        return []
    match = _LICHESS_LINK.match(text)
    if match:
        return [("lichess", match.group(1))]
    match = _CHESSCOM_LINK.match(text)
    if match:
        return [("chess.com", match.group(1))]
    pairs = []
    if _BARE_LICHESS.fullmatch(text):
        pairs.append(("lichess", text))
    if _BARE_CHESSCOM.fullmatch(text):
        pairs += [("chess.com", f"live/{text}"), ("chess.com", f"daily/{text}")]
    return pairs


def side_of(row, username):
    """"white" or "black": the side `username` played in the game row (matched ignoring case), or None."""
    if row["white_username"].lower() == username.lower():
        return "white"
    if row["black_username"].lower() == username.lower():
        return "black"
    return None


def _mine(row, accounts):
    for account in accounts:
        if account.site == row["site"]:
            side = side_of(row, account.username)
            if side:
                return account, side
    return None


def find_game(accounts, refs):
    """The member's own game among `refs` (candidates' pairs): (row as a dict, account, side), or None if none of
    them is a game the analysis table holds that one of `accounts` (store.Player) played."""
    with store.transaction() as conn:
        for site, game_id in refs:
            row = conn.execute("SELECT * FROM game_analysis WHERE site = ? AND game_id = ?", (site, game_id)).fetchone()
            mine = _mine(row, accounts) if row else None
            if mine:
                return dict(row), mine[0], mine[1]
    return None


def latest_game(accounts):
    """The member's most recent game that is, or can be, analysed: (row as a dict, account, side), or None.

    Games skipped for a reason that can't change (not standard chess, too short) are passed over, but one skipped only
    for being over the monthly limit counts, since asking for it lifts the limit.
    """
    best = None
    with store.transaction() as conn:
        for account in accounts:
            row = conn.execute(
                "SELECT * FROM game_analysis WHERE site = ? AND (white_username = ? OR black_username = ?) "
                "AND (status IN (?, ?, ?, ?) OR (status = ? AND skip_reason = ?)) ORDER BY ended_at DESC, game_id LIMIT 1",
                (account.site, account.username, account.username, q.PENDING, q.CLAIMED, q.DONE, q.FAILED, q.SKIPPED, q.OVER_MONTHLY_LIMIT)).fetchone()
            if row and (best is None or row["ended_at"] > best[0]["ended_at"]):
                best = (dict(row), account, side_of(row, account.username))
    return best


def game_row(site, game_id):
    with store.transaction() as conn:
        row = conn.execute("SELECT * FROM game_analysis WHERE site = ? AND game_id = ?", (site, game_id)).fetchone()
    return dict(row) if row else None


# --- the requests waiting to be answered ----------------------------------------------------------------------------

def add_request(user_id, site, game_id, username, channel_id, now):
    """Note that `user_id` wants the review of a game. Asking again for the same game just renews the request."""
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO obit_requests (user_id, site, game_id, username, channel_id, requested_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (user_id, site, game_id) DO UPDATE SET username = excluded.username, channel_id = excluded.channel_id, "
            "requested_at = excluded.requested_at", (user_id, site, game_id, username, channel_id, now))


def waiting_count(user_id, excluding=None):
    """How many reviews `user_id` has waiting, not counting the game `excluding` ((site, game_id))."""
    with store.transaction() as conn:
        rows = conn.execute("SELECT site, game_id FROM obit_requests WHERE user_id = ?", (user_id,)).fetchall()
    return sum(1 for r in rows if excluding != (r["site"], r["game_id"]))


def outstanding():
    """Every request waiting, as dicts, oldest first."""
    with store.transaction() as conn:
        rows = conn.execute("SELECT * FROM obit_requests ORDER BY requested_at, user_id, site, game_id").fetchall()
    return [dict(r) for r in rows]


def close_request(user_id, site, game_id):
    """Remove a request. True if it was there, so whoever gets True is the one to answer it (nobody answers twice)."""
    with store.transaction() as conn:
        return conn.execute("DELETE FROM obit_requests WHERE user_id = ? AND site = ? AND game_id = ?", (user_id, site, game_id)).rowcount > 0


def action_for(request, row, now):
    """What to do with a request, given its game's row in the analysis table (None if the game has gone):
    SEND the review, WAIT for the analysis, say the game CAN'T be analysed, or say it took too LATE."""
    if row is None or row["status"] == q.FAILED or row["status"] == q.SKIPPED:
        return CANT
    if row["status"] == q.DONE:
        return SEND
    return LATE if now - request["requested_at"] > GIVE_UP_SECONDS else WAIT


SKIP_WORDS = {
    q.NOT_STANDARD_START: "it didn't start from the standard position (chess960 and set-up games can't be analysed)",
    q.TOO_SHORT: "it was too short to say anything useful about",
    q.UNAVAILABLE: "I couldn't get its moves from the site",
}


def cant_reason(row):
    """Why a game can't be analysed, in words that follow "I couldn't review that game:"."""
    if row is None:
        return "I no longer hold it"
    if row["status"] == q.SKIPPED:
        return SKIP_WORDS.get(row["skip_reason"], "it was skipped")
    return "the analysis failed (the bot's admin can see why)"

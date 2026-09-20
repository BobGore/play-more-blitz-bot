"""Chess.com and Lichess lookups.

A month of rated standard blitz games for a player, and their current blitz
rating. Both sites are turned into the same Game record, so nothing downstream
needs to know which site a game came from.

Site calls are strictly serial per site: Chess.com may block parallel requests
and Lichess asks for one request at a time.

Anything unexpected (an unknown result code, a game that isn't standard rated
blitz, a missing rating change) raises instead of being guessed at.
"""

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone

import aiohttp

SITES = ("chess.com", "lichess")

# Both sites ask for a contact address in the User-Agent so they can reach the
# owner before blocking a misbehaving bot. It comes from the environment, not
# the code, because this repository is public.
CONTACT = os.environ.get("CONTACT", "").strip()
USER_AGENT = "PlayMoreBlitz-Bot/1.0" + (f" (contact: {CONTACT})" if CONTACT else "")

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)
# Lichess streams a game export at about 11-12 games a second, so a busy month
# (375 games took 33s) can't have a short flat limit. A generous overall cap
# plus a short limit on the stream stalling keeps a healthy month working
# while a stuck connection still fails quickly.
MONTH_TIMEOUT = aiohttp.ClientTimeout(total=300, sock_read=30)

# Lichess throttles its game export harder than the rest of its API.
LICHESS_EXPORT_MIN_INTERVAL = 2.0  # seconds between the end of one export and the next
_lichess_lock = asyncio.Lock()
_lichess_last_export = 0.0
_chesscom_lock = asyncio.Lock()


class SourceError(Exception):
    """A site lookup failed. The message is safe to show in Discord."""


class NoSuchUser(SourceError):
    """The account doesn't exist on that site."""


class NoRating(SourceError):
    """The account exists but has no usable blitz rating."""


class UnknownResult(SourceError):
    """A game ended in a way this code doesn't know how to count."""


@dataclass(frozen=True)
class Game:
    ended_at: datetime  # aware, UTC
    colour: str  # "white" or "black"
    result: str  # "W", "D" or "L", from this player's side
    ending: str  # what decided it: "resigned", "checkmated", "timeout", "repetition", ...
    rating_after: int
    # Lichess gives this directly. Chess.com only gives the rating after each
    # game, so it is the previous game's rating_after, and None for the first
    # game of a month (the caller knows the start-of-month rating).
    rating_before: int | None
    opponent: str
    opponent_rating: int | None
    moves: int  # full moves
    time_control: str  # "initial+increment" in seconds, e.g. "300+5"
    opening: str | None  # as the site names it; grouped later, in openings.py
    eco: str | None
    url: str


def month_bounds(month):
    """(start, end) of a "YYYY-MM" UTC calendar month; start inclusive, end exclusive."""
    try:
        year, mon = (int(part) for part in month.split("-"))
        start = datetime(year, mon, 1, tzinfo=timezone.utc)
    except ValueError:
        raise ValueError(f"month must look like 2026-09, got {month!r}") from None
    end = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=timezone.utc)
    return start, end


def current_month(now=None):
    """The "YYYY-MM" UTC month containing `now` (the present moment by default)."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m")


def next_month(month):
    """The "YYYY-MM" month after `month`."""
    _, end = month_bounds(month)
    return f"{end.year:04d}-{end.month:02d}"


def previous_month(month):
    """The "YYYY-MM" month before `month`."""
    start, _ = month_bounds(month)
    year, mon = (start.year - 1, 12) if start.month == 1 else (start.year, start.month - 1)
    return f"{year:04d}-{mon:02d}"


# Both sites allow letters, digits, "_" and "-". Usernames are put into URLs, so
# anything else is refused before it can bend a request (a "/" or "?" or "..").
_USERNAME = re.compile(r"[A-Za-z0-9_-]{2,30}")


def shorten(text, limit=40):
    """Text a user typed, cut down for echoing back, so a huge argument can't push a reply
    over Discord's message limit (or turn a short reply into a wall of text)."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def check_username(username):
    if not _USERNAME.fullmatch(username):
        raise SourceError(f"'{shorten(username)}' isn't a valid username (letters, numbers, - and _ only)")


# --- Chess.com -------------------------------------------------------------

CC_DRAW = {"agreed", "repetition", "stalemate", "insufficient", "50move", "timevsinsufficient"}
CC_LOSS = {"checkmated", "resigned", "timeout", "abandoned", "lose"}


def _chesscom_result(me, opponent):
    code = me["result"]
    if code == "win":
        return "W", opponent["result"]  # the loser's code says how it ended
    if code in CC_DRAW:
        return "D", code
    if code in CC_LOSS:
        return "L", code
    raise UnknownResult(f"chess.com result code '{code}' isn't one I know how to count")


def _pgn_header(pgn, name):
    match = re.search(r'\[' + name + r' "([^"]*)"\]', pgn)
    return match.group(1) if match else None


def _chesscom_moves(pgn):
    movetext = re.split(r"\n\s*\n", pgn, maxsplit=1)[-1]
    movetext = re.sub(r"\{[^}]*\}", "", movetext)  # clock comments
    numbers = [int(n) for n in re.findall(r"(\d+)\.(?!\.)", movetext)]  # "12." but not "12..."
    return max(numbers, default=0)


def _chesscom_opening(pgn):
    """The opening as Chess.com's URL slug, with the trailing move list cut off.

    Kept as the slug (hyphens and all) because real hyphens, as in Caro-Kann,
    can't be told apart from word separators here. openings.py does the grouping.
    """
    url = _pgn_header(pgn, "ECOUrl")
    if not url or "/openings/" not in url:
        return None
    slug = url.split("/openings/", 1)[1].split("...", 1)[0]
    return re.sub(r"-\d.*$", "", slug) or None


def _normalise_time_control(value):
    return value if "+" in value else f"{value}+0"


def parse_chesscom_month(data, username):
    """Rated standard blitz games from one Chess.com monthly archive, oldest first."""
    user = username.lower()
    games = []
    for raw in data.get("games", []):
        if raw.get("rules") != "chess" or raw.get("time_class") != "blitz" or not raw.get("rated"):
            continue  # bughouse and other variants show up as blitz here, so rules matters

        white, black = raw["white"], raw["black"]
        if white["username"].lower() == user:
            colour, me, opponent = "white", white, black
        elif black["username"].lower() == user:
            colour, me, opponent = "black", black, white
        else:
            raise SourceError(f"chess.com sent a game without {username} in it: {raw.get('url')}")

        result, ending = _chesscom_result(me, opponent)
        pgn = raw.get("pgn", "")
        games.append(
            Game(
                ended_at=datetime.fromtimestamp(raw["end_time"], tz=timezone.utc),
                colour=colour,
                result=result,
                ending=ending,
                rating_after=me["rating"],
                rating_before=None,
                opponent=opponent["username"],
                opponent_rating=opponent.get("rating"),
                moves=_chesscom_moves(pgn),
                time_control=_normalise_time_control(raw["time_control"]),
                opening=_chesscom_opening(pgn),
                eco=_pgn_header(pgn, "ECO"),
                url=raw.get("url", ""),
            )
        )

    games.sort(key=lambda g: g.ended_at)
    for i in range(1, len(games)):
        games[i] = replace(games[i], rating_before=games[i - 1].rating_after)
    return games


# --- Lichess ---------------------------------------------------------------

# Statuses that end a game with a winner, and ones that end it without.
LI_DECIDED = {"mate": "checkmated", "resign": "resigned", "timeout": "timeout", "outoftime": "timeout", "cheat": "cheat"}
LI_DRAWN = {"draw": "draw", "stalemate": "stalemate", "insufficientMaterialClaim": "insufficient"}
# A flag fall (or a player leaving) against someone who can't checkmate is a
# draw, and Lichess still reports the status as a timeout, with no winner.
# Confirmed on real games: Result 1/2-1/2, Termination "Time forfeit".
LI_TIMEOUT_DRAWABLE = {"timeout", "outoftime"}
LI_NOT_PLAYED = {"aborted", "noStart"}  # never a real game; skipped
# Still being played when we fetched. Skipped, not an error: the next incremental
# fetch (which starts after the last game counted) picks it up once it has finished.
LI_IN_PROGRESS = {"created", "started"}


def parse_lichess_month(body, username):
    """Rated standard blitz games from a Lichess NDJSON export, oldest first."""
    user = username.lower()
    games = []
    for line in body.splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            raise SourceError("lichess sent something that wasn't JSON") from None

        game_id = raw.get("id", "?")
        if raw.get("variant") != "standard" or raw.get("perf") != "blitz" or not raw.get("rated"):
            raise UnknownResult(f"lichess game {game_id} isn't rated standard blitz, which I asked for")

        status = raw.get("status")
        if status in LI_NOT_PLAYED or status in LI_IN_PROGRESS:
            continue

        white, black = raw["players"]["white"], raw["players"]["black"]
        if (white.get("user") or {}).get("id", "").lower() == user:
            colour, me, opponent = "white", white, black
        elif (black.get("user") or {}).get("id", "").lower() == user:
            colour, me, opponent = "black", black, white
        else:
            raise SourceError(f"lichess sent a game without {username} in it: {game_id}")

        winner = raw.get("winner")
        if status in LI_DECIDED:
            if winner in ("white", "black"):
                result, ending = ("W" if winner == colour else "L"), LI_DECIDED[status]
            elif status in LI_TIMEOUT_DRAWABLE:
                result, ending = "D", LI_DECIDED[status]
            else:
                raise UnknownResult(f"lichess game {game_id} is '{status}' but has no winner")
        elif status in LI_DRAWN:
            if winner:
                raise UnknownResult(f"lichess game {game_id} is '{status}' but has a winner")
            result, ending = "D", LI_DRAWN[status]
        else:
            raise UnknownResult(f"lichess game {game_id} has status '{status}', which I don't know how to count")

        if me.get("ratingDiff") is None:
            raise SourceError(f"lichess game {game_id} has no rating change, so I can't work out the rating")
        clock = raw.get("clock")
        if not clock:
            raise SourceError(f"lichess game {game_id} has no clock, which blitz always has")

        opening = raw.get("opening") or {}
        games.append(
            Game(
                ended_at=datetime.fromtimestamp(raw["lastMoveAt"] / 1000, tz=timezone.utc),
                colour=colour,
                result=result,
                ending=ending,
                rating_after=me["rating"] + me["ratingDiff"],
                rating_before=me["rating"],
                opponent=(opponent.get("user") or {}).get("name", "anonymous"),
                opponent_rating=opponent.get("rating"),
                moves=(len(raw.get("moves", "").split()) + 1) // 2,
                time_control=f"{clock['initial']}+{clock['increment']}",
                opening=opening.get("name"),
                eco=opening.get("eco"),
                url=f"https://lichess.org/{game_id}",
            )
        )

    games.sort(key=lambda g: g.ended_at)
    return games


# --- Network ---------------------------------------------------------------


async def _get(session, site, url, username, *, timeout, params=None, ndjson=False):
    """GET and return the body text, turning every failure into a SourceError."""
    headers = {"User-Agent": USER_AGENT}
    if ndjson:
        headers["Accept"] = "application/x-ndjson"
    try:
        async with session.get(url, params=params, headers=headers, timeout=timeout) as resp:
            if resp.status == 404:
                raise NoSuchUser(f"no {site} account '{username}'")
            if resp.status == 429:
                raise SourceError(f"{site} is rate limiting me (HTTP 429), try again in a few minutes")
            if resp.status != 200:
                raise SourceError(f"{site} returned HTTP {resp.status}")
            return await resp.text()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise SourceError(f"couldn't reach {site} ({str(exc) or type(exc).__name__})") from exc


def _json(site, text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise SourceError(f"{site} sent something that wasn't JSON") from None


def only_after(games, after):
    """Games that ended strictly after `after` (all of them if `after` is None)."""
    return games if after is None else [g for g in games if g.ended_at > after]


def lichess_export_params(month, after=None, limit=None):
    """Query for a Lichess export of a month, optionally only games after a watermark.

    `since` is only a hint about what to transfer, not an exact cut. Probed on
    real games: Lichess compares it to a game's last move at about one-second
    granularity, so a game whose last move is at the watermark comes back again.
    The exact cut (strictly after the watermark) is done by only_after().
    """
    start, end = month_bounds(month)
    since = int(start.timestamp() * 1000)
    if after is not None:
        since = max(since, int(after.timestamp() * 1000))
    params = {
        "since": since,
        "until": int(end.timestamp() * 1000),
        "perfType": "blitz",
        "rated": "true",
        "opening": "true",
        "sort": "dateAsc",
    }
    if limit is not None:
        params["max"] = limit
    return params


async def month_games(session, site, username, month, *, after=None, limit=None):
    """Rated standard blitz games in a "YYYY-MM" UTC month, oldest first. Empty if none.

    With `after` (an aware UTC datetime, the end of the last game already counted)
    only games that ended strictly later are returned. Lichess is asked only for
    recent games, and Chess.com, which only serves a whole month, is fetched whole;
    both are then cut exactly by only_after(). Each game's rating_before is still
    right, because Chess.com's chain is built before the cut.

    With `limit`, only that many of the oldest games are returned. On Lichess that
    is asked of the site, so a busy month isn't downloaded to read one game.
    """
    check_username(username)
    start, _ = month_bounds(month)

    if site == "chess.com":
        url = f"https://api.chess.com/pub/player/{username.lower()}/games/{start.year}/{start.month:02d}"
        async with _chesscom_lock:
            text = await _get(session, site, url, username, timeout=MONTH_TIMEOUT)
        games = only_after(parse_chesscom_month(_json(site, text), username), after)
        return games[:limit] if limit is not None else games

    if site == "lichess":
        global _lichess_last_export
        url = f"https://lichess.org/api/games/user/{username}"
        params = lichess_export_params(month, after, limit)
        async with _lichess_lock:
            wait = LICHESS_EXPORT_MIN_INTERVAL - (time.monotonic() - _lichess_last_export)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                text = await _get(session, site, url, username, timeout=MONTH_TIMEOUT, params=params, ndjson=True)
            finally:
                _lichess_last_export = time.monotonic()
        return only_after(parse_lichess_month(text, username), after)

    raise ValueError(f"unknown site {site!r}")


async def current_rating(session, site, username):
    """The player's current blitz rating. Raises NoSuchUser or NoRating."""
    check_username(username)
    if site == "chess.com":
        url = f"https://api.chess.com/pub/player/{username.lower()}/stats"
        async with _chesscom_lock:
            text = await _get(session, site, url, username, timeout=REQUEST_TIMEOUT)
        entry = _json(site, text).get("chess_blitz")
        if not entry or "last" not in entry:
            raise NoRating(f"'{username}' has no rated chess.com blitz games")
        return entry["last"]["rating"]

    if site == "lichess":
        url = f"https://lichess.org/api/user/{username}"
        async with _lichess_lock:
            text = await _get(session, site, url, username, timeout=REQUEST_TIMEOUT)
        data = _json(site, text)
        if data.get("disabled"):
            raise NoRating(f"lichess account '{username}' is closed")
        perf = data.get("perfs", {}).get("blitz")
        if not perf or not perf.get("games"):
            raise NoRating(f"'{username}' has no rated lichess blitz games")
        return perf["rating"]

    raise ValueError(f"unknown site {site!r}")


async def account_name(session, site, username):
    """The account's own spelling of its username ("Alice" for a typed "ALICE").

    Chess.com's profile lowercases its `username` field but ends its `url` in the
    real spelling; Lichess's `username` field is the real spelling. This is for
    display only, so if the site's answer doesn't match what was typed (ignoring
    case) the typed spelling is kept. Raises NoSuchUser for an unknown account.
    """
    check_username(username)
    if site == "chess.com":
        url = f"https://api.chess.com/pub/player/{username.lower()}"
        async with _chesscom_lock:
            text = await _get(session, site, url, username, timeout=REQUEST_TIMEOUT)
        shown = _json(site, text).get("url", "").rstrip("/").rsplit("/", 1)[-1]
    elif site == "lichess":
        url = f"https://lichess.org/api/user/{username}"
        async with _lichess_lock:
            text = await _get(session, site, url, username, timeout=REQUEST_TIMEOUT)
        shown = _json(site, text).get("username", "")
    else:
        raise ValueError(f"unknown site {site!r}")

    return shown if shown.lower() == username.lower() else username


async def start_rating(session, site, username, month):
    """The blitz rating the player held at the start of `month`, per the brief.

    Also proves the account exists: an unknown user raises NoSuchUser.

    Chess.com only reports the rating after each game, so it is the rating after
    the last game of the previous month. With none, it is the current rating: no
    games at all this month means it hasn't moved, and if they have played this
    month already the games before registering are missing from the gain, which
    the brief accepts.

    Lichess reports the rating before each game, so it is the rating before the
    month's first game (one game is fetched, not the month), or the current
    rating if there isn't one.
    """
    if site == "chess.com":
        last_month = await month_games(session, site, username, previous_month(month))
        if last_month:
            return last_month[-1].rating_after
    elif site == "lichess":
        first = await month_games(session, site, username, month, limit=1)
        if first:
            return first[0].rating_before
    else:
        raise ValueError(f"unknown site {site!r}")

    return await current_rating(session, site, username)

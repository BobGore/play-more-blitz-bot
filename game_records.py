"""Turn the games the refresher fetched into the records the analysis queue takes.

A sources.Game is one player's view of a game. The queue holds one row per game with both sides, so this
works out who was white and who was black, what the result was, and the game's id on its site. Pure functions:
no network and no database.
"""

import re
from datetime import timezone

# The last part of a game's URL is what identifies it: 8 letters and digits on Lichess, and on Chess.com
# "live/" or "daily/" and a number (the two kinds of game are numbered separately).
_LICHESS_ID = re.compile(r"https?://(?:www\.)?lichess\.org/([A-Za-z0-9]{8})(?:[/#?].*)?$")
_CHESSCOM_ID = re.compile(r"https?://(?:www\.)?chess\.com/game/((?:live|daily)/[0-9]+)(?:[/#?].*)?$")


def game_id(site, url):
    """The game's id on its site, from its URL, or None if the URL isn't one we recognise."""
    pattern = _LICHESS_ID if site == "lichess" else _CHESSCOM_ID if site == "chess.com" else None
    match = pattern.match(url or "") if pattern else None
    return match.group(1) if match else None


def _winner(game):
    if game.result == "D":
        return "draw"
    return game.colour if game.result == "W" else ("black" if game.colour == "white" else "white")


def record(site, username, game):
    """The queue record for `game`, played by `username` on `site`, or None if it has no usable id."""
    gid = game_id(site, game.url)
    if gid is None:
        return None
    mine = {"username": username, "rating": game.rating_before,
            "change": None if game.rating_before is None else game.rating_after - game.rating_before}
    theirs = {"username": game.opponent, "rating": game.opponent_rating, "change": None}
    white, black = (mine, theirs) if game.colour == "white" else (theirs, mine)
    ended = game.ended_at.astimezone(timezone.utc)
    return {
        "site": site,
        "game_id": gid,
        "month": ended.strftime("%Y-%m"),
        "ended_at": int(ended.timestamp()),
        "time_control": game.time_control,
        "result": _winner(game),
        "ending": game.ending,
        "opening_site": game.opening,
        "eco_site": game.eco,
        "white_username": white["username"],
        "black_username": black["username"],
        "white_rating": white["rating"],
        "black_rating": black["rating"],
        "white_rating_change": white["change"],
        "black_rating_change": black["change"],
    }


def records(site, username, games):
    """(records, how many games had no usable id and were left out)."""
    made = [record(site, username, g) for g in games]
    return [r for r in made if r is not None], made.count(None)


def game_url(site, game_id, ply=None):
    """The address of a game on its site, from the id game_id() found. Lichess can open the game at a ply; for
    Chess.com the ply is ignored until its link form is checked."""
    if site == "lichess":
        return f"https://lichess.org/{game_id}" + (f"#{int(ply)}" if ply else "")
    return f"https://www.chess.com/game/{game_id}"

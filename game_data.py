"""Reading a game as a site sends it: its moves, whether it can be analysed, and the site's own accuracy figures.

Used by the analysis worker (worker.py). Pure functions on the JSON the two sites send, with no network and no
chess library. A game that can't be analysed raises NotAnalysable with the reason to give the queue.
"""

import re
from dataclasses import dataclass

STANDARD_START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

# The reasons a worker may give the queue for skipping a game (see analysis_queue.WORKER_SKIP_REASONS).
NOT_STANDARD_START = "not_standard_start"
UNAVAILABLE = "unavailable"
TOO_SHORT = "too_short"

MIN_PLIES = 6  # a game shorter than this has nothing to analyse


class NotAnalysable(Exception):
    def __init__(self, reason, detail):
        super().__init__(detail)
        self.reason = reason


@dataclass(frozen=True)
class GameData:
    moves: tuple  # SAN moves in order
    site_white_accuracy: float | None  # the site's own figure, when it has one
    site_black_accuracy: float | None


def _accuracy(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100 else None


def _checked(moves, min_plies):
    if len(moves) < min_plies:
        raise NotAnalysable(TOO_SHORT, f"only {len(moves)} moves")
    return tuple(moves)


def from_lichess(raw, min_plies=MIN_PLIES):
    """A game from Lichess's game export (moves=true; accuracy=true for the site's own figures)."""
    if raw.get("variant") != "standard":
        raise NotAnalysable(NOT_STANDARD_START, f"the variant is {raw.get('variant')!r}")
    if raw.get("initialFen"):
        raise NotAnalysable(NOT_STANDARD_START, "the game does not start from the standard position")
    if raw.get("status") in ("aborted", "noStart", "created", "started"):
        raise NotAnalysable(TOO_SHORT if raw.get("status") in ("aborted", "noStart") else UNAVAILABLE, f"the game is {raw.get('status')}")
    moves = raw.get("moves")
    if not isinstance(moves, str):
        raise NotAnalysable(UNAVAILABLE, "the export has no moves")
    players = raw.get("players") or {}
    figures = {colour: ((players.get(colour) or {}).get("analysis") or {}).get("accuracy") for colour in ("white", "black")}
    return GameData(_checked(moves.split(), min_plies), _accuracy(figures["white"]), _accuracy(figures["black"]))


_COMMENT = re.compile(r"\{[^}]*\}")
_VARIATION = re.compile(r"\([^)]*\)")
_MOVE_NUMBER = re.compile(r"[0-9]+\.(?:\.\.)?")
_RESULTS = {"1-0", "0-1", "1/2-1/2", "*"}


def moves_from_pgn(pgn):
    """The SAN moves of a PGN's move text, without move numbers, clock comments, results or annotations."""
    text = pgn.split("\n\n", 1)[1] if "\n\n" in pgn else pgn
    text = _VARIATION.sub(" ", _COMMENT.sub(" ", text))
    return [t for t in _MOVE_NUMBER.sub(" ", text).split() if t not in _RESULTS and not t.startswith("$")]


def _pgn_header(pgn, name):
    match = re.search(rf'^\[{name} "([^"]*)"\]', pgn, re.M)
    return match.group(1) if match else None


def from_chesscom(raw, min_plies=MIN_PLIES):
    """A game from a Chess.com monthly archive."""
    if raw.get("rules") != "chess":
        raise NotAnalysable(NOT_STANDARD_START, f"the rules are {raw.get('rules')!r}")
    pgn = raw.get("pgn")
    if not isinstance(pgn, str) or not pgn.strip():
        raise NotAnalysable(UNAVAILABLE, "the archive has no moves for this game")
    start = raw.get("initial_setup") or _pgn_header(pgn, "FEN")
    if start is not None and start != STANDARD_START:
        raise NotAnalysable(NOT_STANDARD_START, "the game does not start from the standard position")
    if _pgn_header(pgn, "SetUp") == "1" and _pgn_header(pgn, "FEN") not in (None, STANDARD_START):
        raise NotAnalysable(NOT_STANDARD_START, "the game does not start from the standard position")
    accuracies = raw.get("accuracies") or {}
    return GameData(_checked(moves_from_pgn(pgn), min_plies), _accuracy(accuracies.get("white")), _accuracy(accuracies.get("black")))


def find_chesscom_game(archive, game_id):
    """The game with this id (`live/<number>` or `daily/<number>`) in a monthly archive, or None."""
    suffix = f"/game/{game_id}"
    for raw in archive.get("games", []):
        url = raw.get("url") or ""
        if url.endswith(suffix) and url[: -len(suffix)].rstrip("/").endswith("chess.com"):
            return raw
    return None

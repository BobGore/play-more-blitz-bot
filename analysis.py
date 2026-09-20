"""Turn an engine's evaluation of every position in a game into the figures we keep: accuracy overall
and by phase, inaccuracies, mistakes and blunders, and average centipawn loss.

This follows the method Lichess publishes for its computer analysis (thanks to the Lichess team), so a game
analysed here reads on the same scale as a game analysed there. Pure functions: no engine, no network, no
chess library. Where the phases start comes from divider.py; the worker in worker.py runs the engine.

A score is ("cp", centipawns) or ("mate", n), always from White's point of view: a positive number is good
for White, and "mate" n is a forced mate in n moves (positive: White mates, negative: Black mates).

`scores[i]` is the evaluation after ply i + 1, so `scores[0]` is the position after White's first move. The
starting position counts as 15 centipawns, as it does on Lichess.
"""

import math
import statistics
import struct
from dataclasses import dataclass

METHOD_VERSION = 1  # raised whenever a change to the method would change the figures, so older rows can be re-run
INITIAL_CP = 15
CAP = 1000  # accuracy treats every evaluation as at most this many centipawns, and a forced mate as exactly this
_K = -0.00368208  # Lichess's fit of centipawns to winning chances

# Winning chances (a scale of -1 to +1, so 0.1 is 5 percentage points of win probability) lost by a move.
INACCURACY, MISTAKE, BLUNDER = 0.1, 0.2, 0.3

_ACCURACY_A, _ACCURACY_B, _ACCURACY_C = 103.1668100711649, -0.04354415386753951, -3.166924740191411
_MAX_WINDOW, _MIN_WINDOW = 8, 2
_MIN_WEIGHT, _MAX_WEIGHT = 0.5, 12.0
_OPENING_SPAN = 20  # eval_ply20 is the evaluation after this many plies


def clamp_cp(cp):
    return max(-CAP, min(CAP, cp))


def as_cp(score):
    """A score as centipawns for accuracy: capped, and a forced mate counts as the cap."""
    kind, value = score
    return clamp_cp(value) if kind == "cp" else (CAP if value > 0 else -CAP)


def win_percent(cp):
    """White's chance of winning, 0 to 100, from an evaluation capped at +-1000 centipawns (used for accuracy)."""
    return 50 + 50 * (2 / (1 + math.exp(_K * clamp_cp(cp))) - 1)


def winning_chances(cp):
    """White's winning chances from -1 to +1, from the raw evaluation. NOT capped: judging a move uses the
    real evaluation, so a slip in an already lost position of -13 pawns still counts."""
    return 2 / (1 + math.exp(_K * cp)) - 1


def move_accuracy(before, after):
    """Accuracy of one move from the mover's win percent before and after it (0 to 100)."""
    if after >= before:
        return 100.0
    raw = _ACCURACY_A * math.exp(_ACCURACY_B * (before - after)) + _ACCURACY_C
    return max(0.0, min(100.0, raw + 1))  # +1: allowance for the analysis being imperfect


def game_accuracy(first_white, cps, initial_cp=INITIAL_CP):
    """Accuracy of each side over a run of consecutive moves, as {"white": x, "black": y}.

    `cps` are the capped evaluations after each move of the run and `first_white` says whether the first of
    those moves is White's. A side that made no move in the run is left out. Each side's figure is the mean of
    a weighted mean, which counts moves in volatile stretches more, and a harmonic mean, which punishes
    the odd terrible move.
    """
    if not cps:
        return {}
    wps = [win_percent(initial_cp)] + [win_percent(c) for c in cps]
    size = min(_MAX_WINDOW, max(_MIN_WINDOW, len(cps) // 10))
    windows = [wps[:size]] * max(0, min(size, len(wps)) - 2) + [wps[i:i + size] for i in range(max(1, len(wps) - size + 1))]
    weights = [min(_MAX_WEIGHT, max(_MIN_WEIGHT, statistics.pstdev(w))) for w in windows]
    per_side = {True: [], False: []}
    for i, (weight, (before, after)) in enumerate(zip(weights, zip(wps, wps[1:]))):
        white_moved = (i % 2 == 0) == first_white
        accuracy = move_accuracy(before, after) if white_moved else move_accuracy(100 - before, 100 - after)
        per_side[white_moved].append((accuracy, weight))
    result = {}
    for white, pairs in per_side.items():
        if pairs:
            weighted = sum(a * w for a, w in pairs) / sum(w for _, w in pairs)
            harmonic = len(pairs) / sum(1 / max(a, 1e-9) for a, _ in pairs)
            result["white" if white else "black"] = (weighted + harmonic) / 2
    return result


def judgement(prev, cur, mover_white):
    """"inaccuracy", "mistake", "blunder" or None for a move, from the scores before and after it."""
    (prev_kind, prev_value), (cur_kind, cur_value) = prev, cur
    if prev_kind == "cp" and cur_kind == "cp":
        change = winning_chances(cur_value) - winning_chances(prev_value)
        lost = -change if mover_white else change
        for threshold, name in ((BLUNDER, "blunder"), (MISTAKE, "mistake"), (INACCURACY, "inaccuracy")):
            if lost >= threshold:
                return name
        return None
    # A mate is involved: work from the mover's own point of view.
    flip = -1 if not mover_white else 1
    prev_cp = flip * prev_value if prev_kind == "cp" else 0
    cur_cp = flip * cur_value if cur_kind == "cp" else 0
    prev_mate = flip * prev_value if prev_kind == "mate" else 0
    cur_mate = flip * cur_value if cur_kind == "mate" else 0
    if prev_kind == "cp" and cur_kind == "mate" and cur_mate < 0:
        # The mover has walked into a forced mate: the worse they were doing already, the milder the verdict.
        return "inaccuracy" if prev_cp < -999 else "mistake" if prev_cp < -700 else "blunder"
    threw_away_mate = prev_kind == "mate" and prev_mate > 0 and (cur_kind == "cp" or (cur_kind == "mate" and cur_mate < 0))
    if threw_away_mate:
        # The mover had a forced mate and let it go: the better they still are, the milder the verdict.
        return "inaccuracy" if cur_cp > 999 else "mistake" if cur_cp > 700 else "blunder"
    return None


@dataclass(frozen=True)
class Side:
    accuracy: float | None
    acc_opening: float | None
    acc_middle: float | None
    acc_end: float | None
    inaccuracies: int
    mistakes: int
    blunders: int
    acpl: int | None


@dataclass(frozen=True)
class Summary:
    white: Side
    black: Side
    eval_ply20: int | None  # centipawns, White's point of view, after 10 moves each (None for a shorter game)


def _phase_accuracy(colour, cps, indexes):
    """One side's accuracy over the moves at `indexes` (a run of consecutive plies), or None if it made none."""
    if not indexes:
        return None
    first = indexes[0]
    initial = cps[first - 1] if first else INITIAL_CP
    return game_accuracy(first % 2 == 0, [cps[i] for i in indexes], initial).get(colour)


def summarise(scores, middle, end, bests=None, played=None):
    """The figures for a whole game.

    `middle` and `end` are the plies at which the middlegame and endgame start (from divider.py; None if the
    game never got there). If `bests` and `played` are given, one move per ply, a move that is the engine's own
    best choice is never called an error however the evaluations wobble, as on Lichess.
    """
    n = len(scores)
    cps = [as_cp(s) for s in scores]
    counts = {"white": {"inaccuracy": 0, "mistake": 0, "blunder": 0}, "black": {"inaccuracy": 0, "mistake": 0, "blunder": 0}}
    losses = {"white": [], "black": []}
    for i in range(n):
        white = i % 2 == 0
        colour = "white" if white else "black"
        prev = scores[i - 1] if i else ("cp", INITIAL_CP)
        prev_cp = cps[i - 1] if i else INITIAL_CP
        verdict = judgement(prev, scores[i], white)
        if verdict and bests is not None and played is not None and played[i] == bests[i]:
            verdict = None
        if verdict:
            counts[colour][verdict] += 1
        losses[colour].append(max(0, (prev_cp - cps[i]) if white else (cps[i] - prev_cp)))

    overall = game_accuracy(True, cps)
    first_middle = middle if middle is not None else n + 1
    endgame_from = end if (end is not None and middle is not None) else n + 1
    phases = {
        "opening": [i for i in range(n) if i + 1 < first_middle],
        "middle": [i for i in range(n) if first_middle <= i + 1 < endgame_from],
        "end": [i for i in range(n) if endgame_from <= i + 1],
    }

    sides = {}
    for colour in ("white", "black"):
        sides[colour] = Side(
            accuracy=overall.get(colour),
            acc_opening=_phase_accuracy(colour, cps, phases["opening"]),
            acc_middle=_phase_accuracy(colour, cps, phases["middle"]),
            acc_end=_phase_accuracy(colour, cps, phases["end"]),
            inaccuracies=counts[colour]["inaccuracy"],
            mistakes=counts[colour]["mistake"],
            blunders=counts[colour]["blunder"],
            acpl=round(sum(losses[colour]) / len(losses[colour])) if losses[colour] else None,
        )
    return Summary(sides["white"], sides["black"], cps[_OPENING_SPAN - 1] if n >= _OPENING_SPAN else None)


# --- packing the evaluation curve for the database ------------------------------------------------------

_CP_LIMIT = 30000
_MATE_BASE = 31000  # a packed value beyond +-_CP_LIMIT is a mate: sign * (_MATE_BASE + moves)
_MATE_MOVES_LIMIT = 1500


def pack_evals(scores):
    """The scores as two bytes each. Centipawns beyond +-30000 are cut to that; a mate keeps its sign and distance."""
    values = []
    for kind, value in scores:
        if kind == "cp":
            values.append(max(-_CP_LIMIT, min(_CP_LIMIT, int(value))))
        else:
            if value == 0:
                raise ValueError("a mate score needs a direction: give it as 1 or -1 for a game that is already over")
            values.append((1 if value > 0 else -1) * (_MATE_BASE + min(abs(int(value)), _MATE_MOVES_LIMIT)))
    return struct.pack(f"<{len(values)}h", *values)


def unpack_evals(blob):
    if len(blob) % 2:
        raise ValueError("a packed evaluation curve has two bytes per position")
    scores = []
    for (value,) in struct.iter_unpack("<h", blob):
        if abs(value) > _CP_LIMIT:
            scores.append(("mate", (1 if value > 0 else -1) * (abs(value) - _MATE_BASE)))
        else:
            scores.append(("cp", value))
    return scores

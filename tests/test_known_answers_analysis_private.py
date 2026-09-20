"""Known-answer check of analysis.py and divider.py against Lichess's own computer analysis of real games.

For every game in the fixture we feed our code the evaluations Lichess itself produced, and compare what
comes out with the summary Lichess published for the same game: accuracy overall and by phase, average
centipawn loss, the counts of inaccuracies, mistakes and blunders, and where the phases start. Our method is a
reimplementation of Lichess's published one, so this is the test that it really is the same.

The games name real people, so the file lives in tests/fixtures_private/ (gitignored) and these tests skip
themselves when it is absent. It is a Lichess export, one JSON game per line, fetched with
`accuracy=true&division=true&evals=true`. Nothing real is named in this committed file.

What is known to differ, and why the tests allow for it:
- A game that ends in checkmate has no evaluation for its last position in the export, and Lichess's own
  accuracy skips a window there; ours has the full run. Two sides of 290 differ by more than rounding.
- Lichess does not judge a move that was the engine's own best choice. The export doesn't say which moves
  those were, so the counts here can only be too high, never too low.
"""

import json
from pathlib import Path

import pytest

import analysis
import divider

FIXTURE = Path(__file__).parent / "fixtures_private" / "lichess_analysed_games.ndjson"

pytestmark = pytest.mark.skipif(not FIXTURE.exists(), reason="private fixture not present")


@pytest.fixture(scope="module")
def games():
    rows = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [g for g in rows if g["variant"] == "standard" and "initialFen" not in g and "analysis" in g and "division" in g
            and all("analysis" in g["players"][c] for c in ("white", "black"))]


def scores_of(game):
    return [("cp", e["eval"]) if "eval" in e else ("mate", e["mate"]) for e in game["analysis"]]


def summary_of(game):
    return analysis.summarise(scores_of(game), game["division"].get("middle"), game["division"].get("end"))


def test_there_are_enough_games_to_mean_something(games):
    assert len(games) >= 100


def test_the_phases_start_where_lichess_says(games):
    chess = pytest.importorskip("chess")

    def position(b):
        return (b.occupied, b.kings, b.pawns, b.occupied_co[chess.WHITE], b.occupied_co[chess.BLACK])

    for g in games:
        board = chess.Board()
        positions = [position(board)]
        for move in g["moves"].split():
            board.push_san(move)
            positions.append(position(board))
        assert divider.divide(positions) == (g["division"].get("middle"), g["division"].get("end")), g["id"]


def test_average_centipawn_loss_and_opening_accuracy_are_exactly_lichess(games):
    for g in games:
        s = summary_of(g)
        for colour in ("white", "black"):
            theirs, ours = g["players"][colour]["analysis"], getattr(s, colour)
            assert ours.acpl == theirs["acpl"], (g["id"], colour)
            assert round(ours.acc_opening) == theirs["phases"]["opening"], (g["id"], colour)


def test_overall_and_phase_accuracy_match_lichess_to_the_nearest_point(games):
    checked = wrong = 0
    for g in games:
        s = summary_of(g)
        for colour in ("white", "black"):
            theirs, ours = g["players"][colour]["analysis"], getattr(s, colour)
            pairs = [(ours.accuracy, theirs["accuracy"]), (ours.acc_middle, theirs["phases"].get("middlegame")),
                     (ours.acc_end, theirs["phases"].get("endgame"))]
            for mine, lichess in pairs:
                if mine is None or lichess is None:
                    continue  # Lichess leaves out a phase in which a side made a single move at the very end
                checked += 1
                wrong += round(mine) != round(lichess)
    assert checked > 500 and wrong / checked <= 0.01, f"{wrong} of {checked} differ"


def test_we_never_miss_a_move_lichess_calls_an_error_and_add_only_a_few(games):
    fewer = more = 0
    for g in games:
        s = summary_of(g)
        for colour in ("white", "black"):
            theirs, ours = g["players"][colour]["analysis"], getattr(s, colour)
            for name, mine in (("inaccuracy", ours.inaccuracies), ("mistake", ours.mistakes), ("blunder", ours.blunders)):
                fewer += max(0, theirs[name] - mine)
                more += max(0, mine - theirs[name])
    assert fewer == 0, "a move Lichess called an error was not"
    assert more <= 40, f"{more} extra verdicts"

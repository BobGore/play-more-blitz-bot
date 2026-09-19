"""Known-answer check against one real month of Chess.com games.

The expected numbers come from independent throwaway scripts written before
stats.py existed, and were agreed in the design discussion. The raw archive holds
a real account and its opponents' usernames, so it lives in tests/fixtures_private/
(gitignored) and these tests skip themselves when it is absent. Two files are needed:

    known_month.json       a Chess.com monthly archive, as the site serves it
    known_month.meta.json  {"username": "<the account>", "start_rating": <closing
                           rating of the month before>}

Nothing real is named in this committed file.
"""

import json
from pathlib import Path

import pytest

import sources
import stats

PRIVATE = Path(__file__).parent / "fixtures_private"
FIXTURE = PRIVATE / "known_month.json"
META = PRIVATE / "known_month.meta.json"

pytestmark = pytest.mark.skipif(not (FIXTURE.exists() and META.exists()), reason="private fixture not present")


@pytest.fixture(scope="module")
def meta():
    return json.loads(META.read_text(encoding="utf-8-sig"))  # -sig: Notepad may add a BOM


@pytest.fixture(scope="module")
def games(meta):
    return sources.parse_chesscom_month(json.loads(FIXTURE.read_text(encoding="utf-8-sig")), meta["username"])


def counts(row):
    return (row.games, row.wins, row.draws, row.losses)


def test_results_block(games, meta):
    r = stats.summarise(games, meta["start_rating"])
    assert (r.games, r.wins, r.draws, r.losses) == (28, 12, 2, 14)
    assert r.score == pytest.approx(13 / 28)  # 46%
    assert (r.start_rating, r.end_rating, r.high, r.low) == (1466, 1448, 1487, 1417)
    assert r.days_played == 7
    assert r.longest_play_streak == 4
    assert r.best_win_streak == 3
    assert (r.most_games_in_day, str(r.busiest_day)) == (10, "2025-11-17")


def test_opening_tables(games):
    tables = stats.opening_tables(games)
    assert [(t.label, counts(t)) for t in tables["white"]] == [
        ("London System", (5, 2, 0, 3)),
        ("Indian Game", (2, 1, 1, 0)),
        ("All others", (7, 3, 0, 4)),
    ]
    assert [(t.label, counts(t)) for t in tables["black"]] == [
        ("Caro-Kann Defense", (7, 3, 1, 3)),
        ("All others", (7, 3, 0, 4)),
    ]


def test_records(games):
    rec = stats.records(games)
    # Ratings and results only: real opponents' usernames stay out of committed files.
    assert (rec.best_win.rating, rec.best_win.result) == (1563, "W")
    assert (rec.worst_loss.rating, rec.worst_loss.result) == (1386, "L")
    assert (rec.strongest_opponent.rating, rec.strongest_opponent.result) == (1564, "L")
    assert (rec.weakest_opponent.rating, rec.weakest_opponent.result) == (1327, "W")
    assert (rec.quickest_mate_won, rec.quickest_mate_lost) == (30, 39)


def test_splits(games, meta):
    s = stats.splits(games, meta["start_rating"])
    assert [(t.label, counts(t)) for t in s.by_opponent_rating] == [
        ("Higher", (7, 2, 1, 4)),
        ("Similar", (17, 6, 1, 10)),
        ("Lower", (4, 4, 0, 0)),
    ]
    assert [(t.label, counts(t)) for t in s.by_colour] == [("White", (14, 6, 1, 7)), ("Black", (14, 6, 1, 7))]
    assert [(t.label, counts(t)) for t in s.by_weekday] == [
        ("Mon", (11, 2, 1, 8)),
        ("Tue", (13, 8, 1, 4)),
        ("Wed", (1, 1, 0, 0)),
        ("Thu", (3, 1, 0, 2)),
    ]
    # Pinned from this code, not independently derived: the buckets (Night 21:00-05:59,
    # Morning 06-11, Afternoon 12-16, Evening 17-20, by the hour the game ended, UTC)
    # differ from the throwaway script's, so this guards against regressions only.
    assert [(t.label, counts(t)) for t in s.by_time_of_day] == [
        ("Afternoon", (17, 6, 1, 10)),
        ("Evening", (8, 4, 1, 3)),
        ("Night", (3, 2, 0, 1)),
    ]

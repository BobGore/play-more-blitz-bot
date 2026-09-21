"""analysis_reports.py: what the commands read from the analysis table."""

import pytest
from analysis_helpers import MONTH, NOW, analysed, register, side, spec

import analysis_queue as q
import analysis_reports as reports
import store


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")


# --- the latest games ---------------------------------------------------------------------------------------------

def test_a_players_analysed_games_come_newest_first_with_the_colour_they_had():
    register("alice_example")
    analysed(spec(1), spec(3, "rival_example", "alice_example"), spec(2))
    games = reports.player_games("lichess", "alice_example", limit=5)
    assert [(g["game_id"], g["side"]) for g in games] == [("00000003", "black"), ("00000002", "white"), ("00000001", "white")]


def test_only_analysed_games_are_included_and_other_sites_and_players_are_not():
    register("alice_example")
    register("carol_example", site="chess.com")
    analysed(spec(1))
    q.queue_games([spec(2)], NOW)                                              # pending
    q.queue_games([spec(3, "carol_example", "x_example", site="chess.com")], NOW)
    analysed_other = spec(4, "zed_example", "rival_example")
    assert reports.player_games("lichess", "alice_example", limit=10)[0]["game_id"] == "00000001"
    assert len(reports.player_games("lichess", "alice_example", limit=10)) == 1
    assert reports.player_games("chess.com", "alice_example") == []
    assert reports.player_games("lichess", "zed_example") == [] and analysed_other


def test_the_name_is_matched_whatever_its_capitals():
    register("Alice_Example")
    analysed(spec(1, "ALICE_EXAMPLE", "rival_example"))
    (game,) = reports.player_games("lichess", "alice_example")
    assert game["side"] == "white"
    register("Bob_Example")
    analysed(spec(2, "rival_example", "BOB_EXAMPLE"))
    assert reports.player_games("lichess", "bob_example")[0]["side"] == "black"


def test_limit_and_offset_page_through_the_games():
    register("alice_example")
    analysed(*[spec(n) for n in range(1, 6)])
    assert [g["game_id"] for g in reports.player_games("lichess", "alice_example", limit=2, offset=1)] == ["00000004", "00000003"]
    assert reports.player_games("lichess", "alice_example", limit=0) == [] and reports.player_games("lichess", "alice_example", limit=-1) == []
    assert reports.player_games("lichess", "alice_example", limit=3, offset=10) == []


def test_the_row_carries_everything_the_panel_needs():
    register("alice_example")
    analysed(spec(1, white_rating_change=8, opening_site="Sicilian-Defense", eco_site="B20"))
    (g,) = reports.player_games("lichess", "alice_example")
    assert (g["engine"], g["nodes"], g["white_accuracy"], g["black_blunders"], g["white_rating_change"], g["opening_site"]) == \
        ("Stockfish 19", 200_000, 88.4, 2, 8, "Sicilian-Defense")


# --- games waiting ---------------------------------------------------------------------------------------------------

def test_waiting_counts_queued_and_with_the_worker_but_not_finished_or_skipped(monkeypatch):
    register("alice_example")
    q.queue_games([spec(n) for n in range(1, 5)], NOW)
    q.claim("desk", 1, NOW)                      # one claimed, three pending
    assert reports.waiting_count("lichess", "alice_example") == 4
    q.release("desk", "lichess", "00000004", skip_reason=q.TOO_SHORT)
    assert reports.waiting_count("lichess", "alice_example") == 3
    assert reports.waiting_count("lichess", "rival_example") == 3          # a game has two sides
    assert reports.waiting_count("chess.com", "alice_example") == 0


# --- a month's summary ---------------------------------------------------------------------------------------------------

def test_a_month_with_nothing_in_the_queue_is_empty():
    s = reports.month_summary("lichess", "alice_example", MONTH)
    assert (s.total, s.analysed, s.waiting, s.accuracy, s.acpl, s.blunders) == (0, 0, 0, None, None, None)


def test_the_averages_are_of_the_players_own_side_across_the_analysed_games():
    register("alice_example")
    analysed(spec(1), spec(2, "rival_example", "alice_example"),
             white=side(accuracy=90.0, acc_opening=100.0, acc_middle=80.0, acc_end=70.0, inaccuracies=4, mistakes=2, blunders=0, acpl=40),
             black=side(accuracy=50.0, acc_opening=60.0, acc_middle=40.0, acc_end=None, inaccuracies=8, mistakes=4, blunders=2, acpl=100))
    s = reports.month_summary("lichess", "alice_example", MONTH)
    # game 1 as White (90, 100, 80, 70 ...), game 2 as Black (50, 60, 40, none ...)
    assert (s.total, s.analysed) == (2, 2)
    assert s.accuracy == pytest.approx(70.0) and s.opening == pytest.approx(80.0) and s.middlegame == pytest.approx(60.0)
    assert s.endgame == pytest.approx(70.0)                       # only one game reached the endgame for this player
    assert s.acpl == pytest.approx(70.0) and s.inaccuracies == pytest.approx(6.0) and s.mistakes == pytest.approx(3.0) and s.blunders == pytest.approx(1.0)


def test_what_became_of_every_game_is_counted(monkeypatch):
    import settings
    monkeypatch.setattr(settings, "ANALYSIS_FULL_PRIORITY_GAMES", 4)
    monkeypatch.setattr(settings, "ANALYSIS_MAX_GAMES", 5)
    register("alice_example")
    q.queue_games([spec(n) for n in range(1, 8)], NOW)         # 1-4 normal, 5 low, 6-7 over the limit
    claimed = q.claim("desk", 3, NOW)                             # the three newest normal ones: 4, 3, 2
    q.release("desk", "lichess", claimed[0]["game_id"], skip_reason=q.NOT_STANDARD_START)
    q.release("desk", "lichess", claimed[1]["game_id"], error="boom")
    s = reports.month_summary("lichess", "alice_example", MONTH)
    assert (s.total, s.analysed, s.over_limit, s.skipped, s.failed) == (7, 0, 2, 1, 0)
    assert s.waiting == 4 and s.accuracy is None                # 1, 5, and the released-for-retry 3, plus the still-claimed 2


def test_other_months_players_and_sites_are_left_out():
    register("alice_example")
    analysed(spec(1), spec(2, month="2026-08"), spec(3, "zed_example", "rival_example"))
    s = reports.month_summary("lichess", "alice_example", MONTH)
    assert s.total == 1 and s.analysed == 1
    assert reports.month_summary("chess.com", "alice_example", MONTH).total == 0


def test_a_game_between_two_members_counts_for_each_from_their_own_side():
    register("alice_example", "bob_example")
    analysed(spec(1, "alice_example", "bob_example"), white=side(accuracy=90.0), black=side(accuracy=40.0))
    assert reports.month_summary("lichess", "alice_example", MONTH).accuracy == pytest.approx(90.0)
    assert reports.month_summary("lichess", "bob_example", MONTH).accuracy == pytest.approx(40.0)

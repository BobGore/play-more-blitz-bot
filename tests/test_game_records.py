"""game_records.py: from one player's view of a game to a queue record with both sides."""

from datetime import datetime, timedelta, timezone

import pytest
from helpers import game

import game_records as gr


# --- the game's id --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["https://lichess.org/abcd1234", "http://lichess.org/abcd1234", "https://www.lichess.org/abcd1234",
                                 "https://lichess.org/abcd1234/black", "https://lichess.org/abcd1234#37", "https://lichess.org/abcd1234?a=1"])
def test_a_lichess_game_id_is_the_eight_characters_after_the_domain(url):
    assert gr.game_id("lichess", url) == "abcd1234"


@pytest.mark.parametrize("url", ["https://lichess.org/abcd123", "https://lichess.org/abcd12345", "https://lichess.org/abcd1234x/black",
                                 "https://lichess.org/@/someone", "https://lichess.org/", "https://example.test/abcd1234",
                                 "https://lichess.org.evil.test/abcd1234", "https://lichess.org/abcd-234", "", None])
def test_anything_else_is_not_a_lichess_game(url):
    assert gr.game_id("lichess", url) is None


@pytest.mark.parametrize("url, expected", [
    ("https://www.chess.com/game/live/123456789", "live/123456789"),
    ("https://chess.com/game/live/123456789", "live/123456789"),
    ("https://www.chess.com/game/daily/42", "daily/42"),
    ("https://www.chess.com/game/live/123456789?tab=analysis", "live/123456789"),
])
def test_a_chesscom_game_id_keeps_its_kind_because_live_and_daily_are_numbered_separately(url, expected):
    assert gr.game_id("chess.com", url) == expected


@pytest.mark.parametrize("url", ["https://www.chess.com/game/123", "https://www.chess.com/game/live/abc", "https://www.chess.com/game/live/",
                                 "https://www.chess.com/analysis/game/live/123", "https://www.chess.com.evil.test/game/live/1",
                                 "https://lichess.org/abcd1234", "", None])
def test_anything_else_is_not_a_chesscom_game(url):
    assert gr.game_id("chess.com", url) is None


def test_an_unknown_site_has_no_game_ids():
    assert gr.game_id("other.example", "https://lichess.org/abcd1234") is None


# --- the record ------------------------------------------------------------------------------------------------------

URL = "https://lichess.org/abcd1234"
WHEN = datetime(2026, 9, 14, 18, 30, tzinfo=timezone.utc)


def rec(result="W", colour="white", **over):
    g = game(result, colour=colour, when=WHEN, rating_before=1500, rating_after=1508, opponent="Rival", opponent_rating=1530, url=URL, **over)
    return gr.record("lichess", "alice_example", g)


def test_a_win_as_white_puts_the_player_on_the_white_side():
    r = rec("W", "white")
    assert (r["white_username"], r["black_username"], r["result"]) == ("alice_example", "Rival", "white")
    assert (r["white_rating"], r["black_rating"], r["white_rating_change"], r["black_rating_change"]) == (1500, 1530, 8, None)


def test_a_win_as_black_puts_the_player_on_the_black_side_and_black_wins():
    r = rec("W", "black")
    assert (r["white_username"], r["black_username"], r["result"]) == ("Rival", "alice_example", "black")
    assert (r["white_rating"], r["black_rating"], r["white_rating_change"], r["black_rating_change"]) == (1530, 1500, None, 8)


@pytest.mark.parametrize("result, colour, winner", [("L", "white", "black"), ("L", "black", "white"), ("D", "white", "draw"), ("D", "black", "draw")])
def test_losses_and_draws(result, colour, winner):
    assert rec(result, colour)["result"] == winner


def test_a_first_chesscom_game_has_no_known_starting_rating_so_no_change():
    g = game("W", when=WHEN, rating_before=None, rating_after=1508, opponent="Rival", opponent_rating=None, url=URL)
    r = gr.record("lichess", "alice_example", g)
    assert (r["white_rating"], r["white_rating_change"], r["black_rating"]) == (None, None, None)


def test_everything_else_the_refresher_knew_comes_along():
    r = rec("W", "white", ending="checkmated", opening="Sicilian-Defense")
    assert (r["site"], r["game_id"], r["month"], r["ending"], r["opening_site"], r["time_control"]) == \
        ("lichess", "abcd1234", "2026-09", "checkmated", "Sicilian-Defense", "300+5")
    assert r["ended_at"] == int(WHEN.timestamp()) and r["eco_site"] is None


def test_the_month_is_the_utc_month_whatever_zone_the_time_arrives_in():
    late = datetime(2026, 10, 1, 0, 30, tzinfo=timezone(timedelta(hours=1)))   # 23:30 UTC on 30 September
    g = game("W", when=late, url=URL)
    r = gr.record("lichess", "alice_example", g)
    assert r["month"] == "2026-09" and r["ended_at"] == int(late.timestamp())
    edge = game("W", when=datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc), url=URL)
    assert gr.record("lichess", "alice_example", edge)["month"] == "2026-10"


def test_a_game_without_a_recognisable_url_has_no_record():
    assert gr.record("lichess", "alice_example", game("W", url="https://example.test/game/1")) is None


def test_records_reports_how_many_games_could_not_be_used():
    games = [game("W", url=URL), game("L", url="nonsense"), game("D", url="https://lichess.org/zzzz9999")]
    made, unusable = gr.records("lichess", "alice_example", games)
    assert [m["game_id"] for m in made] == ["abcd1234", "zzzz9999"] and unusable == 1
    assert gr.records("lichess", "alice_example", []) == ([], 0)

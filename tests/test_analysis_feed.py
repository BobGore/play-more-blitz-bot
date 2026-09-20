"""Feeding the analysis queue from the refresher and the month-end close."""

import asyncio
import logging
from datetime import datetime, timezone

import pytest
from helpers import at, game

import analysis_feed
import analysis_queue
import monthend
import refresh
import settings
import sources
import store

MONTH = "2026-09"
OWNER = 1001
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
CLOSE_NOW = datetime(2026, 10, 1, 0, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(settings, "ANALYSIS_ENABLED", True)
    monkeypatch.setattr(settings, "ANALYSIS_FULL_PRIORITY_GAMES", 500)
    monkeypatch.setattr(settings, "ANALYSIS_MAX_GAMES", 1000)
    refresh._locks.clear()
    monthend.last_failures.clear()


@pytest.fixture
def site(monkeypatch):
    state = {"games": {}, "fail": {}}

    async def fake(session, site_name, username, month, *, after=None, limit=None):
        if username in state["fail"]:
            raise state["fail"][username]
        games = sorted(state["games"].get(username, []), key=lambda g: g.ended_at)
        return sources.only_after(games, after)

    monkeypatch.setattr(sources, "month_games", fake)
    return state


def lichess_game(n, result="W", day=2, **over):
    return game(result, when=at(day, 8 + n % 12), url=f"https://lichess.org/{n:08d}", rating_after=1500 + n, **over)


def register(name="alice_example", site_name="lichess", month=MONTH):
    store.add_player(site_name, name, OWNER, month, 1500)
    return store.get_player(site_name, name)


def do_refresh(player, now=NOW):
    return asyncio.run(refresh.refresh_player(None, player, MONTH, now=now))


def queued():
    with store.transaction() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM game_analysis ORDER BY ended_at, game_id")]


# --- the switch --------------------------------------------------------------------------------------------------------

def test_with_analysis_off_nothing_is_queued_and_nothing_is_written(site, monkeypatch):
    monkeypatch.setattr(settings, "ANALYSIS_ENABLED", False)
    site["games"]["alice_example"] = [lichess_game(1)]
    assert do_refresh(register()) == refresh.UPDATED
    with store.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM game_analysis").fetchone()[0] == 0


def test_feeding_no_games_does_nothing():
    assert asyncio.run(analysis_feed.feed("lichess", "alice_example", [], NOW)) is None


# --- the refresher -----------------------------------------------------------------------------------------------------

def test_a_players_first_refresh_queues_the_whole_month_so_far(site):
    site["games"]["alice_example"] = [lichess_game(n, day=n) for n in range(1, 6)]
    do_refresh(register())
    rows = queued()
    assert [r["game_id"] for r in rows] == [f"{n:08d}" for n in range(1, 6)]
    assert {(r["status"], r["priority"], r["month"], r["site"]) for r in rows} == {("pending", 0, MONTH, "lichess")}
    assert {r["white_username"] for r in rows} == {"alice_example"} and {r["black_username"] for r in rows} == {"Rival"}
    assert {r["queued_at"] for r in rows} == {int(NOW.timestamp())}


def test_later_refreshes_queue_only_the_new_games_and_never_twice(site):
    player = register()
    site["games"]["alice_example"] = [lichess_game(1, day=2)]
    do_refresh(player)
    site["games"]["alice_example"].append(lichess_game(2, day=3))
    do_refresh(player, now=datetime(2026, 9, 20, 12, 30, tzinfo=timezone.utc))
    do_refresh(player, now=datetime(2026, 9, 20, 13, 0, tzinfo=timezone.utc))
    assert [r["game_id"] for r in queued()] == ["00000001", "00000002"]


def test_the_totals_and_the_queue_agree_on_the_games(site):
    site["games"]["alice_example"] = [lichess_game(1, "W", 2), lichess_game(2, "L", 3), lichess_game(3, "D", 4, colour="black")]
    do_refresh(register())
    assert store.month_row("lichess", "alice_example", MONTH)["games"] == 3
    # a win as white, a loss as white (so Black won), a draw
    assert [r["result"] for r in queued()] == ["white", "black", "draw"]


def test_a_players_own_result_becomes_the_winning_colour(site):
    site["games"]["alice_example"] = [lichess_game(1, "W", 2, colour="black"), lichess_game(2, "L", 3, colour="black")]
    do_refresh(register())
    assert [r["result"] for r in queued()] == ["black", "white"]


def test_a_players_games_are_numbered_through_the_limits(site, monkeypatch):
    monkeypatch.setattr(settings, "ANALYSIS_FULL_PRIORITY_GAMES", 2)
    monkeypatch.setattr(settings, "ANALYSIS_MAX_GAMES", 3)
    site["games"]["alice_example"] = [lichess_game(n, day=n) for n in range(1, 6)]
    do_refresh(register())
    assert [(r["status"], r["priority"]) for r in queued()] == [("pending", 0), ("pending", 0), ("pending", 1), ("skipped", 1), ("skipped", 1)]


def test_games_that_cannot_be_identified_are_counted_but_not_queued_and_the_log_says_so(site, caplog):
    bad = game("W", when=at(2, 9), url="https://example.test/game/1")
    site["games"]["alice_example"] = [bad, lichess_game(2, day=3)]
    with caplog.at_level(logging.WARNING, logger="playmoreblitz.analysis"):
        assert do_refresh(register()) == refresh.UPDATED
    assert store.month_row("lichess", "alice_example", MONTH)["games"] == 2
    assert [r["game_id"] for r in queued()] == ["00000002"]
    assert "no recognisable game id" in caplog.text


def test_a_failure_in_the_queue_never_spoils_the_totals(site, monkeypatch, caplog):
    def broken(records, now):
        raise RuntimeError("the disk is full")
    monkeypatch.setattr(analysis_queue, "queue_games", broken)
    site["games"]["alice_example"] = [lichess_game(1), lichess_game(2, day=3)]
    with caplog.at_level(logging.ERROR, logger="playmoreblitz.analysis"):
        assert do_refresh(register()) == refresh.UPDATED
    row = store.month_row("lichess", "alice_example", MONTH)
    assert row["games"] == 2 and row["refresh_error"] is None and row["last_game_at"] is not None
    assert "could not queue" in caplog.text


def test_a_refresh_that_lost_the_race_queues_nothing(site, monkeypatch):
    monkeypatch.setattr(store, "apply_refresh", lambda *a, **k: False)
    site["games"]["alice_example"] = [lichess_game(1)]
    assert do_refresh(register()) == refresh.SKIPPED
    assert queued() == []


def test_a_failed_fetch_queues_nothing(site):
    site["fail"]["alice_example"] = sources.SourceError("the site is down")
    assert do_refresh(register()) == refresh.FAILED
    assert queued() == []


def test_chesscom_games_are_queued_with_their_kind_in_the_id(site):
    site["games"]["carol_example"] = [game("W", when=at(2, 9), url="https://www.chess.com/game/live/987654321", colour="black")]
    do_refresh(register("carol_example", "chess.com"))
    (row,) = queued()
    assert (row["site"], row["game_id"], row["black_username"], row["white_username"], row["result"]) == \
        ("chess.com", "live/987654321", "carol_example", "Rival", "black")


def test_two_members_playing_each_other_share_one_row(site):
    """Alice's refresh sees the game as white and Bob's sees the same game as black: it is queued once."""
    same_url = "https://lichess.org/abcd1234"
    site["games"]["alice_example"] = [game("W", when=at(2, 9), url=same_url, colour="white", opponent="bob_example")]
    site["games"]["bob_example"] = [game("L", when=at(2, 9), url=same_url, colour="black", opponent="alice_example")]
    do_refresh(register("alice_example"))
    do_refresh(register("bob_example"))
    (row,) = queued()
    assert (row["white_username"], row["black_username"], row["result"]) == ("alice_example", "bob_example", "white")


# --- the month-end close -----------------------------------------------------------------------------------------------

def september(n=4):
    return [lichess_game(i, day=i * 2) for i in range(1, n + 1)]


def close(month=MONTH):
    return asyncio.run(monthend.close_month(None, month, now=CLOSE_NOW))


def test_closing_a_month_queues_every_game_of_it_including_ones_the_refresher_missed(site):
    player = register()
    site["games"]["alice_example"] = september(2)
    do_refresh(player)                                       # the refresher saw only two games
    site["games"]["alice_example"] = september(4)            # two more were played before the month ended
    before = queued()
    assert close().ok
    after = queued()
    assert [r["game_id"] for r in after] == [f"{n:08d}" for n in range(1, 5)]
    assert after[:2] == before                               # the two already there are untouched
    assert {r["queued_at"] for r in after[2:]} == {int(CLOSE_NOW.timestamp())}


def test_the_close_is_all_or_nothing_for_the_queue_too(site):
    register("alice_example")
    register("bob_example")
    site["games"]["alice_example"] = september(2)
    site["fail"]["bob_example"] = sources.SourceError("the site is down")
    assert not close().ok
    assert queued() == []


def test_a_failure_in_the_queue_does_not_undo_a_close(site, monkeypatch):
    def broken(records, now):
        raise RuntimeError("nope")
    monkeypatch.setattr(analysis_queue, "queue_games", broken)
    register()
    site["games"]["alice_example"] = september(2)
    result = close()
    assert result.ok and result.closed == 1
    assert store.month_row("lichess", "alice_example", MONTH)["closed_at"] is not None


def test_with_analysis_off_a_close_queues_nothing(site, monkeypatch):
    monkeypatch.setattr(settings, "ANALYSIS_ENABLED", False)
    register()
    site["games"]["alice_example"] = september(2)
    assert close().ok
    with store.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM game_analysis").fetchone()[0] == 0

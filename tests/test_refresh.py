"""The refresher: incremental counting, failure handling and never double counting."""

import asyncio
from datetime import datetime, timezone

import pytest
from helpers import at, game

import refresh
import sources
import store

MONTH = "2026-09"
OWNER = 1001
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 20, 12, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    refresh._locks.clear()


@pytest.fixture
def site(monkeypatch):
    """A fake site: state["games"][username] is what it would return; failures can be queued."""
    state = {"games": {}, "calls": [], "raise": None, "during_fetch": None}

    async def fake_month_games(session, site_name, username, month, *, after=None, limit=None):
        state["calls"].append((username, after))
        if state["during_fetch"]:
            state["during_fetch"]()
        if state["raise"]:
            raise state["raise"]
        games = sorted(state["games"].get(username, []), key=lambda g: g.ended_at)
        return sources.only_after(games, after)

    monkeypatch.setattr(sources, "month_games", fake_month_games)
    return state


def five_games():
    """W, L, D, W, W with rising and falling ratings, across three days."""
    return [
        game("W", when=at(2, 10), rating_after=1510),
        game("L", when=at(2, 11), rating_after=1495),
        game("D", when=at(3, 9), rating_after=1495),
        game("W", when=at(4, 20), rating_after=1512),
        game("W", when=at(4, 21), rating_after=1528),
    ]


def register(username="alice", site_name="chess.com", start=1500):
    store.add_player(site_name, username, OWNER, MONTH, start)
    return store.get_player(site_name, username)


def do(player, now=NOW):
    return asyncio.run(refresh.refresh_player(None, player, MONTH, now=now))


def row(username="alice", site_name="chess.com"):
    return store.month_row(site_name, username, MONTH)


def totals(r):
    return (r["games"], r["wins"], r["draws"], r["losses"], r["end_rating"], r["last_game_at"])


# --- the core promise ------------------------------------------------------


def test_a_first_refresh_counts_the_whole_month(site):
    site["games"]["alice"] = five_games()
    assert do(register()) == refresh.UPDATED
    r = row()
    assert totals(r) == (5, 3, 1, 1, 1528, at(4, 21).isoformat())
    assert (r["start_rating"], r["refreshed_at"], r["refresh_error"]) == (1500, NOW.isoformat(), None)
    assert site["calls"] == [("alice", None)]  # no watermark yet: the whole month


def test_refreshing_in_pieces_gives_the_same_totals_as_fetching_everything_at_once(site):
    everything = five_games()

    site["games"]["whole"] = everything
    do(register("whole"))

    site["games"]["pieces"] = everything[:2]
    pieces = register("pieces")
    do(pieces)
    site["games"]["pieces"] = everything[:3]  # a game arrives
    do(pieces, LATER)
    site["games"]["pieces"] = everything  # two more
    do(pieces, LATER)

    assert totals(row("pieces")) == totals(row("whole")) == (5, 3, 1, 1, 1528, at(4, 21).isoformat())


def test_a_later_refresh_asks_only_for_games_after_the_last_one_counted(site):
    site["games"]["alice"] = five_games()[:3]
    player = register()
    do(player)
    site["games"]["alice"] = five_games()
    do(player, LATER)
    assert site["calls"][1] == ("alice", at(3, 9))  # the third game's end time, not None


def test_nothing_new_changes_no_totals_but_shows_the_table_is_current(site):
    site["games"]["alice"] = five_games()
    player = register()
    do(player)
    before = totals(row())
    assert do(player, LATER) == refresh.UNCHANGED
    assert totals(row()) == before
    assert row()["refreshed_at"] == LATER.isoformat()  # freshness moves on


def test_a_player_with_no_games_yet_stays_at_zero_with_their_start_rating(site):
    assert do(register()) == refresh.UNCHANGED
    r = row()
    assert totals(r) == (0, 0, 0, 0, 1500, None) and r["refreshed_at"] == NOW.isoformat()


# --- failures ---------------------------------------------------------------


def test_a_site_failure_is_recorded_and_the_old_totals_are_kept(site):
    site["games"]["alice"] = five_games()[:2]
    player = register()
    do(player)
    before = totals(row())

    site["raise"] = sources.SourceError("couldn't reach chess.com (TimeoutError)")
    assert do(player, LATER) == refresh.FAILED
    r = row()
    assert totals(r) == before
    assert r["refresh_error"] == "couldn't reach chess.com (TimeoutError)"
    assert r["refreshed_at"] == NOW.isoformat()  # the last SUCCESS, not the failed attempt


def test_the_next_success_clears_the_error_and_catches_up(site):
    site["games"]["alice"] = five_games()
    player = register()
    site["raise"] = sources.NoSuchUser("no chess.com account 'alice'")
    do(player)
    assert row()["refresh_error"] is not None

    site["raise"] = None
    assert do(player, LATER) == refresh.UPDATED
    assert row()["refresh_error"] is None and row()["games"] == 5


def test_an_unexpected_crash_is_recorded_and_does_not_escape(site):
    site["raise"] = ValueError("a bug")
    assert do(register()) == refresh.FAILED
    assert row()["refresh_error"] == "unexpected error, see the log"


def test_a_player_with_no_row_for_the_month_is_skipped_not_crashed(site):
    store.add_player("chess.com", "alice", OWNER, "2026-08", 1500)  # only an August row
    player = store.get_player("chess.com", "alice")
    assert do(player) == refresh.SKIPPED
    assert site["calls"] == []  # and no site was contacted for it


def test_a_closed_month_is_skipped(site):
    site["games"]["alice"] = five_games()
    player = register()
    with store._transaction() as conn:
        conn.execute("UPDATE monthly_results SET closed_at = '2026-10-01T09:00:00+00:00'")
    assert do(player) == refresh.SKIPPED
    assert row()["games"] == 0 and site["calls"] == []


# --- never counting a game twice ---------------------------------------------


def test_if_another_refresh_lands_first_this_one_backs_off_instead_of_double_counting(site):
    site["games"]["alice"] = five_games()
    player = register()

    def competitor():  # another process finishes a refresh while ours is fetching
        store.apply_refresh("chess.com", "alice", MONTH, expected_watermark=None, games=5, wins=3, draws=1, losses=1,
                            end_rating=1528, last_game_at=at(4, 21).isoformat(), now=NOW.isoformat())

    site["during_fetch"] = competitor
    assert do(player) == refresh.SKIPPED
    assert row()["games"] == 5  # counted once


def test_two_simultaneous_refreshes_of_one_player_count_each_game_once(site):
    site["games"]["alice"] = five_games()
    player = register()

    async def both():
        return await asyncio.gather(
            refresh.refresh_player(None, player, MONTH, now=NOW), refresh.refresh_player(None, player, MONTH, now=NOW)
        )

    outcomes = asyncio.run(both())
    assert sorted(outcomes) == [refresh.UNCHANGED, refresh.UPDATED]  # the second saw the first's watermark
    assert row()["games"] == 5
    assert site["calls"][1][1] == at(4, 21)


# --- refresh_all and refresh_one --------------------------------------------


def test_refresh_all_goes_through_everyone_and_a_failure_does_not_stop_the_rest(site):
    site["games"]["alice"] = five_games()
    site["games"]["carol"] = five_games()[:1]
    register("alice")
    register("bob")
    register("carol")

    real = sources.month_games

    async def bob_is_down(session, site_name, username, month, **kw):
        if username == "bob":
            raise sources.SourceError("couldn't reach chess.com ()")
        return await real(session, site_name, username, month, **kw)

    sources.month_games = bob_is_down
    try:
        outcomes = asyncio.run(refresh.refresh_all(MONTH, now=NOW))
    finally:
        sources.month_games = real

    assert outcomes == {"updated": 2, "failed": 1}
    assert row("alice")["games"] == 5 and row("carol")["games"] == 1 and row("bob")["games"] == 0
    assert row("bob")["refresh_error"] is not None


def test_refresh_all_visits_players_one_after_another_in_order(site):
    for name in ("alice", "bob", "carol"):
        register(name)
    asyncio.run(refresh.refresh_all(MONTH, now=NOW))
    assert [c[0] for c in site["calls"]] == ["alice", "bob", "carol"]


def test_refresh_all_skips_removed_players(site):
    register("alice")
    register("bob")
    store.remove_player("chess.com", "bob")
    asyncio.run(refresh.refresh_all(MONTH, now=NOW))
    assert [c[0] for c in site["calls"]] == ["alice"]


def test_refresh_all_with_nobody_registered_is_fine(site):
    assert asyncio.run(refresh.refresh_all(MONTH, now=NOW)) == {}


def test_refresh_one_refreshes_that_player(site):
    site["games"]["alice"] = five_games()
    register()
    assert asyncio.run(refresh.refresh_one("chess.com", "alice", MONTH)) == refresh.UPDATED
    assert row()["games"] == 5


def test_refresh_one_ignores_unknown_and_removed_players(site):
    register()
    store.remove_player("chess.com", "alice")
    assert asyncio.run(refresh.refresh_one("chess.com", "alice", MONTH)) == refresh.SKIPPED
    assert asyncio.run(refresh.refresh_one("chess.com", "nobody", MONTH)) == refresh.SKIPPED
    assert site["calls"] == []

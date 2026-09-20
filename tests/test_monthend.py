"""Closing a month: all or nothing, from a full fetch, in order."""

import asyncio
from datetime import datetime, timezone

import pytest
from helpers import at, game

import monthend
import sources
import store

OWNER = 1001
NOW = datetime(2026, 10, 1, 0, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monthend.last_failures.clear()
    yield
    monthend.last_failures.clear()


@pytest.fixture
def site(monkeypatch):
    """Fake site: state["games"][username], what any full fetch returns; failures by name."""
    state = {"games": {}, "fail": {}, "calls": []}

    async def fake(session, site_name, username, month, *, after=None, limit=None):
        state["calls"].append((username, month, after))
        if username in state["fail"]:
            raise state["fail"][username]
        return sorted(state["games"].get(username, []), key=lambda g: g.ended_at)

    monkeypatch.setattr(sources, "month_games", fake)
    return state


def register(name, month="2026-09", start=1500, site_name="chess.com"):
    store.add_player(site_name, name, OWNER, month, start)


def close(month="2026-09"):
    return asyncio.run(monthend.close_month(None, month, now=NOW))


def row(name, month="2026-09", site_name="chess.com"):
    return store.month_row(site_name, name, month)


def september():
    return [
        game("W", when=at(2, 10), rating_after=1510),
        game("L", when=at(9, 11), rating_after=1495),
        game("D", when=at(20, 9), rating_after=1495),
        game("W", when=at(30, 22), rating_after=1512),
    ]


# --- a successful close ------------------------------------------------------


def test_closing_writes_the_final_figures_from_the_whole_month(site):
    site["games"]["alice"] = september()
    register("alice")
    result = close()
    assert result.ok and result.closed == 1 and result.failures == ()
    r = row("alice")
    assert (r["games"], r["wins"], r["draws"], r["losses"]) == (4, 2, 1, 1)
    assert r["end_rating"] == 1512  # the rating after the LAST game of the month
    assert r["last_game_at"] == at(30, 22).isoformat()
    assert r["closed_at"] == NOW.isoformat()
    assert site["calls"] == [("alice", "2026-09", None)]  # a full fetch, not an incremental one


def test_the_next_months_row_starts_from_the_closing_rating(site):
    site["games"]["alice"] = september()
    register("alice")
    close()
    nxt = row("alice", "2026-10")
    assert (nxt["start_rating"], nxt["end_rating"], nxt["games"], nxt["closed_at"]) == (1512, 1512, 0, None)


def test_a_player_with_no_games_closes_at_their_start_rating(site):
    register("quiet", start=1433)
    result = close()
    assert result.ok
    r = row("quiet")
    assert (r["games"], r["end_rating"], r["last_game_at"]) == (0, 1433, None)
    assert row("quiet", "2026-10")["start_rating"] == 1433


def test_stale_running_totals_are_replaced_by_the_full_recount(site):
    site["games"]["alice"] = september()
    register("alice")
    store.apply_refresh("chess.com", "alice", "2026-09", expected_watermark=None, games=99, wins=99, draws=0, losses=0,
                        end_rating=9999, last_game_at=at(5).isoformat(), now=at(5).isoformat())
    close()
    assert totals(row("alice")) == (4, 2, 1, 1, 1512)


def totals(r):
    return (r["games"], r["wins"], r["draws"], r["losses"], r["end_rating"])


def test_sign_ups_are_applied_to_the_new_months_rows(site):
    register("alice")
    register("bob")
    store.join_100gob("chess.com", "alice", "2026-10")
    close()
    assert row("alice", "2026-10")["in_100gob"] == 1 and row("bob", "2026-10")["in_100gob"] == 0


def test_players_are_fetched_one_after_another_in_name_order(site):
    for name in ("carol", "alice", "bob"):
        register(name)
    close()
    assert [c[0] for c in site["calls"]] == ["alice", "bob", "carol"]


def test_a_removed_player_is_not_fetched_and_their_row_is_closed_as_it_stands(site):
    register("alice")
    register("gone")
    store.remove_player("chess.com", "gone")
    close()
    assert [c[0] for c in site["calls"]] == ["alice"]
    assert row("gone")["closed_at"] == NOW.isoformat() and row("gone", "2026-10") is None


def test_both_sites_are_closed_together(site):
    site["games"]["alice"] = september()
    register("alice")
    register("alice_li", site_name="lichess")
    assert close().closed == 2
    assert row("alice_li", site_name="lichess")["closed_at"] is not None


# --- failure: nothing is written and the players are named -----------------


def test_if_any_fetch_fails_nothing_is_written_at_all(site):
    site["games"]["alice"] = september()
    register("alice")
    register("bob")
    site["fail"]["bob"] = sources.SourceError("couldn't reach chess.com (TimeoutError)")
    result = close()
    assert result.ok is False and result.closed == 0
    for name in ("alice", "bob"):  # not even alice, whose fetch worked
        r = row(name)
        assert r["closed_at"] is None and r["games"] == 0
        assert row(name, "2026-10") is None
    assert store.unclosed_months("2026-10") == ["2026-09"]


def test_the_failure_names_the_player_the_site_and_the_reason(site):
    register("alice")
    register("bob", site_name="lichess")
    site["fail"]["bob"] = sources.NoSuchUser("no lichess account 'bob'")
    result = close()
    assert result.failures == (monthend.Failure("lichess", "bob", "no lichess account 'bob'"),)
    assert monthend.last_failures["2026-09"] == result.failures


def test_every_failing_player_is_named_not_just_the_first(site):
    for name in ("alice", "bob", "carol"):
        register(name)
    site["fail"]["alice"] = sources.SourceError("a")
    site["fail"]["carol"] = sources.SourceError("c")
    assert [f.username for f in close().failures] == ["alice", "carol"]
    assert [c[0] for c in site["calls"]] == ["alice", "bob", "carol"]  # all were tried


def test_an_unexpected_crash_counts_as_a_failure_and_is_named(site):
    register("alice")
    site["fail"]["alice"] = ValueError("a bug")
    result = close()
    assert result.ok is False and result.failures[0].reason == "unexpected error, see the log"


def test_a_later_success_closes_the_month_and_forgets_the_failure(site):
    site["games"]["alice"] = september()
    register("alice")
    site["fail"]["alice"] = sources.SourceError("down")
    assert close().ok is False and "2026-09" in monthend.last_failures
    del site["fail"]["alice"]
    assert close().ok is True
    assert "2026-09" not in monthend.last_failures and row("alice")["closed_at"] is not None


def test_removing_the_failing_player_lets_the_month_close(site):
    register("alice")
    register("bob")
    site["fail"]["bob"] = sources.NoSuchUser("account closed")
    assert close().ok is False
    store.remove_player("chess.com", "bob")
    assert close().ok is True and row("alice")["closed_at"] is not None


# --- close_due_months --------------------------------------------------------


def due():
    return asyncio.run(monthend.close_due_months(now=NOW))


def test_nothing_is_closed_when_no_month_has_finished(site):
    register("alice", month="2026-10")  # the current month, as of NOW
    assert due() == [] and site["calls"] == []


def test_the_current_month_is_never_closed(site):
    register("alice", month="2026-10")
    register("bob", month="2026-09")
    results = due()
    assert [r.month for r in results] == ["2026-09"]
    assert row("alice", "2026-10")["closed_at"] is None


def test_several_missed_months_are_closed_oldest_first(site):
    register("alice", month="2026-08", start=1500)
    site["games"]["alice"] = [game("W", when=at(5, 10, month=8), rating_after=1520)]
    results = due()
    assert [(r.month, r.ok) for r in results] == [("2026-08", True), ("2026-09", True)]
    assert row("alice", "2026-09")["start_rating"] == 1520  # September builds on August's closing rating
    assert row("alice", "2026-09")["closed_at"] is not None and row("alice", "2026-10")["start_rating"] == 1520


def test_it_stops_at_the_first_month_that_cannot_be_closed(site):
    register("alice", month="2026-08")
    site["fail"]["alice"] = sources.SourceError("down")
    results = due()
    assert [(r.month, r.ok) for r in results] == [("2026-08", False)]
    assert row("alice", "2026-09") is None  # September was not started on a foundation that isn't there
    assert len(site["calls"]) == 1


def test_a_closed_month_is_not_closed_again(site):
    register("alice")
    assert [r.ok for r in due()] == [True]
    assert due() == []
    assert len(site["calls"]) == 1

import asyncio

import pytest
from helpers import at, game

import gamecache
import sources


@pytest.fixture(autouse=True)
def fresh():
    gamecache.clear()
    yield
    gamecache.clear()


@pytest.fixture
def site(monkeypatch):
    """A fake site holding state["games"][username], recording each call's `after`."""
    state = {"games": {}, "calls": [], "raise": None}

    async def fake(session, site_name, username, month, *, after=None, limit=None):
        state["calls"].append((username, month, after))
        if state["raise"]:
            raise state["raise"]
        await asyncio.sleep(0)  # a real fetch yields, which is what lets calls overlap
        return sources.only_after(sorted(state["games"].get(username, []), key=lambda g: g.ended_at), after)

    monkeypatch.setattr(sources, "month_games", fake)
    return state


def five():
    return [game("W", when=at(2, h)) for h in (9, 10, 11, 12, 13)]


def get(username="alice", month="2026-09", site_name="chess.com"):
    return asyncio.run(gamecache.month_games(None, site_name, username, month))


def test_the_first_request_fetches_the_whole_month(site):
    site["games"]["alice"] = five()
    assert len(get()) == 5
    assert site["calls"] == [("alice", "2026-09", None)]


def test_a_later_request_fetches_only_what_is_new_and_returns_everything(site):
    site["games"]["alice"] = five()[:3]
    get()
    site["games"]["alice"] = five()
    games = get()
    assert len(games) == 5
    assert site["calls"][1] == ("alice", "2026-09", at(2, 11))  # after the third game
    assert [g.ended_at for g in games] == sorted(g.ended_at for g in games)


def test_nothing_new_returns_the_same_games_without_duplicating_any(site):
    site["games"]["alice"] = five()
    get()
    assert len(get()) == 5 and len(get()) == 5


def test_a_player_with_no_games_is_fetched_whole_each_time_which_is_cheap(site):
    assert get() == [] and get() == []
    assert [c[2] for c in site["calls"]] == [None, None]


def test_players_months_and_sites_are_kept_separate(site):
    site["games"]["alice"] = five()
    site["games"]["bob"] = five()[:2]
    assert (len(get("alice")), len(get("bob")), len(get("alice", "2026-10")), len(get("alice", site_name="lichess"))) == (5, 2, 5, 5)


def test_the_username_case_does_not_matter(site):
    site["games"]["Alice"] = five()
    get("Alice")
    get("ALICE")
    assert site["calls"][1][2] is not None  # the second call found the cached games


def test_a_failed_fetch_leaves_the_cache_intact_and_the_error_reaches_the_caller(site):
    site["games"]["alice"] = five()[:2]
    get()
    site["raise"] = sources.SourceError("couldn't reach chess.com ()")
    with pytest.raises(sources.SourceError):
        get()
    site["raise"] = None
    site["games"]["alice"] = five()
    assert len(get()) == 5  # recovers: still 2 held, 3 added


def test_simultaneous_requests_for_one_player_never_add_a_game_twice(site):
    site["games"]["alice"] = five()

    async def both():
        return await asyncio.gather(gamecache.month_games(None, "chess.com", "alice", "2026-09"),
                                    gamecache.month_games(None, "chess.com", "alice", "2026-09"))

    first, second = asyncio.run(both())
    assert len(first) == len(second) == 5
    assert site["calls"][1][2] == at(2, 13)  # the second waited, then fetched only after the last game


def test_the_cache_holds_a_bounded_number_of_players_dropping_the_least_recently_used(site, monkeypatch):
    monkeypatch.setattr(gamecache, "MAX_PLAYERS", 3)
    for name in ("a1", "a2", "a3"):
        site["games"][name] = five()
        get(name)
    get("a1")  # a1 is now the most recently used, so a2 is the oldest
    site["games"]["a4"] = five()
    get("a4")
    assert len(gamecache._cache) == 3
    assert ("chess.com", "a2", "2026-09") not in gamecache._cache
    assert ("chess.com", "a1", "2026-09") in gamecache._cache
    assert ("chess.com", "a2", "2026-09") not in gamecache._locks  # and its lock is dropped too


def test_the_returned_list_can_be_changed_without_corrupting_the_cache(site):
    site["games"]["alice"] = five()
    get().clear()
    assert len(get()) == 5

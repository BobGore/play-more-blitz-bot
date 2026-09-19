"""Month helpers, username checks and the start-rating rules in sources.py."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from helpers import at, game

import sources


# --- month helpers ---------------------------------------------------------


@pytest.mark.parametrize("month,before", [("2026-09", "2026-08"), ("2026-01", "2025-12"), ("2026-12", "2026-11"), ("2000-01", "1999-12")])
def test_previous_month(month, before):
    assert sources.previous_month(month) == before


def test_current_month_is_the_utc_month():
    assert sources.current_month(datetime(2026, 9, 19, 14, 0, tzinfo=timezone.utc)) == "2026-09"


def test_current_month_converts_to_utc_first():
    # 01:00 on 1 September in UTC+2 is still 23:00 on 31 August in UTC.
    plus_two = timezone(timedelta(hours=2))
    assert sources.current_month(datetime(2026, 9, 1, 1, 0, tzinfo=plus_two)) == "2026-08"


def test_current_month_defaults_to_now():
    assert sources.current_month() == datetime.now(timezone.utc).strftime("%Y-%m")


# --- username checks -------------------------------------------------------


@pytest.mark.parametrize("name", ["ab", "Alice_99", "some-name", "A" * 30, "x_y-z"])
def test_valid_usernames_pass(name):
    sources.check_username(name)


@pytest.mark.parametrize("name", ["", "x", "a/b", "../x", "x?y=1", "a b", "name\n", "ünï", "x" * 31, "a.b", "@here"])
def test_invalid_usernames_are_refused(name):
    with pytest.raises(sources.SourceError, match="valid username"):
        sources.check_username(name)


def test_a_bad_username_is_refused_before_any_request_is_made():
    # session=None: if a request were attempted this would fail differently.
    for call in (
        sources.month_games(None, "chess.com", "../x", "2026-09"),
        sources.month_games(None, "lichess", "a/b", "2026-09"),
        sources.current_rating(None, "chess.com", "x?y"),
    ):
        with pytest.raises(sources.SourceError, match="valid username"):
            asyncio.run(call)


def test_lichess_export_limit_is_sent_as_max_and_only_when_asked():
    assert sources.lichess_export_params("2026-09", limit=1)["max"] == 1
    assert "max" not in sources.lichess_export_params("2026-09")


# --- start_rating ----------------------------------------------------------


@pytest.fixture
def fakes(monkeypatch):
    """Stand-ins for the two network functions, recording how they were called."""
    state = {"calls": [], "games": {}, "current": 1234}

    async def fake_month_games(session, site, username, month, *, after=None, limit=None):
        state["calls"].append(("month_games", site, month, limit))
        return state["games"].get(month, [])

    async def fake_current_rating(session, site, username):
        state["calls"].append(("current_rating", site))
        return state["current"]

    monkeypatch.setattr(sources, "month_games", fake_month_games)
    monkeypatch.setattr(sources, "current_rating", fake_current_rating)
    return state


def rating_of(site, month="2026-09"):
    return asyncio.run(sources.start_rating(None, site, "alice", month))


def test_chesscom_start_is_the_rating_after_last_months_last_game(fakes):
    fakes["games"]["2026-08"] = [game(rating_after=1500, when=at(3, month=8)), game(rating_after=1477, when=at(30, month=8))]
    assert rating_of("chess.com") == 1477
    assert fakes["calls"] == [("month_games", "chess.com", "2026-08", None)]  # last month only; no rating lookup


def test_chesscom_with_no_games_last_month_falls_back_to_the_current_rating(fakes):
    assert rating_of("chess.com") == 1234
    assert fakes["calls"][-1] == ("current_rating", "chess.com")


def test_chesscom_ignores_this_months_games_when_last_month_had_none(fakes):
    # Played this month but not last: the brief uses the rating at registration.
    fakes["games"]["2026-09"] = [game(rating_after=1600)]
    assert rating_of("chess.com") == 1234


def test_chesscom_january_looks_at_december_of_the_year_before(fakes):
    fakes["games"]["2025-12"] = [game(rating_after=1401)]
    assert rating_of("chess.com", month="2026-01") == 1401


def test_lichess_start_is_the_rating_before_the_months_first_game(fakes):
    fakes["games"]["2026-09"] = [game(rating_before=1450, rating_after=1460)]
    assert rating_of("lichess") == 1450
    assert fakes["calls"] == [("month_games", "lichess", "2026-09", 1)]  # asks for one game only


def test_lichess_with_no_games_this_month_falls_back_to_the_current_rating(fakes):
    assert rating_of("lichess") == 1234
    assert fakes["calls"][-1] == ("current_rating", "lichess")


def test_unknown_site_is_a_programming_error(fakes):
    with pytest.raises(ValueError):
        rating_of("example.org")

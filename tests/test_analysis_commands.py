"""!lastgame, !analysisQ, !queuemonth, and the analysis part of !mystats, run against a fake Discord context."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from analysis_helpers import MONTH, NOW, analysed, register, side, spec
from helpers import at, game

import analysis_feed
import analysis_queue as q
import analysis_reports
import bot as botmod
import settings
import sources
import store

OK, NO = "✅", "❌"
ALICE, BOB, ADMIN = 1001, 1002, min(botmod.ADMIN_USER_IDS)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(settings, "ANALYSIS_ENABLED", True)


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def make_ctx(author_id):
    return SimpleNamespace(author=SimpleNamespace(id=author_id), message=SimpleNamespace(add_reaction=AsyncMock()), send=AsyncMock(),
                           typing=lambda: Typing(), command=MagicMock())


def run(command, ctx, *args):
    asyncio.run(command.callback(ctx, *args))


def reactions(ctx):
    return [c.args[0] for c in ctx.message.add_reaction.await_args_list]


def said(ctx):
    return [c.args[0] for c in ctx.send.await_args_list]


def member(owner=ALICE, name="alice_example", site="lichess"):
    store.add_player(site, name, owner, MONTH, 1500)


# --- !lastgame ------------------------------------------------------------------------------------------------------------------

def test_lastgame_with_no_name_shows_the_callers_latest_analysed_game_as_a_panel():
    member()
    analysed(spec(1, white_rating_change=8), spec(2, "rival_example", "alice_example", black_rating_change=-6))
    ctx = make_ctx(ALICE)
    run(botmod.lastgame, ctx)
    (text,) = said(ctx)
    assert reactions(ctx) == []
    assert "`alice_example` · Lichess" in text and "<https://lichess.org/00000002>" in text
    assert "alice_example (B)" in text and "rival_example (W)" in text and "-6" in text          # game 2, where Alice had Black
    assert "the bot's own estimate" in text


def test_lastgame_by_name_and_site_works_for_any_registered_player():
    member(BOB, "bob_example")
    member(ALICE, "alice_example")
    analysed(spec(1, "bob_example", "x_example"))
    ctx = make_ctx(ALICE)
    run(botmod.lastgame, ctx, "bob_example", "lichess")
    assert "`bob_example`" in said(ctx)[0]


def test_lastgame_says_how_many_are_still_waiting_when_some_are():
    member()
    analysed(spec(1))
    q.queue_games([spec(2), spec(3)], NOW)
    ctx = make_ctx(ALICE)
    run(botmod.lastgame, ctx)
    assert "2 more of alice_example's games are waiting to be analysed." in said(ctx)[0]


def test_lastgame_when_nothing_is_analysed_yet_but_games_are_waiting():
    member()
    q.queue_games([spec(1), spec(2)], NOW)
    ctx = make_ctx(ALICE)
    run(botmod.lastgame, ctx)
    assert said(ctx) == ["None of alice_example's games have been analysed yet (2 waiting)."]


def test_lastgame_when_analysis_is_off_and_when_it_is_on_but_empty(monkeypatch):
    member()
    monkeypatch.setattr(settings, "ANALYSIS_ENABLED", False)
    ctx = make_ctx(ALICE)
    run(botmod.lastgame, ctx)
    assert said(ctx) == ["Game analysis isn't switched on yet."]
    monkeypatch.setattr(settings, "ANALYSIS_ENABLED", True)
    ctx = make_ctx(ALICE)
    run(botmod.lastgame, ctx)
    assert "No analysed games for alice_example yet" in said(ctx)[0]


def test_lastgame_still_shows_games_analysed_before_analysis_was_switched_off(monkeypatch):
    member()
    analysed(spec(1))
    monkeypatch.setattr(settings, "ANALYSIS_ENABLED", False)
    ctx = make_ctx(ALICE)
    run(botmod.lastgame, ctx)
    assert "<https://lichess.org/00000001>" in said(ctx)[0]


def test_lastgame_uses_the_same_account_picking_as_the_other_commands():
    ctx = make_ctx(ALICE)
    run(botmod.lastgame, ctx)
    assert reactions(ctx) == [NO] and "haven't added an account" in said(ctx)[0]
    member()
    member(ALICE, "alice_cc", "chess.com")
    ctx = make_ctx(ALICE)
    run(botmod.lastgame, ctx)
    assert "you have 2 accounts" in said(ctx)[0] and "!lastgame" in said(ctx)[0]
    ctx = make_ctx(ALICE)
    run(botmod.lastgame, ctx, "nobody_example")
    assert "isn't on the list" in said(ctx)[0]
    ctx = make_ctx(ALICE)
    run(botmod.lastgame, ctx, "alice_example", "myspace")
    assert "the site must be" in said(ctx)[0]


def test_lastgame_makes_no_calls_to_the_chess_sites_and_has_no_cooldown(monkeypatch):
    async def forbidden(*a, **k):
        raise AssertionError("a site was called")
    monkeypatch.setattr(sources, "month_games", forbidden)
    member()
    analysed(spec(1))
    run(botmod.lastgame, make_ctx(ALICE))
    assert botmod.lastgame._buckets._cooldown is None


# --- !analysisQ ---------------------------------------------------------------------------------------------------------------------

def test_analysisq_is_admin_only_and_has_a_longer_alias():
    assert botmod._admin_only in botmod.analysisq.checks
    assert botmod.bot.get_command("analysisq") is botmod.analysisq and botmod.bot.get_command("analysisQ") is botmod.analysisq
    assert botmod.bot.get_command("analysisqueue") is botmod.analysisq


def test_analysisq_shows_the_queue():
    member()
    analysed(spec(1))
    q.queue_games([spec(2), spec(3)], NOW)
    q.claim("desk", 1, NOW)
    ctx = make_ctx(ADMIN)
    run(botmod.analysisq, ctx)
    (text,) = said(ctx)
    assert text.startswith("**Analysis queue**") and "Done" in text and "Worker desk" in text and "switched off" not in text


def test_analysisq_counts_games_still_on_an_older_method():
    member()
    analysed(spec(1), spec(2))
    with store.transaction() as conn:
        conn.execute("UPDATE game_analysis SET method_version = 1")
    ctx = make_ctx(ADMIN)
    run(botmod.analysisq, ctx)
    assert "Waiting to be redone with the newer method: 2" in said(ctx)[0]


def test_analysisq_says_when_analysis_is_off(monkeypatch):
    monkeypatch.setattr(settings, "ANALYSIS_ENABLED", False)
    ctx = make_ctx(ADMIN)
    run(botmod.analysisq, ctx)
    assert "switched off" in said(ctx)[0]


# --- !gamestate ------------------------------------------------------------------------------------------------------------------

def test_gamestate_is_admin_only():
    assert botmod._admin_only in botmod.gamestate.checks


def test_gamestate_finds_the_game_by_link_or_bare_id_and_shows_the_row():
    member()
    analysed(spec(1))
    for game in ("https://lichess.org/00000001", "00000001"):
        ctx = make_ctx(ADMIN)
        run(botmod.gamestate, ctx, game)
        text = said(ctx)[0]
        assert "<https://lichess.org/00000001>" in text and "Status: done" in text and "Analysed" in text


def test_gamestate_finds_anyones_game_not_just_the_admins_own():
    member(owner=BOB, name="bob_example")                                  # a member, not the admin
    analysed(spec(1, "bob_example", "x_example"))
    ctx = make_ctx(ADMIN)
    run(botmod.gamestate, ctx, "00000001")
    assert "bob_example" in said(ctx)[0]


def test_gamestate_tries_live_and_daily_for_a_bare_chesscom_id():
    member(site="chess.com")
    analysed(spec(123456, site="chess.com"))                              # game_id "live/123456" (see analysis_helpers.spec)
    ctx = make_ctx(ADMIN)
    run(botmod.gamestate, ctx, "123456")                                  # a bare id: obit.candidates tries live/ and daily/
    assert "<https://www.chess.com/game/live/123456>" in said(ctx)[0]


def test_gamestate_refuses_text_that_is_not_a_game():
    ctx = make_ctx(ADMIN)
    run(botmod.gamestate, ctx, "not a game")
    assert reactions(ctx) == [NO] and "I can't read" in said(ctx)[0]


def test_gamestate_says_when_nothing_is_held_for_a_wellformed_id():
    ctx = make_ctx(ADMIN)
    run(botmod.gamestate, ctx, "00000001")
    assert reactions(ctx) == [NO] and said(ctx) == ["I don't hold anything for that game."]


# --- !queuemonth ---------------------------------------------------------------------------------------------------------------------

def test_queuemonth_is_admin_only():
    assert botmod._admin_only in botmod.queuemonth.checks


def test_queuemonth_refuses_when_analysis_is_off(monkeypatch):
    monkeypatch.setattr(settings, "ANALYSIS_ENABLED", False)
    ctx = make_ctx(ADMIN)
    run(botmod.queuemonth, ctx)
    assert reactions(ctx) == [NO] and "switched off" in said(ctx)[0]


def fake_backfill(monkeypatch, result, calls=None):
    async def backfill(month, now):
        if calls is not None:
            calls.append(month)
        return result
    monkeypatch.setattr(analysis_feed, "backfill", backfill)


def test_queuemonth_reports_what_it_queued_and_ticks(monkeypatch):
    from collections import Counter
    calls = []
    fake_backfill(monkeypatch, analysis_feed.Backfill(4, Counter({q.QUEUED: 60, q.QUEUED_LOW: 5, q.ALREADY_QUEUED: 12, q.OVER_LIMIT: 3})), calls)
    ctx = make_ctx(ADMIN)
    run(botmod.queuemonth, ctx)
    assert calls == [sources.current_month()]
    assert said(ctx) == ["Queued 65 game(s) from 4 player(s) for analysis (12 were already queued, 3 over the monthly limit)."]
    assert reactions(ctx) == [OK]


def test_queuemonth_names_the_players_it_could_not_do_and_does_not_tick(monkeypatch):
    from collections import Counter
    fake_backfill(monkeypatch, analysis_feed.Backfill(2, Counter({q.QUEUED: 5}), [("bob_example", "lichess", "the site is down")]))
    ctx = make_ctx(ADMIN)
    run(botmod.queuemonth, ctx)
    assert "Couldn't do: bob_example (lichess): the site is down" in said(ctx)[0] and reactions(ctx) == []


def test_queuemonth_will_not_run_twice_at_once(monkeypatch):
    async def go():
        async with botmod._queuemonth_lock:
            ctx = make_ctx(ADMIN)
            await botmod.queuemonth.callback(ctx)
            return ctx
    ctx = asyncio.run(go())
    assert reactions(ctx) == [NO] and "already running" in said(ctx)[0]


def test_queuemonth_really_queues_the_players_games_through_the_real_backfill(monkeypatch):
    member(ALICE, "alice_example")
    games = [game("W", when=at(2, 9), url="https://lichess.org/aaaaaaa1"), game("L", when=at(3, 9), url="https://lichess.org/aaaaaaa2")]

    async def fake_month_games(session, site_name, username, month, *, after=None, limit=None):
        return games
    monkeypatch.setattr(sources, "month_games", fake_month_games)
    ctx = make_ctx(ADMIN)
    run(botmod.queuemonth, ctx)
    assert "Queued 2 game(s) from 1 player(s)" in said(ctx)[0]
    assert q.status(NOW)["counts"]["pending"] == 2
    run(botmod.queuemonth, make_ctx(ADMIN))          # again: nothing new
    assert q.status(NOW)["counts"]["pending"] == 2


# --- the backfill itself ------------------------------------------------------------------------------------------------------------------

def run_backfill(month="2026-09"):
    from datetime import datetime, timezone
    return asyncio.run(analysis_feed.backfill(month, datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)))


@pytest.fixture
def site(monkeypatch):
    state = {"games": {}, "fail": {}, "calls": []}

    async def fake(session, site_name, username, month, *, after=None, limit=None):
        state["calls"].append((site_name, username, month, after, limit))
        if username in state["fail"]:
            raise state["fail"][username]
        return list(state["games"].get(username, []))
    monkeypatch.setattr(sources, "month_games", fake)
    return state


def li(n, day=2):
    return game("W", when=at(day, 8 + n % 12), url=f"https://lichess.org/{n:08d}")


def test_backfill_fetches_every_active_players_whole_month_and_queues_it(site):
    member(ALICE, "alice_example")
    member(BOB, "bob_example")
    store.remove_player("lichess", "bob_example")
    site["games"]["alice_example"] = [li(1), li(2, 3)]
    site["games"]["bob_example"] = [li(9)]
    result = run_backfill()
    assert result.players == 1 and result.outcome == {q.QUEUED: 2} and result.failures == []
    assert site["calls"] == [("lichess", "alice_example", "2026-09", None, None)]          # the whole month: no watermark, no limit


def test_backfill_is_safe_to_repeat_and_aggregates_the_players(site):
    member(ALICE, "alice_example")
    member(BOB, "bob_example")
    site["games"]["alice_example"] = [li(1)]
    site["games"]["bob_example"] = [li(2), li(3)]
    assert run_backfill().outcome == {q.QUEUED: 3}
    assert run_backfill().outcome == {q.ALREADY_QUEUED: 3}


def test_backfill_carries_on_past_a_player_it_cannot_fetch(site):
    member(ALICE, "alice_example")
    member(BOB, "bob_example")
    site["games"]["alice_example"] = [li(1)]
    site["fail"]["bob_example"] = sources.SourceError("the site is down")
    result = run_backfill()
    assert result.outcome == {q.QUEUED: 1} and result.failures == [("bob_example", "lichess", "the site is down")]


def test_backfill_survives_an_unexpected_error_for_one_player(site, caplog):
    member(ALICE, "alice_example")
    site["fail"]["alice_example"] = RuntimeError("boom")
    with caplog.at_level(logging.ERROR, logger="playmoreblitz.analysis"):
        result = run_backfill()
    assert result.failures == [("alice_example", "lichess", "unexpected error, see the log")] and "backfill: could not fetch" in caplog.text


def test_backfill_reports_a_player_whose_games_could_not_be_queued(site, monkeypatch):
    member(ALICE, "alice_example")
    site["games"]["alice_example"] = [li(1)]

    def broken(records, now):
        raise RuntimeError("no")
    monkeypatch.setattr(q, "queue_games", broken)
    assert run_backfill().failures == [("alice_example", "lichess", "could not be queued, see the log")]


def test_backfill_does_nothing_when_analysis_is_off(site, monkeypatch):
    monkeypatch.setattr(settings, "ANALYSIS_ENABLED", False)
    member()
    site["games"]["alice_example"] = [li(1)]
    result = run_backfill()
    assert (result.players, result.outcome, result.failures, site["calls"]) == (0, {}, [], [])


def test_backfill_with_no_games_is_not_a_failure(site):
    member()
    assert run_backfill().failures == [] and run_backfill().outcome == {}


# --- the analysis part of !mystats ----------------------------------------------------------------------------------------------------------

@pytest.fixture
def played(monkeypatch):
    state = {"games": [game("W", when=at(2, 10), rating_after=1510, colour="white", opening="London-System", opponent="rival_a"),
                       game("L", when=at(2, 11), rating_after=1495, colour="white", opening="London-System", opponent="rival_b")]}

    async def fake(session, site_name, username, month):
        return list(state["games"])
    monkeypatch.setattr(botmod.gamecache, "month_games", fake)
    return state


def open_month(name="alice_example"):
    store.add_player("lichess", name, ALICE, sources.current_month(), 1500)


def test_mystats_shows_the_analysis_part_after_the_results_when_games_are_in_the_queue(played):
    open_month()
    month = sources.current_month()
    analysed(spec(1, month=month), spec(2, month=month), white=side(accuracy=80.0), black=side(accuracy=60.0))
    ctx = make_ctx(ALICE)
    run(botmod.mystats, ctx)
    text = "\n".join(said(ctx))
    assert "**Analysis** (the bot's own, by Stockfish)" in text and "Analysed 2 of 2 games" in text and "Accuracy 80%" in text
    assert text.index("Games 2") < text.index("**Analysis**") < text.index("**As White**")


def test_mystats_has_no_analysis_part_for_a_player_with_no_games_in_the_queue(played):
    open_month()
    ctx = make_ctx(ALICE)
    run(botmod.mystats, ctx)
    assert "Analysis" not in "\n".join(said(ctx))


def test_mystats_still_works_if_the_analysis_cannot_be_read(played, monkeypatch, caplog):
    open_month()

    def broken(*a):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(analysis_reports, "month_summary", broken)
    ctx = make_ctx(ALICE)
    with caplog.at_level(logging.ERROR, logger="playmoreblitz"):
        run(botmod.mystats, ctx)
    text = "\n".join(said(ctx))
    assert "Games 2" in text and "**As White**" in text and "Analysis" not in text
    assert "could not read the analysis" in caplog.text


def test_mystatsfull_does_not_carry_the_analysis_part(played):
    open_month()
    analysed(spec(1, month=sources.current_month()))
    ctx = make_ctx(ALICE)
    run(botmod.mystatsfull, ctx)
    assert "Analysis" not in "\n".join(said(ctx))


def test_the_help_lists_lastgame():
    ctx = make_ctx(ALICE)
    run(botmod.help_blitz_bot, ctx)
    assert "`!lastgame [username]`" in said(ctx)[0]

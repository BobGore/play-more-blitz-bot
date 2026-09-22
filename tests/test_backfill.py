""""!backfill and /backfill: a member's own past month, fetched from its site and queued for analysis, in a direct message or by
slash command. The fetch-and-queue itself is analysis_feed.feed, already covered by tests/test_analysis_feed.py; these tests
cover the command's own rules (whose accounts, which months, the cooldown) and its DM/channel/slash wiring."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from analysis_helpers import MONTH, NOW, OWNER, register
from helpers import at, game

import analysis_queue as q
import bot as botmod
import settings
import sources
import store

OK, NO = "✅", "❌"
ALICE, BOB = OWNER, 1002
CHANNEL, DM_CHANNEL = 555, 777


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(botmod, "ALLOWED_CHANNEL_IDS", {CHANNEL})
    monkeypatch.setattr(botmod.sources, "current_month", lambda now=None: MONTH)
    monkeypatch.setattr(settings, "ANALYSIS_ENABLED", True)
    botmod._backfills.clear()


def flow(month=None, user=ALICE):
    return asyncio.run(botmod._backfill_flow(user, month))


def two_games():
    """Two games in November 2025, with real, distinct ids: game_records needs a recognisable url to keep a game."""
    return [game("W", when=at(2, month=11, year=2025), url="https://lichess.org/aaaaaaa1"),
            game("L", when=at(3, month=11, year=2025), url="https://lichess.org/aaaaaaa2")]


def fetching(monkeypatch, games_by_account=None, error=None, calls=None):
    """Fakes sources.month_games. `games_by_account` maps username -> list of sources.Game (default: two_games() each);
    `error` is raised for every account if given."""
    async def fake(session, site, username, month, *, after=None, limit=None):
        if calls is not None:
            calls.append((site, username, month))
        if error is not None:
            raise error
        if games_by_account is not None:
            return games_by_account.get(username, [])
        return two_games()
    monkeypatch.setattr(sources, "month_games", fake)


# --- the flow's own rules -------------------------------------------------------------------------------------------------------------

def test_refuses_someone_with_no_account():
    assert flow("2025-11") == (None, "you haven't added an account yet - use `!add <username> <site>` in the server's channel first")


def test_refuses_when_analysis_is_off(monkeypatch):
    monkeypatch.setattr(settings, "ANALYSIS_ENABLED", False)
    register("alice_example")
    assert flow("2025-11") == (None, "analysis is switched off (`ANALYSIS_ENABLED`)")


def test_refuses_with_no_month_named():
    register("alice_example")
    assert flow() == (None, "tell me which month, e.g. `!backfill 2025-11` or `!backfill november`")
    assert flow("") == (None, "tell me which month, e.g. `!backfill 2025-11` or `!backfill november`")


def test_refuses_a_month_it_does_not_understand():
    register("alice_example")
    text, error = flow("fortnight")
    assert text is None and error == "I don't know the month 'fortnight': try a name like `november` or a month like `2025-11`"


def test_refuses_a_month_that_has_not_happened():
    register("alice_example")
    assert flow("2099-01") == (None, "January 2099 hasn't happened yet")


def test_a_bad_month_error_is_kept_short():
    register("alice_example")
    _, error = flow("x" * 500)
    assert len(error) < 200


def test_accepts_a_month_name_or_an_explicit_month(monkeypatch):
    calls = []
    fetching(monkeypatch, calls=calls)
    register("alice_example")
    flow("november")
    assert calls == [("lichess", "alice_example", "2025-11")]                          # today is September 2026: latest November not in the future
    botmod._backfills.clear()
    calls.clear()
    flow("2025-11")
    assert calls == [("lichess", "alice_example", "2025-11")]


def test_really_fetches_and_queues_the_callers_own_accounts_only(monkeypatch):
    calls = []
    fetching(monkeypatch, calls=calls)
    register("alice_example")
    store.add_player("lichess", "bob_example", BOB, MONTH, 1500)
    text, error = flow("2025-11")
    assert error is None and calls == [("lichess", "alice_example", "2025-11")]         # not bob's account
    assert text == "November 2025: queued 2 game(s) for analysis (0 were already queued, 0 over the monthly limit)."
    assert q.status(NOW)["counts"]["pending"] == 2


def test_every_account_the_caller_owns_is_fetched(monkeypatch):
    calls = []
    fetching(monkeypatch, {"alice_example": [game("W", when=at(2, month=11, year=2025), url="https://lichess.org/aaaaaaa1")],
                            "alice_cc": [game("L", when=at(3, month=11, year=2025), url="https://www.chess.com/game/live/123")]}, calls=calls)
    register("alice_example")
    register("alice_cc", site="chess.com")
    text, error = flow("2025-11")
    assert error is None
    assert {(site, name) for site, name, _ in calls} == {("lichess", "alice_example"), ("chess.com", "alice_cc")}
    assert "queued 2 game(s)" in text


def test_running_it_twice_is_safe_and_the_second_time_finds_them_already_queued(monkeypatch):
    fetching(monkeypatch)
    register("alice_example")
    flow("2025-11")
    botmod._backfills.clear()
    text, error = flow("2025-11")
    assert error is None and text == "November 2025: queued 0 game(s) for analysis (2 were already queued, 0 over the monthly limit)."


def test_a_site_error_for_one_account_is_named_and_does_not_stop_the_others(monkeypatch):
    calls = []

    async def fake(session, site, username, month, *, after=None, limit=None):
        calls.append(username)
        if username == "alice_cc":
            raise sources.SourceError("chess.com is rate limiting me")
        return [game("W", when=at(2, month=11, year=2025), url="https://lichess.org/aaaaaaa1")]
    monkeypatch.setattr(sources, "month_games", fake)
    register("alice_example")
    register("alice_cc", site="chess.com")
    text, error = flow("2025-11")
    assert error is None and set(calls) == {"alice_example", "alice_cc"}
    assert "Couldn't do: alice_cc (chess.com): chess.com is rate limiting me" in text
    assert "queued 1 game(s)" in text


def test_an_unexpected_error_for_one_account_is_survived_and_logged(monkeypatch, caplog):
    async def fake(session, site, username, month, *, after=None, limit=None):
        raise ValueError("boom")
    monkeypatch.setattr(sources, "month_games", fake)
    register("alice_example")
    with caplog.at_level(logging.ERROR):
        text, error = flow("2025-11")
    assert error is None and "Couldn't do: alice_example (lichess): unexpected error, see the log" in text
    assert "backfill: could not fetch alice_example's games on lichess" in caplog.text


def test_one_person_backfills_once_a_minute_and_someone_elses_wait_is_their_own(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(botmod, "_monotonic", lambda: clock[0])
    fetching(monkeypatch)
    register("alice_example")
    store.add_player("lichess", "bob_example", BOB, MONTH, 1500)
    assert flow("2025-11")[1] is None
    assert flow("2025-12") == (None, "one backfill at a time please: try again in a minute")
    assert flow("2025-11", user=BOB)[1] is None
    clock[0] += botmod.BACKFILL_GAP_SECONDS - 1
    assert flow("2025-12")[1] is not None
    clock[0] += 1
    assert flow("2025-12")[1] is None


# --- !backfill in a direct message ----------------------------------------------------------------------------------------------------

class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def the_server(*member_ids, error=None, guild_id=1):
    async def fetch_member(user_id):
        if user_id in member_ids:
            return SimpleNamespace(id=user_id)
        raise error or discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Member")
    return SimpleNamespace(id=guild_id, fetch_member=AsyncMock(side_effect=fetch_member))


@pytest.fixture(autouse=True)
def on_the_server(monkeypatch):
    channel = SimpleNamespace(id=CHANNEL, guild=the_server(ALICE, BOB), send=AsyncMock())
    monkeypatch.setattr(botmod.bot, "get_channel", lambda channel_id: channel if channel_id == CHANNEL else None)
    return channel


def make_ctx(author_id=ALICE, dm=True):
    return SimpleNamespace(author=SimpleNamespace(id=author_id), channel=SimpleNamespace(id=DM_CHANNEL if dm else CHANNEL), guild=None if dm else SimpleNamespace(id=1),
                           message=SimpleNamespace(add_reaction=AsyncMock()), send=AsyncMock(), typing=lambda: Typing(), command=MagicMock())


def backfill(ctx, *args):
    asyncio.run(botmod.backfill_command.callback(ctx, *args))


def said(ctx):
    return [c.args[0] for c in ctx.send.await_args_list]


def reactions(ctx):
    return [c.args[0] for c in ctx.message.add_reaction.await_args_list]


def test_a_dm_queues_the_month_and_ticks(monkeypatch):
    fetching(monkeypatch)
    register("alice_example")
    ctx = make_ctx()
    backfill(ctx, "2025-11")
    assert reactions(ctx) == [OK] and said(ctx) == ["November 2025: queued 2 game(s) for analysis (0 were already queued, 0 over the monthly limit)."]


def test_a_dm_with_a_problem_gets_it_in_words():
    ctx = make_ctx()
    backfill(ctx, "2025-11")                                                                          # not registered
    assert reactions(ctx) == [NO] and "haven't added an account" in said(ctx)[0]
    register("alice_example")
    ctx = make_ctx()
    backfill(ctx, "fortnight")
    assert reactions(ctx) == [NO] and "I don't know the month 'fortnight'" in said(ctx)[0]
    ctx = make_ctx()
    backfill(ctx)
    assert reactions(ctx) == [NO] and "tell me which month" in said(ctx)[0]


def test_someone_who_is_not_on_the_server_gets_nothing(on_the_server):
    register("alice_example")
    on_the_server.guild = the_server(BOB)
    ctx = make_ctx()
    backfill(ctx, "2025-11")
    assert reactions(ctx) == [NO] and said(ctx) == ["This is only for members of the server."]


def test_in_the_channel_backfill_only_points_to_slash_and_dms():
    ctx = make_ctx(dm=False)
    backfill(ctx, "2025-11")
    ctx.send.assert_awaited_once_with(botmod.BACKFILL_HINT, delete_after=20)
    assert "/backfill" in botmod.BACKFILL_HINT and "direct message" in botmod.BACKFILL_HINT and reactions(ctx) == []


def test_a_failure_to_post_the_reply_is_survived(monkeypatch):
    fetching(monkeypatch)
    register("alice_example")
    ctx = make_ctx()
    ctx.send = AsyncMock(side_effect=discord.HTTPException(SimpleNamespace(status=500, reason="oops"), "server error"))
    backfill(ctx, "2025-11")
    assert reactions(ctx) == [OK]                                                                     # the tick still lands; only the reply failed


# --- /backfill --------------------------------------------------------------------------------------------------------------------------

def make_interaction(user_id=ALICE, channel_id=CHANNEL):
    return SimpleNamespace(user=SimpleNamespace(id=user_id), channel_id=channel_id,
                           response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))


def slash(interaction, *args):
    asyncio.run(botmod.backfill_slash.callback(interaction, *args))


def test_slash_backfill_queues_the_month_and_answers_only_the_asker(monkeypatch):
    fetching(monkeypatch)
    register("alice_example")
    interaction = make_interaction()
    slash(interaction, "2025-11")
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.followup.send.assert_awaited_once_with(
        "November 2025: queued 2 game(s) for analysis (0 were already queued, 0 over the monthly limit).", ephemeral=True)
    interaction.response.send_message.assert_not_awaited()


def test_slash_backfill_outside_the_channel_is_refused_before_any_fetch(monkeypatch):
    calls = []
    fetching(monkeypatch, calls=calls)
    register("alice_example")
    interaction = make_interaction(channel_id=CHANNEL + 1)
    slash(interaction, "2025-11")
    interaction.response.send_message.assert_awaited_once_with("This command only works in the blitz channel.", ephemeral=True)
    assert calls == []


def test_slash_backfill_problems_are_private(monkeypatch):
    interaction = make_interaction()
    slash(interaction, "2025-11")                                                                      # not registered
    interaction.followup.send.assert_awaited_once_with(
        "you haven't added an account yet - use `!add <username> <site>` in the server's channel first", ephemeral=True)


# --- the checks let a DM run backfill too, and !usage counts it like any other command --------------------------------------------------

def test_the_command_checks_let_a_dm_run_backfill():
    async def passes():
        ctx = SimpleNamespace(guild=None, channel=SimpleNamespace(id=DM_CHANNEL), command=SimpleNamespace(name="backfill"))
        return all([await discord.utils.maybe_coroutine(check, ctx) for check in botmod.bot._checks])
    assert asyncio.run(passes()) is True

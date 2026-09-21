"""Deleting the bot's own messages in a DM: a Delete button on everything it says there, and !clear for the ones already sent (Discord
doesn't let a person delete a bot's messages in a DM, but a bot can delete its own, whatever their age)."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext import commands

import bot as botmod
import store

ALICE, BOT = 1001, 999
CHANNEL, DM_CHANNEL = 555, 777


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(botmod, "ALLOWED_CHANNEL_IDS", {CHANNEL})
    monkeypatch.setattr(type(botmod.bot), "user", property(lambda self: SimpleNamespace(id=BOT)))
    guild = SimpleNamespace(id=1, fetch_member=AsyncMock(return_value=SimpleNamespace(id=ALICE)))
    channel = SimpleNamespace(id=CHANNEL, guild=guild)
    monkeypatch.setattr(botmod.bot, "get_channel", lambda channel_id: channel if channel_id == CHANNEL else None)


def run(coro):
    return asyncio.run(coro)


# --- a Delete button on everything the bot says in a DM ---------------------------------------------------------------------------------

def context(guild):
    ctx = object.__new__(botmod.DMContext)
    ctx.message = SimpleNamespace(guild=guild)
    return ctx


@pytest.fixture
def base_send(monkeypatch):
    calls = []

    async def fake(self, content=None, **kwargs):
        calls.append((content, kwargs))
    monkeypatch.setattr(commands.Context, "send", fake)
    return calls


def test_what_the_bot_says_in_a_dm_carries_a_delete_button(base_send):
    async def go():
        await context(None).send("your usage", delete_after=30)
    run(go())
    (content, kwargs), = base_send
    assert content == "your usage" and kwargs["delete_after"] == 30 and isinstance(kwargs["view"], botmod.DeleteButton)


def test_what_the_bot_says_in_a_server_is_untouched(base_send):
    run(context(SimpleNamespace(id=1)).send("results table"))
    assert base_send == [("results table", {})]


def test_a_view_given_explicitly_is_kept(base_send):
    mine = object()
    run(context(None).send("x", view=mine))
    assert base_send[0][1]["view"] is mine


def test_the_bot_uses_that_context_for_every_message(monkeypatch):
    seen = []

    async def fake(self, origin, *, cls=commands.Context):
        seen.append(cls)
    monkeypatch.setattr(commands.Bot, "get_context", fake)
    run(botmod.bot.get_context(object()))
    assert isinstance(botmod.bot, botmod.PlayMoreBlitzBot) and seen == [botmod.DMContext]


# --- !clear -----------------------------------------------------------------------------------------------------------------------------------

class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def message(author_id, delete=None):
    return SimpleNamespace(author=SimpleNamespace(id=author_id), delete=delete or AsyncMock())


def dm_ctx(messages, dm=True):
    seen = {}

    async def generator(limit):
        seen["limit"] = limit
        for m in messages:
            yield m

    channel = SimpleNamespace(id=DM_CHANNEL if dm else CHANNEL, history=lambda limit=None: generator(limit))
    ctx = SimpleNamespace(author=SimpleNamespace(id=ALICE), channel=channel, guild=None if dm else SimpleNamespace(id=1), typing=lambda: Typing(),
                          message=SimpleNamespace(add_reaction=AsyncMock()), send=AsyncMock())
    ctx.seen = seen
    return ctx


def clear(ctx):
    run(botmod.clear_command.callback(ctx))


def said(ctx):
    return [(c.args[0], c.kwargs) for c in ctx.send.await_args_list]


def reactions(ctx):
    return [c.args[0] for c in ctx.message.add_reaction.await_args_list]


def test_only_the_bots_own_messages_are_deleted_and_the_person_is_told_how_many(caplog):
    mine = [message(BOT) for _ in range(3)]
    theirs = [message(ALICE), message(ALICE)]
    ctx = dm_ctx([mine[0], theirs[0], mine[1], theirs[1], mine[2]])
    with caplog.at_level(logging.INFO, logger="playmoreblitz"):
        clear(ctx)
    assert all(m.delete.await_count == 1 for m in mine) and all(m.delete.await_count == 0 for m in theirs)
    assert reactions(ctx) == ["✅"] and said(ctx) == [("Deleted 3 of my messages here.", {"delete_after": 30})]
    assert f"!clear for {ALICE}: deleted 3 of the bot's messages (0 failed)" in caplog.text


def test_it_looks_back_through_the_most_recent_thousand_messages():
    ctx = dm_ctx([])
    clear(ctx)
    assert ctx.seen["limit"] == botmod.CLEAR_LIMIT == 1000
    assert said(ctx) == [("Deleted 0 of my messages here.", {"delete_after": 30})]


def test_a_message_already_gone_is_not_counted_and_one_that_cannot_be_deleted_is_reported():
    gone = message(BOT, AsyncMock(side_effect=discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")))
    stuck = message(BOT, AsyncMock(side_effect=discord.HTTPException(SimpleNamespace(status=500, reason="oops"), "server error")))
    fine = message(BOT)
    ctx = dm_ctx([gone, stuck, fine])
    clear(ctx)
    assert reactions(ctx) == ["❌"] and said(ctx) == [("Deleted 1 of my messages here. I couldn't delete 1.", {"delete_after": 30})]


def test_in_the_channel_it_only_points_to_a_dm_in_a_note_that_removes_itself():
    victim = message(BOT)
    ctx = dm_ctx([victim], dm=False)
    clear(ctx)
    assert said(ctx) == [(botmod.CLEAR_HINT, {"delete_after": 20})] and "limit" not in ctx.seen and victim.delete.await_count == 0
    assert "direct message" in botmod.CLEAR_HINT


def test_someone_not_on_the_server_gets_nothing_deleted_and_is_told(monkeypatch):
    victim = message(BOT)
    guild = SimpleNamespace(id=1, fetch_member=AsyncMock(side_effect=discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Member")))
    monkeypatch.setattr(botmod.bot, "get_channel", lambda channel_id: SimpleNamespace(id=CHANNEL, guild=guild))
    ctx = dm_ctx([victim])
    clear(ctx)
    assert reactions(ctx) == ["❌"] and said(ctx) == [("This is only for members of the server.", {})] and victim.delete.await_count == 0 and "limit" not in ctx.seen


def test_when_membership_cannot_be_checked_nothing_is_deleted(monkeypatch):
    victim = message(BOT)
    monkeypatch.setattr(botmod.bot, "get_channel", lambda channel_id: None)
    ctx = dm_ctx([victim])
    clear(ctx)
    assert reactions(ctx) == ["❌"] and "couldn't check" in said(ctx)[0][0] and victim.delete.await_count == 0


def test_a_failure_to_post_the_summary_is_survived(caplog):
    ctx = dm_ctx([message(BOT)])
    ctx.send = AsyncMock(side_effect=discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions"))
    with caplog.at_level(logging.WARNING, logger="playmoreblitz"):
        clear(ctx)
    assert "couldn't answer a direct message" in caplog.text


def test_a_failure_to_post_the_hint_in_the_channel_is_survived():
    ctx = dm_ctx([], dm=False)
    ctx.send = AsyncMock(side_effect=discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions"))
    clear(ctx)


# --- who may send it, and where it is described --------------------------------------------------------------------------------------------------

def passes(guild, channel_id, command):
    ctx = SimpleNamespace(guild=guild, channel=SimpleNamespace(id=channel_id), author=SimpleNamespace(id=ALICE), command=SimpleNamespace(name=command))

    async def run_all():
        return all([await discord.utils.maybe_coroutine(check, ctx) for check in botmod.bot._checks])
    return run(run_all())


def test_it_may_be_sent_in_a_dm_and_in_the_allowed_channel_only():
    assert passes(None, DM_CHANNEL, "clear") is True and passes(SimpleNamespace(id=1), CHANNEL, "clear") is True
    assert passes(SimpleNamespace(id=1), CHANNEL + 1, "clear") is False
    assert "clear" in botmod.DM_COMMANDS and botmod.USAGE["clear"] == "!clear"


def test_help_and_readme_describe_it_and_the_help_still_fits_in_a_message():
    ctx = SimpleNamespace(send=AsyncMock())
    run(botmod.help_blitz_bot.callback(ctx))
    text = ctx.send.await_args.args[0]
    assert "`!clear`" in text and "direct message" in text and len(text) < 2000
    readme = open(botmod.__file__.replace("bot.py", "README.md"), encoding="utf-8").read()
    assert any(line.startswith("| `!clear` | Anyone on the server, in a direct message to the bot |") for line in readme.splitlines())

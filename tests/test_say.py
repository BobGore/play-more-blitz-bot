"""!say: an admin, in a direct message, has the bot post a message in its channel word for word."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

import bot as botmod

ADMIN = min(botmod.ADMIN_USER_IDS)


def dm():
    return SimpleNamespace(author=SimpleNamespace(id=ADMIN), guild=None, channel=SimpleNamespace(id=777), send=AsyncMock(),
                           message=SimpleNamespace(add_reaction=AsyncMock()), command=botmod.say)


def the_channel(monkeypatch, send):
    channel = SimpleNamespace(send=send)
    monkeypatch.setattr(botmod, "_post_channel", AsyncMock(return_value=channel))
    return channel


def test_it_is_an_admin_only_dm_command():
    assert botmod._admin_only in botmod.say.checks and "say" in botmod.ADMIN_DM_COMMANDS


def test_the_message_is_posted_word_for_word_with_no_mass_pings(monkeypatch):
    send = AsyncMock(return_value=SimpleNamespace(jump_url="https://discord.com/channels/1/2/3"))
    the_channel(monkeypatch, send)
    ctx = dm()
    text = "🏆 Most games in September: `alice_example` with **795 games**. @everyone"
    asyncio.run(botmod.say.callback(ctx, message=text))
    args, kwargs = send.call_args
    assert args == (text,)
    mentions = kwargs["allowed_mentions"]
    assert mentions.everyone is False and mentions.roles is False and mentions.users is True
    ctx.message.add_reaction.assert_awaited_with("✅")
    ctx.send.assert_awaited_with("Posted: https://discord.com/channels/1/2/3")


def test_too_long_a_message_is_refused_and_nothing_is_posted(monkeypatch):
    send = AsyncMock()
    the_channel(monkeypatch, send)
    ctx = dm()
    asyncio.run(botmod.say.callback(ctx, message="x" * 2001))
    send.assert_not_awaited()
    ctx.message.add_reaction.assert_awaited_with("❌")
    assert "2001 characters" in ctx.send.call_args.args[0]


def test_a_failed_post_is_reported_to_the_admin(monkeypatch):
    send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=503), "down"))
    the_channel(monkeypatch, send)
    ctx = dm()
    asyncio.run(botmod.say.callback(ctx, message="hello"))
    ctx.message.add_reaction.assert_awaited_with("❌")
    assert "couldn't post" in ctx.send.call_args.args[0]

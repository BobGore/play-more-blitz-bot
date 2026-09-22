"""System-type admin commands work only in a direct message to the bot, and only for an admin."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ext import commands

import bot as botmod

ADMIN = min(botmod.ADMIN_USER_IDS)
MEMBER = 1001
CHANNEL, OTHER_CHANNEL, DM_CHANNEL = 555, 556, 777
SERVER = SimpleNamespace(id=1)


@pytest.fixture(autouse=True)
def the_blitz_channel(monkeypatch):
    monkeypatch.setattr(botmod, "ALLOWED_CHANNEL_IDS", {CHANNEL})


def passes(user, guild, channel_id, command):
    """Whether a message would get past every check registered on the bot, run the way discord.py runs them."""
    ctx = SimpleNamespace(guild=guild, channel=SimpleNamespace(id=channel_id), author=SimpleNamespace(id=user),
                          command=SimpleNamespace(name=command) if command else None)

    async def run_all():
        return all([await discord.utils.maybe_coroutine(check, ctx) for check in botmod.bot._checks])
    return asyncio.run(run_all())


def test_the_system_commands_are_named():
    assert botmod.ADMIN_DM_COMMANDS == ("analysisq", "queuemonth", "closemonth", "setowner", "usage", "gamestate")
    for name in botmod.ADMIN_DM_COMMANDS:
        command = botmod.bot.get_command(name)
        assert command is not None and botmod._admin_only in command.checks                       # each is admin-only in its own right


@pytest.mark.parametrize("name", botmod.ADMIN_DM_COMMANDS)
def test_an_admin_can_send_a_system_command_in_a_dm(name):
    assert passes(ADMIN, None, DM_CHANNEL, name) is True


@pytest.mark.parametrize("name", botmod.ADMIN_DM_COMMANDS)
def test_nobody_else_can_send_one_even_in_a_dm(name):
    assert passes(MEMBER, None, DM_CHANNEL, name) is False


@pytest.mark.parametrize("name", botmod.ADMIN_DM_COMMANDS)
def test_not_even_an_admin_can_use_one_in_the_channel(name):
    assert passes(ADMIN, SERVER, CHANNEL, name) is False and passes(MEMBER, SERVER, CHANNEL, name) is False


def test_everything_else_is_as_it_was():
    for name in ("results", "mystats", "add", "remove", "helpblitzbot", "lastgame"):
        assert passes(MEMBER, SERVER, CHANNEL, name) is True and passes(ADMIN, SERVER, CHANNEL, name) is True
        assert passes(MEMBER, SERVER, OTHER_CHANNEL, name) is False
        assert passes(ADMIN, None, DM_CHANNEL, name) is False and passes(MEMBER, None, DM_CHANNEL, name) is False
    # obit/export/backfill/history: the check lets a DM through. A channel message passes the check too (they aren't admin
    # system commands), same as before; each one's own body is what gives a hint instead of really running there.
    for name in ("obit", "export", "backfill", "history", "myhistory"):
        assert passes(MEMBER, None, DM_CHANNEL, name) is True
    assert passes(ADMIN, None, DM_CHANNEL, None) is False


def hint_ctx(user, guild, channel_id, command="analysisq"):
    return SimpleNamespace(author=SimpleNamespace(id=user), guild=guild, channel=SimpleNamespace(id=channel_id), invoked_with=command,
                           command=SimpleNamespace(name=command), send=AsyncMock(), message=SimpleNamespace(add_reaction=AsyncMock()))


def refused(ctx):
    asyncio.run(botmod.on_command_error(ctx, commands.CheckFailure("no")))
    return ctx


def test_an_admin_who_types_a_system_command_in_the_channel_is_told_where_it_goes_in_a_note_that_removes_itself():
    ctx = refused(hint_ctx(ADMIN, SERVER, CHANNEL))
    ctx.send.assert_awaited_once_with(botmod.ADMIN_HINT, delete_after=20)
    assert "direct message" in botmod.ADMIN_HINT


@pytest.mark.parametrize("who, guild, channel, command", [
    (MEMBER, SERVER, CHANNEL, "analysisq"),            # not an admin: silent, as before
    (ADMIN, SERVER, OTHER_CHANNEL, "analysisq"),       # not the bot's channel: silent
    (ADMIN, None, DM_CHANNEL, "analysisq"),            # already in a DM: nothing to say (something else refused it)
    (ADMIN, SERVER, CHANNEL, "results"),               # not a system command
])
def test_nobody_else_gets_a_hint(who, guild, channel, command):
    ctx = refused(hint_ctx(who, guild, channel, command))
    ctx.send.assert_not_awaited()


def test_a_failure_to_post_the_hint_is_survived():
    ctx = hint_ctx(ADMIN, SERVER, CHANNEL)
    ctx.send = AsyncMock(side_effect=discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions"))
    refused(ctx)


def test_the_readme_says_the_system_commands_are_for_a_direct_message():
    readme = open(botmod.__file__.replace("bot.py", "README.md"), encoding="utf-8").read()
    for name in ("!setowner <username> <@member> [site]", "!closemonth", "!analysisq", "!queuemonth"):
        line = next(l for l in readme.splitlines() if l.startswith(f"| `{name}`"))
        assert "in a direct message to the bot" in line

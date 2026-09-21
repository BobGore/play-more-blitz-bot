"""The log tells the story of each request, so the person running the bot can see that a DM asking for something was answered by
delivering it: a line when the request arrives (on_command), a line for its outcome, and a line when the DM is delivered.
These lines carry IDs, outcomes and counts only: never what was typed, and never what was sent."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from analysis_helpers import NOW, analysed, register, spec

import analysis_queue as q
import bot as botmod
import settings
import store

ALICE = 1001
CHANNEL, DM_CHANNEL = 555, 777
TYPED = "SECRET-WORDS-THE-PERSON-TYPED"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(settings, "ANALYSIS_ENABLED", True)
    monkeypatch.setattr(botmod, "ALLOWED_CHANNEL_IDS", {CHANNEL})
    botmod._lookups.clear()
    botmod._exports.clear()
    guild = SimpleNamespace(id=1, fetch_member=AsyncMock(return_value=SimpleNamespace(id=ALICE)))
    channel = SimpleNamespace(id=CHANNEL, guild=guild, send=AsyncMock())
    monkeypatch.setattr(botmod.bot, "get_channel", lambda channel_id: channel if channel_id == CHANNEL else None)
    monkeypatch.setattr(botmod.refresh, "refresh_one", AsyncMock())


@pytest.fixture
def delivered(monkeypatch):
    """A stand-in for the person's DMs: what `user.send` was given (content and file names)."""
    sent = []

    class FakeUser:
        async def send(self, content, **kwargs):
            file = kwargs.get("file")
            sent.append((content, file.filename if file else None))
    monkeypatch.setattr(botmod.bot, "get_user", lambda user_id: FakeUser() if user_id == ALICE else None)
    return sent


@pytest.fixture
def log(caplog):
    caplog.set_level(logging.INFO, logger="playmoreblitz")
    return caplog


def lines(log):
    return [r.getMessage() for r in log.records if r.name == "playmoreblitz"]


def run(coro):
    return asyncio.run(coro)


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def dm_ctx():
    return SimpleNamespace(author=SimpleNamespace(id=ALICE), channel=SimpleNamespace(id=DM_CHANNEL), guild=None, message=SimpleNamespace(add_reaction=AsyncMock()),
                           send=AsyncMock(), typing=lambda: Typing(), command=SimpleNamespace(qualified_name="obit", name="obit"))


# --- each DM the bot sends -----------------------------------------------------------------------------------------------------------------

def test_a_dm_sent_is_noted_by_count_only(delivered, log):
    run(botmod._dm(ALICE, ["your review is very secret"]))
    run(botmod._dm(ALICE, ["one", "two"]))
    assert lines(log) == [f"DM sent to {ALICE} (1 message)", f"DM sent to {ALICE} (2 messages)"]
    assert "secret" not in log.text


def test_a_dm_that_cannot_be_sent_is_not_noted_as_sent(monkeypatch, log):
    class Closed:
        async def send(self, content, **kwargs):
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Cannot send messages to this user")
    monkeypatch.setattr(botmod.bot, "get_user", lambda user_id: Closed())
    with pytest.raises(discord.Forbidden):
        run(botmod._dm(ALICE, ["x"]))
    assert "DM sent" not in log.text


def test_files_sent_are_noted_by_count_and_never_by_content(delivered, log):
    run(botmod._dm_parts(ALICE, [{"text": "one file", "filename": "a.csv", "data": b"secret,figures\r\n"}, {"text": "no games"}]))
    run(botmod._dm_parts(ALICE, [{"text": "x", "filename": "b.csv", "data": b"y"}]))
    assert lines(log) == [f"export sent to {ALICE}: 2 messages, 1 file", f"export sent to {ALICE}: 1 message, 1 file"]
    assert "secret" not in log.text and "a.csv" not in log.text


# --- a review ----------------------------------------------------------------------------------------------------------------------------------

def request(game_id="00000001"):
    return {"user_id": ALICE, "site": "lichess", "game_id": game_id, "username": "alice_example", "channel_id": DM_CHANNEL, "requested_at": 10 ** 10}


def test_a_review_delivered_is_noted_with_the_game_and_the_person(delivered, log):
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example"))
    botmod.obit.add_request(ALICE, "lichess", "00000001", "alice_example", DM_CHANNEL, 10 ** 10)
    assert run(botmod._process_obit(request(), immediate=True)) == "sent"
    assert f"review of lichess 00000001 sent to {ALICE}" in lines(log)
    assert delivered and "x_example" not in " ".join(l for l in lines(log))                     # the opponent's name is in the review, not the log


def test_a_review_that_could_not_be_delivered_is_a_warning(monkeypatch, log):
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example"))
    botmod.obit.add_request(ALICE, "lichess", "00000001", "alice_example", DM_CHANNEL, 10 ** 10)

    async def refuse(user_id, messages):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Cannot send messages to this user")
    monkeypatch.setattr(botmod, "_dm", refuse)
    assert run(botmod._process_obit(request(), immediate=True)) == "no_dm"
    warnings = [r for r in log.records if r.levelno == logging.WARNING]
    assert [w.getMessage() for w in warnings] == [f"couldn't send the review of lichess 00000001 to {ALICE}: they don't accept DMs from the bot"]


def test_a_review_given_up_on_is_noted_with_why(delivered, log):
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example")], NOW)
    with store.transaction() as conn:
        conn.execute("UPDATE game_analysis SET status = 'failed'")
    botmod.obit.add_request(ALICE, "lichess", "00000001", "alice_example", DM_CHANNEL, 10 ** 10)
    assert run(botmod._process_obit(request(), immediate=True)) == "closed"
    assert f"the request of {ALICE} for lichess 00000001 was closed without a review (cant)" in lines(log)


@pytest.mark.parametrize("game, kind", [("00000001", "sent"), ("00000002", "waiting"), (TYPED, "error"), (None, "waiting")])      # no game: the latest, still queued
def test_each_request_is_noted_with_how_it_came_out_and_never_with_what_was_typed(delivered, log, game, kind):
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example"))
    q.queue_games([spec(2, "alice_example", "y_example")], NOW)
    run(botmod._obit_flow(ALICE, DM_CHANNEL, game))
    assert f"obit request from {ALICE}: {kind}" in lines(log)
    assert TYPED not in log.text


def test_a_refused_request_for_something_never_seen_is_noted_as_an_error(delivered, log):
    register("alice_example")
    run(botmod._obit_flow(ALICE, DM_CHANNEL, "00000042"))
    assert f"obit request from {ALICE}: error" in lines(log)


# --- an export -----------------------------------------------------------------------------------------------------------------------------------

def test_an_export_request_is_noted_as_refused_without_the_reason_or_the_typed_text(log):
    register("alice_example")
    run(botmod._export_flow(ALICE, "games", TYPED))
    assert f"export request from {ALICE} (games): refused" in lines(log) and TYPED not in log.text


def test_an_export_request_that_works_is_noted_with_the_number_of_files(log):
    register("alice_example")
    register("alice_cc", site="chess.com")
    q.queue_games([spec(1, "alice_example", "x_example", ended_at=NOW - 60)], NOW)
    run(botmod._export_flow(ALICE, "summary", "all"))
    assert f"export request from {ALICE} (summary): 1 file(s) ready" in lines(log)


# --- the whole story ---------------------------------------------------------------------------------------------------------------------------------

def test_a_dm_asking_for_a_review_reads_as_received_answered_and_delivered(delivered, log):
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example"))
    ctx = dm_ctx()

    async def as_discord_runs_it():
        await botmod.on_command(ctx)                                                      # the log line for the request arriving
        await botmod._count_command(ctx)
        await botmod.obit_command.callback(ctx, "00000001")
    run(as_discord_runs_it())
    story = [l for l in lines(log) if str(ALICE) in l]
    assert story == [
        f"command !obit from {ALICE} in channel {DM_CHANNEL}",          # 1 it arrived
        f"DM sent to {ALICE} (1 message)",                               # 2 the review was delivered
        f"review of lichess 00000001 sent to {ALICE}",                   # 3 which review
        f"obit request from {ALICE}: sent",                              # 4 and the request is answered
    ]
    assert len(delivered) == 1 and delivered[0][0].startswith("**OBIT**")                   # and the person did get it


def test_a_dm_asking_for_an_export_reads_as_received_ready_and_delivered(delivered, log):
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example", ended_at=NOW - 60)], NOW)
    ctx = dm_ctx()
    ctx.command = SimpleNamespace(qualified_name="export", name="export")

    async def as_discord_runs_it():
        await botmod.on_command(ctx)
        await botmod.export_command.callback(ctx, "all")
    run(as_discord_runs_it())
    story = [l for l in lines(log) if str(ALICE) in l]
    assert story == [f"command !export from {ALICE} in channel {DM_CHANNEL}", f"export request from {ALICE} (games): 1 file(s) ready",
                     f"export sent to {ALICE}: 1 message, 1 file"]
    assert delivered == [("`alice_example` · Lichess · all the games held: 1 games (0 analysed).", "lichess_alice_example_all.csv")]

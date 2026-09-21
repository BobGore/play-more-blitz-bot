"""The bot's side of watching over itself: alerts by DM, the health loop, the heartbeat, and counting each use."""

import asyncio
import logging
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from analysis_helpers import NOW, analysed, register, spec
from discord.ext import commands

import analysis_queue as q
import bot as botmod
import monitoring
import settings
import store
import usage

BOB, ALICE = 1, 2


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(botmod, "ALERT_USER_IDS", {BOB})
    clock = Clock()
    monkeypatch.setattr(botmod, "_alerts", monitoring.Alerts(clock))
    monkeypatch.setitem(botmod._health, "refresh_failures", 0)
    monkeypatch.setitem(botmod._health, "heartbeat_failing", False)
    monkeypatch.setattr(settings, "BACKUP_DIR", tmp_path / "backups")
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / f"playmoreblitz-{date.today().isoformat()}.db").write_bytes(b"x")     # a fresh backup
    return clock


@pytest.fixture
def clock(isolated):
    return isolated


@pytest.fixture
def dms(monkeypatch):
    sent = []

    async def fake(user_id, messages):
        sent.append((user_id, list(messages)))
    monkeypatch.setattr(botmod, "_dm", fake)
    return sent


def run(coro):
    return asyncio.run(coro)


def totals():
    return usage.report(1)["totals"]


# --- sending an alert -------------------------------------------------------------------------------------------------------------------

def test_an_alert_goes_by_dm_to_each_person_listed_headed_so_it_is_recognised(dms, monkeypatch):
    monkeypatch.setattr(botmod, "ALERT_USER_IDS", {BOB, ALICE})
    run(botmod._alert_admins("k", "the worker is quiet"))
    assert sorted(dms) == [(BOB, ["⚠ PlayMoreBlitz: the worker is quiet"]), (ALICE, ["⚠ PlayMoreBlitz: the worker is quiet"])]


def test_the_same_problem_is_sent_once_until_the_repeat_time_has_passed_and_the_default_is_six_hours(dms, clock):
    run(botmod._alert_admins("k", "x"))
    run(botmod._alert_admins("k", "x"))
    assert len(dms) == 1
    clock.now += monitoring.REPEAT_SECONDS
    run(botmod._alert_admins("k", "x"))
    assert len(dms) == 2
    run(botmod._alert_admins("other", "y", 60))
    clock.now += 60
    run(botmod._alert_admins("other", "y", 60))
    assert len(dms) == 4


def test_a_dm_that_cannot_be_sent_is_logged_and_the_others_still_get_theirs(monkeypatch, caplog):
    monkeypatch.setattr(botmod, "ALERT_USER_IDS", {BOB, ALICE})
    got = []

    async def flaky(user_id, messages):
        if user_id == BOB:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Cannot send messages to this user")
        got.append(user_id)
    monkeypatch.setattr(botmod, "_dm", flaky)
    with caplog.at_level("ERROR", logger="playmoreblitz"):
        run(botmod._alert_admins("k", "something wrong"))
    assert got == [ALICE] and "couldn't send an alert to 1: something wrong" in caplog.text


# --- counting ---------------------------------------------------------------------------------------------------------------------------

def test_a_command_run_is_counted_with_the_person_who_ran_it():
    ctx = SimpleNamespace(command=SimpleNamespace(qualified_name="results"), author=SimpleNamespace(id=ALICE), channel=SimpleNamespace(id=5))
    run(botmod.on_command(ctx))
    run(botmod.on_command(ctx))
    assert totals() == {"results": 2} and usage.report(1)["days"][0]["people"] == 1


def test_counting_never_breaks_what_is_being_counted(monkeypatch, caplog):
    def broken(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(botmod.usage_stats, "count", broken)
    with caplog.at_level("ERROR", logger="playmoreblitz"):
        run(botmod._count("results", ALICE))
    assert "couldn't count a use of results" in caplog.text


# --- unexpected errors -------------------------------------------------------------------------------------------------------------------

def error_ctx(name="results"):
    return SimpleNamespace(command=SimpleNamespace(name=name), invoked_with=name, author=SimpleNamespace(id=ALICE), channel=SimpleNamespace(id=5),
                           send=AsyncMock(), message=SimpleNamespace(add_reaction=AsyncMock()))


def test_an_unexpected_error_in_a_command_is_counted_alerted_and_answered_in_the_channel(dms):
    ctx = error_ctx()
    run(botmod.on_command_error(ctx, commands.CommandInvokeError(RuntimeError("boom"))))
    assert totals() == {"error": 1}
    assert dms == [(BOB, ["⚠ PlayMoreBlitz: !results hit an unexpected error (RuntimeError: boom). The log has the details."])]
    assert ctx.send.await_args.args[0] == "something went wrong, check the logs"


def test_the_same_error_is_alerted_once_every_half_hour_but_always_counted(dms, clock):
    for _ in range(3):
        run(botmod.on_command_error(error_ctx(), commands.CommandInvokeError(RuntimeError("boom"))))
    assert len(dms) == 1 and totals() == {"error": 3}
    run(botmod.on_command_error(error_ctx(), commands.CommandInvokeError(KeyError("other"))))
    run(botmod.on_command_error(error_ctx("obit"), commands.CommandInvokeError(RuntimeError("boom"))))
    assert len(dms) == 3                                                                    # a different error, and a different command
    clock.now += monitoring.ERROR_REPEAT_SECONDS
    run(botmod.on_command_error(error_ctx(), commands.CommandInvokeError(RuntimeError("boom"))))
    assert len(dms) == 4


def test_a_plain_exception_with_no_command_is_still_handled(dms):
    ctx = error_ctx()
    ctx.command = None
    run(botmod.on_command_error(ctx, RuntimeError("boom")))
    assert "a command hit an unexpected error (RuntimeError: boom)" in dms[0][1][0]


@pytest.mark.parametrize("error", [
    commands.MissingRequiredArgument(SimpleNamespace(name="x", displayed_name="x")), commands.BadArgument("no"),
    commands.CommandOnCooldown(commands.Cooldown(1, 5), 3.0, commands.BucketType.user), commands.CommandNotFound("no"), commands.CheckFailure("no")])
def test_ordinary_refusals_are_neither_counted_as_errors_nor_alerted(dms, error):
    run(botmod.on_command_error(error_ctx(), error))
    assert totals() == {} and dms == []


def test_an_error_in_a_slash_command_is_counted_alerted_and_answered_privately(dms):
    interaction = SimpleNamespace(command=SimpleNamespace(name="obit"), response=SimpleNamespace(is_done=lambda: False, send_message=AsyncMock()),
                                  followup=SimpleNamespace(send=AsyncMock()))
    run(botmod.on_app_command_error(interaction, discord.app_commands.CommandInvokeError(SimpleNamespace(name="obit"), RuntimeError("boom"))))
    assert totals() == {"error": 1} and dms[0][1][0].startswith("⚠ PlayMoreBlitz: /obit hit an unexpected error (RuntimeError: boom)")
    interaction.response.send_message.assert_awaited_once_with("something went wrong, check the logs", ephemeral=True)


def test_a_slash_error_with_no_command_named_is_still_handled(dms):
    interaction = SimpleNamespace(command=None, response=SimpleNamespace(is_done=lambda: True, send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    run(botmod.on_app_command_error(interaction, RuntimeError("boom")))
    assert "a slash command hit an unexpected error" in dms[0][1][0]


# --- refreshes that keep failing ---------------------------------------------------------------------------------------------------------

def test_refresh_cycles_that_all_fail_are_reported_from_the_third_running_and_not_repeated_at_once(dms):
    for _ in range(2):
        run(botmod._track_refresh({"failed": 4}))
    assert dms == []
    run(botmod._track_refresh({"failed": 4}))
    run(botmod._track_refresh({"failed": 4}))
    assert [m for _, m in dms] == [["⚠ PlayMoreBlitz: The last 3 refresh cycles all failed: are Chess.com and Lichess reachable from the Minix?"]]


def test_one_good_cycle_starts_the_count_again_and_lets_a_return_be_reported_at_once(dms):
    for _ in range(3):
        run(botmod._track_refresh({"failed": 4}))
    run(botmod._track_refresh({"unchanged": 4}))
    assert botmod._health["refresh_failures"] == 0
    for _ in range(2):
        run(botmod._track_refresh({"failed": 4}))
    assert len(dms) == 1
    run(botmod._track_refresh({"failed": 4}))
    assert len(dms) == 2


def patch_the_refresh(monkeypatch, outcomes=None, error=None):
    monkeypatch.setattr(botmod.monthend, "close_due_months", AsyncMock(return_value=[]))
    monkeypatch.setattr(botmod.refresh, "refresh_all", AsyncMock(return_value=outcomes, side_effect=error))
    monkeypatch.setattr(botmod, "post_month_end_if_due", AsyncMock())


def test_the_refresh_loop_reports_failing_cycles_and_prunes_the_counts(dms, monkeypatch):
    patch_the_refresh(monkeypatch, {"failed": 4})
    pruned = []
    monkeypatch.setattr(botmod.usage_stats, "prune", lambda: pruned.append(1))
    for _ in range(3):
        run(botmod.refresh_loop.coro())
    assert len(dms) == 1 and "refresh cycles all failed" in dms[0][1][0] and pruned == [1, 1, 1]


def test_a_crashing_refresh_cycle_is_reported_and_the_loop_survives(dms, monkeypatch):
    patch_the_refresh(monkeypatch, error=RuntimeError("boom"))
    run(botmod.refresh_loop.coro())
    run(botmod.refresh_loop.coro())
    assert [m for _, m in dms] == [["⚠ PlayMoreBlitz: a refresh cycle crashed. The log has the details."]]


def test_a_crashing_review_delivery_is_reported(dms, monkeypatch):
    monkeypatch.setattr(botmod.obit, "outstanding", MagicMock(side_effect=RuntimeError("boom")))
    run(botmod.obit_loop.coro())
    assert dms[0][1] == ["⚠ PlayMoreBlitz: sending the reviews people asked for crashed. The log has the details."]


# --- the health loop -----------------------------------------------------------------------------------------------------------------------

def test_a_healthy_bot_sends_nothing(dms):
    run(botmod.health_loop.coro())
    assert dms == []


def stale_backup(tmp_path):
    folder = tmp_path / "backups"
    for f in folder.iterdir():
        f.unlink()
    (folder / f"playmoreblitz-{(date.today() - timedelta(days=3)).isoformat()}.db").write_bytes(b"x")


def test_a_missing_backup_is_reported_once_and_again_after_the_repeat_time(dms, clock, tmp_path):
    stale_backup(tmp_path)
    run(botmod.health_loop.coro())
    run(botmod.health_loop.coro())
    assert len(dms) == 1 and "The newest backup is from" in dms[0][1][0] and "3 days ago" in dms[0][1][0]
    clock.now += monitoring.REPEAT_SECONDS
    run(botmod.health_loop.coro())
    assert len(dms) == 2


def test_a_problem_that_clears_and_comes_back_is_reported_at_once(dms, tmp_path):
    stale_backup(tmp_path)
    run(botmod.health_loop.coro())
    (tmp_path / "backups" / f"playmoreblitz-{date.today().isoformat()}.db").write_bytes(b"x")   # the backup ran
    run(botmod.health_loop.coro())
    for f in (tmp_path / "backups").iterdir():
        f.unlink()
    (tmp_path / "backups" / f"playmoreblitz-{(date.today() - timedelta(days=3)).isoformat()}.db").write_bytes(b"x")
    run(botmod.health_loop.coro())
    assert len(dms) == 2


def test_a_silent_worker_with_games_waiting_is_reported_when_analysis_is_on(dms, monkeypatch):
    monkeypatch.setattr(settings, "ANALYSIS_ENABLED", True)
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example")], 1)
    run(botmod.health_loop.coro())
    assert len(dms) == 1 and "1 games are waiting to be analysed and no analysis worker has ever asked for work." in dms[0][1][0]


def test_the_worker_is_not_checked_when_analysis_is_off(dms, monkeypatch):
    monkeypatch.setattr(settings, "ANALYSIS_ENABLED", False)
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example")], 1)
    run(botmod.health_loop.coro())
    assert dms == []


def test_a_live_worker_and_an_empty_queue_are_fine(dms, monkeypatch):
    monkeypatch.setattr(settings, "ANALYSIS_ENABLED", True)
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example"))
    q.claim("desk", 1, int(botmod.time.time()))
    run(botmod.health_loop.coro())
    assert dms == []


def test_both_problems_at_once_are_two_messages(dms, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ANALYSIS_ENABLED", True)
    stale_backup(tmp_path)
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example")], 1)
    run(botmod.health_loop.coro())
    assert len(dms) == 2


def test_a_failing_check_is_logged_and_does_not_stop_the_loop(dms, monkeypatch, caplog):
    monkeypatch.setattr(botmod.monitoring, "backup_problem", MagicMock(side_effect=RuntimeError("boom")))
    with caplog.at_level("ERROR", logger="playmoreblitz"):
        run(botmod.health_loop.coro())
    assert "health check crashed" in caplog.text and dms == []


def test_the_health_loop_runs_every_five_minutes_and_the_heartbeat_every_minute():
    assert botmod.health_loop.minutes == 5 and botmod.heartbeat_loop.seconds == 60


# --- the heartbeat -------------------------------------------------------------------------------------------------------------------------

class FakeSession:
    """Stands in for aiohttp.ClientSession: records the address asked for, and answers with `outcome` (a status, or an exception)."""

    calls = []
    outcome = 200

    def __init__(self, **kwargs):
        self.timeout = kwargs.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url):
        FakeSession.calls.append(url)
        outcome = FakeSession.outcome
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)


class FakeResponse:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def heartbeat(monkeypatch):
    FakeSession.calls = []
    FakeSession.outcome = 200
    monkeypatch.setattr(botmod.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(settings, "HEARTBEAT_URL", "https://hc.example.test/ping/secret-token")
    return FakeSession


def test_the_heartbeat_pings_the_address(heartbeat, caplog):
    with caplog.at_level("WARNING", logger="playmoreblitz"):
        run(botmod.heartbeat_loop.coro())
    assert heartbeat.calls == ["https://hc.example.test/ping/secret-token"] and caplog.text == "" and botmod._health["heartbeat_failing"] is False


@pytest.mark.parametrize("outcome", [RuntimeError("no route"), asyncio.TimeoutError(), 500, 404])
def test_a_ping_that_fails_is_noted_once_without_the_address(heartbeat, caplog, outcome):
    heartbeat.outcome = outcome
    with caplog.at_level("WARNING", logger="playmoreblitz"):
        run(botmod.heartbeat_loop.coro())
        run(botmod.heartbeat_loop.coro())
    assert caplog.text.count("the heartbeat ping failed") == 1 and "secret-token" not in caplog.text and botmod._health["heartbeat_failing"] is True


def test_when_the_pings_get_through_again_a_new_failure_is_noted_again(heartbeat, caplog):
    heartbeat.outcome = 503
    with caplog.at_level("WARNING", logger="playmoreblitz"):
        run(botmod.heartbeat_loop.coro())
        heartbeat.outcome = 204
        run(botmod.heartbeat_loop.coro())
        assert botmod._health["heartbeat_failing"] is False
        heartbeat.outcome = 503
        run(botmod.heartbeat_loop.coro())
    assert caplog.text.count("the heartbeat ping failed") == 2


def test_a_ping_times_out_after_ten_seconds(heartbeat, monkeypatch):
    seen = []
    original = FakeSession.__init__

    def spy(self, **kwargs):
        seen.append(kwargs["timeout"].total)
        original(self, **kwargs)
    monkeypatch.setattr(FakeSession, "__init__", spy)
    run(botmod.heartbeat_loop.coro())
    assert seen == [10]


# --- starting the loops --------------------------------------------------------------------------------------------------------------------

def on_ready_with(monkeypatch, url):
    started = []
    for name in ("obit_loop", "refresh_loop", "daily_posts", "health_loop", "heartbeat_loop"):
        loop = getattr(botmod, name)
        monkeypatch.setattr(loop, "is_running", lambda: False)
        monkeypatch.setattr(loop, "start", lambda name=name: started.append(name))
    monkeypatch.setattr(botmod, "post_signup_call_if_due", AsyncMock())
    monkeypatch.setattr(botmod, "sync_slash_commands", AsyncMock())
    monkeypatch.setattr(botmod.bot, "add_view", lambda view: None)
    monkeypatch.setattr(settings, "HEARTBEAT_URL", url)
    run(botmod.on_ready())
    return started


def test_the_health_loop_always_starts_and_the_heartbeat_only_with_an_address(monkeypatch):
    assert "health_loop" in on_ready_with(monkeypatch, "") and "heartbeat_loop" not in on_ready_with(monkeypatch, "")
    assert "heartbeat_loop" in on_ready_with(monkeypatch, "https://hc.example.test/ping/x")


def test_a_loop_already_running_is_not_started_again(monkeypatch):
    started = []
    for name in ("obit_loop", "refresh_loop", "daily_posts", "health_loop", "heartbeat_loop"):
        loop = getattr(botmod, name)
        monkeypatch.setattr(loop, "is_running", lambda: True)
        monkeypatch.setattr(loop, "start", lambda name=name: started.append(name))
    monkeypatch.setattr(botmod, "sync_slash_commands", AsyncMock())
    monkeypatch.setattr(botmod.bot, "add_view", lambda view: None)
    monkeypatch.setattr(settings, "HEARTBEAT_URL", "https://hc.example.test/ping/x")
    run(botmod.on_ready())
    assert started == []


# --- counting reviews, exports and the slash commands --------------------------------------------------------------------------------------------

def test_a_review_sent_is_counted(dms):
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example"))
    botmod.obit.add_request(ALICE, "lichess", "00000001", "alice_example", 5, 10 ** 10)
    assert run(botmod._process_obit(botmod.obit.outstanding()[0], immediate=True)) == "sent"
    assert totals() == {usage.OBIT_SENT: 1}


def test_a_review_given_up_on_is_not_counted_as_sent(dms):
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example")], NOW)
    with store.transaction() as conn:
        conn.execute("UPDATE game_analysis SET status = 'failed'")
    botmod.obit.add_request(ALICE, "lichess", "00000001", "alice_example", 5, 10 ** 10)
    assert run(botmod._process_obit(botmod.obit.outstanding()[0], immediate=True)) == "closed"
    assert totals() == {}


def test_exports_sent_are_counted(monkeypatch):
    sent = []

    class FakeUser:
        async def send(self, content, **kwargs):
            sent.append(content)
    monkeypatch.setattr(botmod.bot, "get_user", lambda user_id: FakeUser())
    run(botmod._dm_parts(ALICE, [{"text": "a"}, {"text": "b"}]))
    assert sent == ["a", "b"] and totals() == {usage.EXPORT_SENT: 1}


def test_the_slash_commands_are_counted_as_the_commands_they_are(monkeypatch):
    monkeypatch.setattr(botmod, "ALLOWED_CHANNEL_IDS", {5})
    for command, name in ((botmod.obit_slash, "obit"), (botmod.export_slash, "export")):
        interaction = SimpleNamespace(user=SimpleNamespace(id=ALICE), channel_id=5, response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                                      followup=SimpleNamespace(send=AsyncMock()))
        run(command.callback(interaction))
    assert totals() == {"obit": 1, "export": 1} and usage.report(1)["days"][0]["people"] == 1


# --- !usage ----------------------------------------------------------------------------------------------------------------------------------

def test_usage_shows_the_last_seven_days_by_default(monkeypatch):
    usage.count("results", ALICE)
    ctx = SimpleNamespace(send=AsyncMock())
    run(botmod.usage_command.callback(ctx))
    text = ctx.send.await_args.args[0]
    assert text.startswith("**Usage, last 7 days**") and "Most used: results 1" in text


@pytest.mark.parametrize("asked, shown", [(3, 3), (0, 1), (-5, 1), (30, 30), (31, 30), (500, 30)])
def test_usage_takes_between_one_and_thirty_days(asked, shown):
    ctx = SimpleNamespace(send=AsyncMock())
    run(botmod.usage_command.callback(ctx, asked))
    assert ctx.send.await_args.args[0].startswith(f"**Usage, last {shown} day")


def test_usage_is_an_admin_command_for_a_dm_with_a_usage_line():
    assert botmod._admin_only in botmod.usage_command.checks and "usage" in botmod.ADMIN_DM_COMMANDS and botmod.USAGE["usage"] == "!usage [days]"
    readme = open(botmod.__file__.replace("bot.py", "README.md"), encoding="utf-8").read()
    assert any(line.startswith("| `!usage [days]` | Admins only, in a direct message to the bot |") for line in readme.splitlines())


# --- the settings -------------------------------------------------------------------------------------------------------------------------------

def test_the_default_recipient_is_bob_alone_and_the_heartbeat_is_off():
    assert settings.ALERT_USER_IDS == {810486671174795274} and settings.HEARTBEAT_URL == ""


def test_the_text_setting_is_trimmed_and_defaults_when_blank():
    assert settings.text("X", env={"X": "  https://a.test/b  "}) == "https://a.test/b"
    assert settings.text("X", env={"X": "   "}) == "" and settings.text("X", env={}) == "" and settings.text("X", "fallback", env={}) == "fallback"


def test_the_heartbeat_address_must_be_https_and_the_alert_list_valid():
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(settings.__file__).parent
    base = {k: v for k, v in os.environ.items() if k not in settings.NAMES}

    def run_settings(**env):
        return subprocess.run([sys.executable, "-c", "import settings as s, bot; print(sorted(s.ALERT_USER_IDS), s.HEARTBEAT_URL, sorted(bot.ALERT_USER_IDS))"],
                              capture_output=True, text=True, cwd=root, env={**base, **env})
    out = run_settings(ALERT_USER_IDS="5,6", HEARTBEAT_URL="https://hc.example.test/x")
    assert out.stdout.strip() == "[5, 6] https://hc.example.test/x [5, 6]", out.stderr             # and the bot uses that list, not the admins
    for bad in ("http://hc.example.test/x", "hc.example.test/x"):
        out = run_settings(HEARTBEAT_URL=bad)
        assert out.returncode != 0 and "HEARTBEAT_URL" in out.stderr and "https://" in out.stderr
    out = run_settings(ALERT_USER_IDS="nobody")
    assert out.returncode != 0 and "ALERT_USER_IDS" in out.stderr


def test_the_logs_note_nothing_when_all_is_well(dms, caplog):
    with caplog.at_level(logging.WARNING, logger="playmoreblitz"):
        run(botmod.health_loop.coro())
    assert caplog.text == ""

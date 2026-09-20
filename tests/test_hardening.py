"""Robustness: echoed text, missing Discord permissions, startup checks, the single-instance
lock, logging, and that the README and run files match the code."""

import asyncio
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import bot as botmod
import singleton
import sources
import store

ROOT = Path(__file__).parent.parent
OK, NO = "✅", "❌"
ALICE, ADMIN = 1001, min(botmod.ADMIN_USER_IDS)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(botmod, "_start_refresh", lambda *a: None)


def forbidden():
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def make_ctx(author=ALICE, *, react_error=None, send_error=None):
    return SimpleNamespace(
        author=SimpleNamespace(id=author),
        channel=SimpleNamespace(id=555),
        message=SimpleNamespace(add_reaction=AsyncMock(side_effect=react_error)),
        send=AsyncMock(side_effect=send_error),
        typing=lambda: Typing(),
        command=MagicMock(),
    )


def said(ctx):
    return [c.args[0] for c in ctx.send.await_args_list]


def run(coro):
    return asyncio.run(coro)


# --- echoing what a user typed ---------------------------------------------


def test_shorten_leaves_short_text_alone_and_cuts_long_text_with_an_ellipsis():
    assert sources.shorten("alice") == "alice"
    assert sources.shorten("x" * 40) == "x" * 40
    cut = sources.shorten("x" * 41)
    assert len(cut) == 40 and cut.endswith("…")
    assert len(sources.shorten("y" * 5000, limit=10)) == 10


def test_a_huge_bad_username_gives_a_short_message_not_one_over_the_discord_limit():
    with pytest.raises(sources.SourceError) as caught:
        sources.check_username("z" * 1990)
    assert len(str(caught.value)) < 200 and "…" in str(caught.value)


@pytest.mark.parametrize("command,args", [("remove", ()), ("mystats", ()), ("mystatsfull", ()), ("gob", ()), ("gob_next", ())])
def test_an_enormous_unknown_name_is_echoed_briefly_by_every_command_that_echoes(command, args):
    ctx = make_ctx()
    run(getattr(botmod, command).callback(ctx, "n" * 1900, *args))
    (reply,) = said(ctx)
    assert len(reply) < 200 and "isn't on the list" in reply and "…" in reply


# --- reactions the bot may not be allowed to add ------------------------------


def test_a_reaction_that_works_needs_no_fallback():
    ctx = make_ctx()
    run(botmod._react(ctx, OK, "Done."))
    assert ctx.message.add_reaction.await_count == 1 and said(ctx) == []


def test_a_forbidden_reaction_falls_back_to_words_and_logs_it(caplog):
    ctx = make_ctx(react_error=forbidden())
    with caplog.at_level("WARNING", logger="playmoreblitz"):
        run(botmod._react(ctx, OK, "Done."))
    assert said(ctx) == ["Done."]
    assert "couldn't add a reaction in channel 555" in caplog.text and "Add Reactions" in caplog.text


def test_a_forbidden_reaction_with_no_fallback_is_just_logged():
    ctx = make_ctx(react_error=forbidden())
    run(botmod._react(ctx, NO))
    assert said(ctx) == []


def test_a_command_that_succeeded_does_not_turn_into_an_error_when_the_tick_is_forbidden(monkeypatch):
    async def start_rating(session, site, username, month):
        return 1500

    async def account_name(session, site, username):
        return username

    monkeypatch.setattr(sources, "start_rating", start_rating)
    monkeypatch.setattr(sources, "account_name", account_name)
    ctx = make_ctx(react_error=forbidden())
    run(botmod.add.callback(ctx, "alice", "chess.com"))
    assert said(ctx) == ["Done."]  # the work was done and the outcome is still clear
    assert store.get_player("chess.com", "alice").active


def test_remove_and_100gob_also_say_done_when_the_tick_is_forbidden():
    store.add_player("chess.com", "alice", ALICE, sources.current_month(), 1500)
    ctx = make_ctx(react_error=forbidden())
    run(botmod.gob.callback(ctx))
    assert said(ctx) == ["Done."]
    ctx = make_ctx(react_error=forbidden())
    run(botmod.remove.callback(ctx, "alice"))
    assert said(ctx) == ["Done."]


def test_a_refusal_still_gives_its_reason_when_the_cross_is_forbidden():
    ctx = make_ctx(react_error=forbidden())
    run(botmod._reject(ctx, "no such thing"))
    assert said(ctx) == ["no such thing"]


def test_a_refusal_that_cannot_be_sent_does_not_crash_the_error_handler(caplog):
    ctx = make_ctx(send_error=forbidden())
    with caplog.at_level("WARNING", logger="playmoreblitz"):
        run(botmod._reject(ctx, "no such thing", refund_cooldown=True))  # must not raise
    assert "couldn't send a reply in channel 555" in caplog.text
    ctx.command.reset_cooldown.assert_called_once()  # and the cooldown is still refunded


# --- startup checks -----------------------------------------------------------


@pytest.fixture
def channel_visible(monkeypatch):
    monkeypatch.setattr(botmod.bot, "get_channel", lambda channel_id: object())


def test_a_correct_setup_has_nothing_to_warn_about(monkeypatch, channel_visible):
    monkeypatch.setattr(sources, "CONTACT", "https://example.test/issues")
    assert botmod.configuration_warnings() == []


def test_a_missing_contact_is_warned_about(monkeypatch, channel_visible):
    monkeypatch.setattr(sources, "CONTACT", "")
    (warning,) = botmod.configuration_warnings()
    assert "CONTACT is not set" in warning


def test_a_post_channel_that_is_not_an_allowed_channel_is_warned_about(monkeypatch, channel_visible):
    monkeypatch.setattr(sources, "CONTACT", "x")
    monkeypatch.setattr(botmod, "POST_CHANNEL_ID", 42)
    (warning,) = botmod.configuration_warnings()
    assert "POST_CHANNEL_ID 42" in warning and "ALLOWED_CHANNEL_IDS" in warning


def test_a_post_channel_the_bot_cannot_see_is_warned_about(monkeypatch):
    monkeypatch.setattr(sources, "CONTACT", "x")
    monkeypatch.setattr(botmod.bot, "get_channel", lambda channel_id: None)
    (warning,) = botmod.configuration_warnings()
    assert "can't see the post channel" in warning and "Send Messages" in warning


def test_every_problem_is_reported_not_just_the_first(monkeypatch):
    monkeypatch.setattr(sources, "CONTACT", "")
    monkeypatch.setattr(botmod, "POST_CHANNEL_ID", 42)
    monkeypatch.setattr(botmod.bot, "get_channel", lambda channel_id: None)
    assert len(botmod.configuration_warnings()) == 3


# --- only one copy at a time ----------------------------------------------------


def test_a_second_copy_refuses_to_start_and_says_why(tmp_path):
    lock = tmp_path / "playmoreblitz.lock"
    first = singleton.acquire(lock)
    try:
        with pytest.raises(SystemExit) as refused:
            singleton.acquire(lock)
        assert "already running" in str(refused.value) and "playmoreblitz.lock" in str(refused.value)
    finally:
        first.close()


def test_the_lock_is_released_when_the_first_copy_goes_away(tmp_path):
    lock = tmp_path / "playmoreblitz.lock"
    singleton.acquire(lock).close()
    singleton.acquire(lock).close()  # nothing left behind to block it


def test_on_linux_the_lock_is_a_non_blocking_exclusive_flock(monkeypatch, tmp_path):
    """The Linux branch can't run on a Windows PC, so a stand-in for fcntl checks what it asks for."""
    import sys

    calls = []

    class FakeFcntl:
        LOCK_EX, LOCK_NB = 2, 4

        @staticmethod
        def flock(handle, how):
            calls.append(how)

    monkeypatch.setitem(sys.modules, "fcntl", FakeFcntl)
    with open(tmp_path / "x.lock", "a+") as handle:
        singleton._lock_posix(handle)
    assert calls == [FakeFcntl.LOCK_EX | FakeFcntl.LOCK_NB]  # exclusive, and never waits


def test_on_linux_a_held_lock_makes_the_second_copy_exit(monkeypatch, tmp_path):
    def held(handle):
        raise BlockingIOError("Resource temporarily unavailable")

    monkeypatch.setattr(singleton, "os", SimpleNamespace(name="posix", path=singleton.os.path))
    monkeypatch.setattr(singleton, "_lock_posix", held)
    with pytest.raises(SystemExit) as refused:
        singleton.acquire(tmp_path / "playmoreblitz.lock")
    assert "already running" in str(refused.value)


def test_on_linux_a_free_lock_lets_the_copy_start(monkeypatch, tmp_path):
    monkeypatch.setattr(singleton, "os", SimpleNamespace(name="posix", path=singleton.os.path))
    monkeypatch.setattr(singleton, "_lock_posix", lambda handle: None)
    handle = singleton.acquire(tmp_path / "playmoreblitz.lock")
    assert not handle.closed
    handle.close()


def test_different_folders_can_each_run_their_own_copy(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = singleton.acquire(tmp_path / "a" / "x.lock")
    second = singleton.acquire(tmp_path / "b" / "x.lock")
    first.close()
    second.close()


# --- logging ----------------------------------------------------------------------


def test_the_voice_warnings_are_filtered_and_real_warnings_are_not():
    f = botmod._NoVoiceWarnings()
    voice = logging.LogRecord("discord.client", logging.WARNING, "", 0, "PyNaCl is not installed, voice will NOT be supported", (), None)
    real = logging.LogRecord("discord.client", logging.WARNING, "", 0, "something else went wrong", (), None)
    assert f.filter(voice) is False and f.filter(real) is True
    assert any(isinstance(x, botmod._NoVoiceWarnings) for x in logging.getLogger("discord.client").filters)


def test_the_bot_uses_its_own_logging_so_lines_are_not_printed_twice():
    source = (ROOT / "bot.py").read_text(encoding="utf-8")
    assert "bot.run(token, log_handler=None)" in source


# --- the docs and run files match the code -------------------------------------------


README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_the_readme_commands_table_has_a_row_for_every_command_with_its_aliases():
    rows = {m.group(1): m.group(0) for m in re.finditer(r"^\| `!(\w+)[^\n]*$", README, re.M)}  # the table's own rows
    for command in botmod.bot.commands:
        assert command.name in rows, f"the README commands table has no row for !{command.name}"
        for alias in command.aliases:
            assert f"!{alias}" in rows[command.name], f"!{command.name}'s row does not mention its alias !{alias}"
    assert set(rows) == {c.name for c in botmod.bot.commands}, "the table lists a command the bot doesn't have"


def test_the_readme_lists_every_setting_that_has_to_be_edited():
    for constant in ("ALLOWED_CHANNEL_IDS", "POST_CHANNEL_ID", "ADMIN_USER_IDS", "GOB_TARGET", "REFRESH_INTERVAL_MINUTES", "COOLDOWN_SECONDS"):
        assert constant in README and hasattr(botmod, constant), constant


def test_the_readme_gives_the_permission_number_the_code_and_docs_agree_on():
    permissions = 1024 + 2048 + 65536 + 64  # View Channels, Send Messages, Read Message History, Add Reactions
    assert permissions == 68672 and "68672" in README


def test_the_readme_names_no_real_accounts_or_ids_or_secrets():
    assert not re.search(r"\b\d{17,20}\b", README)  # no Discord IDs
    assert not re.search(r"[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}", README)  # no token-shaped text
    assert "@" not in README


def test_the_env_example_names_both_settings_and_holds_no_values():
    lines = [ln for ln in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines() if ln and not ln.startswith("#")]
    assert lines == ["DISCORD_TOKEN=", "CONTACT="]


def test_the_service_file_is_a_template_with_placeholders_not_a_real_install():
    unit = (ROOT / "playmoreblitz.service").read_text(encoding="utf-8")
    assert "Restart=always" in unit and "EnvironmentFile=" in unit and "bot.py" in unit
    assert re.findall(r"^User=(.*)$", unit, re.M) == ["CHANGEME"]  # no real user name
    home_paths = re.findall(r"=(/home/[^\s]*)", unit)
    assert home_paths and all(p.startswith("/home/CHANGEME/") for p in home_paths)  # and no real path


def test_every_dependency_is_pinned_to_an_exact_version():
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        if line.strip():
            assert re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][^=<>]*", line.strip()), line


def test_the_files_that_hold_secrets_or_local_data_are_ignored():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in (".env", "*.db", "venv/", "tests/fixtures_private/", "playmoreblitz.lock"):
        assert pattern in ignore, pattern
    assert "!.env.example" in ignore

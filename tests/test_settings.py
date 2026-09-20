"""Settings: defaults, .env overrides, refusing bad values, and the docs keeping in step."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import settings

ROOT = Path(settings.__file__).parent


def test_whole_numbers_read_the_environment_or_fall_back_to_the_default():
    assert settings.whole_number("X", 7, env={}) == 7
    assert settings.whole_number("X", 7, env={"X": ""}) == 7  # an empty line means "not set"
    assert settings.whole_number("X", 7, env={"X": " 12 "}) == 12


@pytest.mark.parametrize("bad", ["lots", "1.5", "0", "-3"])
def test_a_bad_whole_number_stops_the_bot_and_names_the_setting(bad):
    with pytest.raises(SystemExit) as stopped:
        settings.whole_number("GOB_TARGET", 100, env={"GOB_TARGET": bad})
    assert "GOB_TARGET" in str(stopped.value) and repr(bad) in str(stopped.value)


def test_a_minimum_of_zero_allows_zero_but_not_less():
    assert settings.whole_number("X", 600, minimum=0, env={"X": "0"}) == 0
    with pytest.raises(SystemExit):
        settings.whole_number("X", 600, minimum=0, env={"X": "-1"})


def test_seconds_accept_decimals_and_refuse_nonsense():
    assert settings.seconds("X", 2.0, env={"X": "0.5"}) == 0.5
    assert settings.seconds("X", 2.0, env={}) == 2.0
    for bad in ("soon", "0", "-2"):
        with pytest.raises(SystemExit):
            settings.seconds("X", 2.0, env={"X": bad})


def test_discord_ids_are_comma_separated_and_never_empty():
    assert settings.discord_ids("X", {1}, env={}) == {1}
    assert settings.discord_ids("X", {1}, env={"X": "5, 6,7"}) == {5, 6, 7}
    for bad in ("5,abc", ",", "0", "-4"):
        with pytest.raises(SystemExit) as stopped:
            settings.discord_ids("ADMIN_USER_IDS", {1}, env={"ADMIN_USER_IDS": bad})
        assert "ADMIN_USER_IDS" in str(stopped.value)


def _run(code, **env):
    base = {k: v for k, v in os.environ.items() if k not in settings.NAMES}
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, env={**base, **env})


def test_the_defaults_are_the_values_the_bot_has_always_used():
    out = _run("import settings as s; print(s.GOB_TARGET, s.REFRESH_INTERVAL_MINUTES, s.COOLDOWN_SECONDS, s.POST_HOUR_UK, "
               "s.CALL_DAYS_BEFORE, s.MIN_OPENING_GAMES, s.MIN_BEST_WORST_GAMES, s.SIMILAR_RATING_BAND, s.STATS_CACHE_PLAYERS, "
               "s.REQUEST_TIMEOUT_SECONDS, s.MONTH_TIMEOUT_SECONDS, s.MONTH_STALL_SECONDS, s.LICHESS_EXPORT_MIN_INTERVAL, s.DB_LOCK_TIMEOUT)")
    assert out.stdout.split() == "100 30 600 9 7 2 3 50 64 10.0 300.0 30.0 2.0 5.0".split(), out.stderr


def test_the_environment_reaches_every_module_that_uses_a_setting():
    code = ("import announce, bot, gamecache, render, sources, stats, store; "
            "print(bot.GOB_TARGET, sorted(bot.ALLOWED_CHANNEL_IDS), bot.POST_CHANNEL_ID, sorted(bot.ADMIN_USER_IDS), bot.COOLDOWN_SECONDS, "
            "bot.REFRESH_INTERVAL_MINUTES, announce.POST_TIME.hour, announce.CALL_DAYS_BEFORE, stats.MIN_OPENING_GAMES, "
            "stats.MIN_BEST_WORST_GAMES, stats.BAND, gamecache.MAX_PLAYERS, sources.REQUEST_TIMEOUT.total, sources.MONTH_TIMEOUT.total, "
            "sources.MONTH_TIMEOUT.sock_read, sources.LICHESS_EXPORT_MIN_INTERVAL, store.DB_LOCK_TIMEOUT, render.OPPONENT_LABELS['Similar'])")
    out = _run(code, GOB_TARGET="80", ALLOWED_CHANNEL_IDS="11,22", POST_CHANNEL_ID="22", ADMIN_USER_IDS="33", COOLDOWN_SECONDS="0",
               REFRESH_INTERVAL_MINUTES="15", POST_HOUR_UK="18", CALL_DAYS_BEFORE="3", MIN_OPENING_GAMES="4", MIN_BEST_WORST_GAMES="5",
               SIMILAR_RATING_BAND="75", STATS_CACHE_PLAYERS="8", REQUEST_TIMEOUT_SECONDS="20", MONTH_TIMEOUT_SECONDS="600",
               MONTH_STALL_SECONDS="45", LICHESS_EXPORT_MIN_INTERVAL="3", DB_LOCK_TIMEOUT="9")
    assert out.stdout.strip() == "80 [11, 22] 22 [33] 0 15 18 3 4 5 75 8 20.0 600.0 45.0 3.0 9.0 Similar (within 75)", out.stderr


def test_the_bot_refuses_to_start_with_a_bad_setting_and_says_which():
    out = _run("import bot", GOB_TARGET="lots")
    assert out.returncode != 0 and "GOB_TARGET" in out.stderr and "'lots'" in out.stderr
    out = _run("import bot", POST_HOUR_UK="24")
    assert out.returncode != 0 and "POST_HOUR_UK" in out.stderr


README = (ROOT / "README.md").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", settings.NAMES)
def test_every_setting_is_in_the_readme_and_the_env_example(name):
    assert f"`{name}`" in README, f"the README doesn't explain {name}"
    assert re.search(rf"^#? ?{name}=", ENV_EXAMPLE, re.M), f".env.example doesn't show {name}"


def test_the_list_of_names_matches_what_settings_py_actually_reads():
    source = (ROOT / "settings.py").read_text(encoding="utf-8")
    read = set(re.findall(r'(?:whole_number|seconds|discord_ids)\(\s*"([A-Z_]+)"', source)) | {"PLAYMOREBLITZ_DB"}
    assert read == set(settings.NAMES)

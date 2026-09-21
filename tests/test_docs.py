"""HOW_IT_WORKS.md is the map of the system, for whoever has to fix it. These tests keep it from drifting: if a command, a module or a
table is added and the map isn't updated, they fail, and say what is missing."""

import re
from pathlib import Path

import pytest

import bot as botmod
import store

ROOT = Path(__file__).resolve().parent.parent
DOC = (ROOT / "HOW_IT_WORKS.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


def modules():
    return sorted(p.name for p in ROOT.glob("*.py"))


@pytest.mark.parametrize("name", sorted(command.name for command in botmod.bot.commands if command.name != "help"))
def test_every_command_is_in_the_map(name):
    assert f"!{name}" in DOC, f"HOW_IT_WORKS.md doesn't mention !{name}"


@pytest.mark.parametrize("name", sorted(command.name for command in botmod.bot.tree.get_commands()))
def test_every_slash_command_is_in_the_map(name):
    assert f"/{name}" in DOC, f"HOW_IT_WORKS.md doesn't mention /{name}"


@pytest.mark.parametrize("filename", modules())
def test_every_module_is_in_the_map(filename):
    assert filename in DOC or filename.removesuffix(".py") in DOC, f"HOW_IT_WORKS.md doesn't mention {filename}"


@pytest.mark.parametrize("table", sorted(set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", store.SCHEMA))))
def test_every_table_is_in_the_map(table):
    assert f"`{table}`" in DOC, f"HOW_IT_WORKS.md doesn't describe the table {table}"


@pytest.mark.parametrize("loop", ["refresh_loop", "obit_loop", "health_loop", "heartbeat_loop", "daily_posts"])
def test_every_background_loop_is_in_the_map(loop):
    assert getattr(botmod, loop) is not None and f"`{loop}`" in DOC


def test_the_readme_points_to_the_map_and_the_map_keeps_private_details_out():
    assert "HOW_IT_WORKS.md" in README
    for private in (r"\bbob@", r"\b100\.\d+\.\d+\.\d+\b", r"\bC:\\Users\\bob\b", r"DISCORD_TOKEN=\S"):
        assert not re.search(private, DOC), f"the public map contains something private: {private}"

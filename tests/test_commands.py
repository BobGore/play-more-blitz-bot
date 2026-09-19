"""The !add and !remove commands, run directly against a fake Discord context.

The commands' Python functions are called as they are (command.callback), with a
stand-in for the message context and for the site lookups, so no Discord and no
network are involved. Cooldown and channel checks are separate discord.py layers
and are not exercised here.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot as botmod
import sources
import store

OK, NO = "✅", "❌"
ALICE, BOB, MATT_ADMIN = 1001, 1002, min(botmod.ADMIN_USER_IDS)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")


@pytest.fixture
def site(monkeypatch):
    """Fake site lookups: every account exists and starts at 1500 unless told otherwise."""
    state = {"rating": 1500, "error": None, "calls": [], "names": {}, "name_error": None}

    async def fake_start_rating(session, site_name, username, month):
        state["calls"].append((site_name, username, month))
        if state["error"]:
            raise state["error"]
        return state["rating"]

    async def fake_account_name(session, site_name, username):
        if state["name_error"]:
            raise state["name_error"]
        return state["names"].get(username.lower(), username)  # the site's spelling, if we set one

    monkeypatch.setattr(sources, "start_rating", fake_start_rating)
    monkeypatch.setattr(sources, "account_name", fake_account_name)
    return state


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def make_ctx(author_id):
    return SimpleNamespace(
        author=SimpleNamespace(id=author_id),
        message=SimpleNamespace(add_reaction=AsyncMock()),
        send=AsyncMock(),
        typing=lambda: Typing(),
        command=MagicMock(),
    )


def run(command, ctx, *args):
    asyncio.run(command.callback(ctx, *args))


def reactions(ctx):
    return [c.args[0] for c in ctx.message.add_reaction.await_args_list]


def said(ctx):
    return [c.args[0] for c in ctx.send.await_args_list]


# --- !add ------------------------------------------------------------------


def test_add_registers_the_player_for_the_caller_and_reacts_with_a_tick(site):
    ctx = make_ctx(ALICE)
    run(botmod.add, ctx, "alice_cc", "chess.com")
    assert reactions(ctx) == [OK] and said(ctx) == []  # no text reply on success
    player = store.get_player("chess.com", "alice_cc")
    assert (player.active, player.added_by) == (True, ALICE)
    row = store.month_row("chess.com", "alice_cc", sources.current_month())
    assert row["start_rating"] == 1500
    assert site["calls"] == [("chess.com", "alice_cc", sources.current_month())]


def test_add_stores_the_sites_own_spelling_not_what_was_typed(site):
    site["names"]["alice"] = "Alice"
    ctx = make_ctx(ALICE)
    run(botmod.add, ctx, "ALICE", "chess.com")
    assert reactions(ctx) == [OK]
    assert store.get_player("chess.com", "alice").username == "Alice"
    assert [p.username for p in store.active_players()] == ["Alice"]


def test_add_reports_a_failed_name_lookup_and_stores_nothing(site):
    site["name_error"] = sources.SourceError("couldn't reach chess.com (TimeoutError)")
    ctx = make_ctx(ALICE)
    run(botmod.add, ctx, "alice", "chess.com")
    assert reactions(ctx) == [NO] and "couldn't reach" in said(ctx)[0]
    assert store.get_player("chess.com", "alice") is None
    ctx.command.reset_cooldown.assert_called_once()


def test_add_site_is_case_insensitive(site):
    ctx = make_ctx(ALICE)
    run(botmod.add, ctx, "alice_li", "LiChess")
    assert reactions(ctx) == [OK]
    assert store.get_player("lichess", "alice_li") is not None


def test_add_rejects_an_unknown_site_without_calling_any_site(site):
    ctx = make_ctx(ALICE)
    run(botmod.add, ctx, "alice", "example.org")
    assert reactions(ctx) == [NO] and "chess.com" in said(ctx)[0]
    assert site["calls"] == []
    ctx.command.reset_cooldown.assert_called_once_with(ctx)  # a refused command doesn't burn the cooldown


def test_add_rejects_a_duplicate_before_calling_the_site(site):
    run(botmod.add, make_ctx(ALICE), "alice", "chess.com")
    site["calls"].clear()
    ctx = make_ctx(BOB)
    run(botmod.add, ctx, "ALICE", "chess.com")
    assert reactions(ctx) == [NO] and "already on the list" in said(ctx)[0]
    assert site["calls"] == []
    assert store.get_player("chess.com", "alice").added_by == ALICE


def test_add_reports_a_site_problem_and_stores_nothing(site):
    site["error"] = sources.NoSuchUser("no chess.com account 'ghost'")
    ctx = make_ctx(ALICE)
    run(botmod.add, ctx, "ghost", "chess.com")
    assert reactions(ctx) == [NO] and said(ctx) == ["no chess.com account 'ghost'"]
    assert store.get_player("chess.com", "ghost") is None
    ctx.command.reset_cooldown.assert_called_once()


@pytest.mark.parametrize("error", [sources.NoRating("'x' has no rated chess.com blitz games"), sources.SourceError("couldn't reach chess.com ()")])
def test_add_passes_on_any_source_error_message(site, error):
    site["error"] = error
    ctx = make_ctx(ALICE)
    run(botmod.add, ctx, "someone", "chess.com")
    assert said(ctx) == [str(error)] and reactions(ctx) == [NO]


def test_add_for_someone_else_needs_an_admin(site):
    ctx = make_ctx(ALICE)
    run(botmod.add, ctx, "friend", "chess.com", SimpleNamespace(id=BOB))
    assert reactions(ctx) == [NO] and "admin" in said(ctx)[0]
    assert store.get_player("chess.com", "friend") is None


def test_an_admin_can_add_someone_else_who_then_owns_it(site):
    ctx = make_ctx(MATT_ADMIN)
    run(botmod.add, ctx, "friend", "chess.com", SimpleNamespace(id=BOB))
    assert reactions(ctx) == [OK]
    assert store.get_player("chess.com", "friend").added_by == BOB


def test_naming_yourself_as_owner_is_not_adding_someone_else(site):
    ctx = make_ctx(ALICE)
    run(botmod.add, ctx, "me_again", "chess.com", SimpleNamespace(id=ALICE))
    assert reactions(ctx) == [OK]


def test_an_admins_rejection_does_not_try_to_refund_a_cooldown_they_never_had(site):
    ctx = make_ctx(MATT_ADMIN)
    run(botmod.add, ctx, "x_y", "example.org")
    assert reactions(ctx) == [NO]
    ctx.command.reset_cooldown.assert_not_called()  # admins have no bucket; resetting it would crash


def test_a_bad_username_from_the_site_layer_is_rejected(site):
    site["error"] = sources.SourceError("'../x' isn't a valid username (letters, numbers, - and _ only)")
    ctx = make_ctx(ALICE)
    run(botmod.add, ctx, "../x", "chess.com")
    assert reactions(ctx) == [NO] and "valid username" in said(ctx)[0]


def test_add_reactivates_a_removed_player_for_the_new_adder(site):
    run(botmod.add, make_ctx(ALICE), "alice", "chess.com")
    store.remove_player("chess.com", "alice")
    ctx = make_ctx(BOB)
    run(botmod.add, ctx, "alice", "chess.com")
    assert reactions(ctx) == [OK]
    player = store.get_player("chess.com", "alice")
    assert (player.active, player.added_by) == (True, BOB)


def test_someone_else_adding_the_same_player_at_the_same_moment_is_reported_not_crashed(site, monkeypatch):
    # Between our "is it already there?" check and the write, another add wins.
    real_add = store.add_player

    def racing_add(site_name, username, owner, month, start):
        real_add(site_name, username, BOB, month, start)  # the other add lands first
        return real_add(site_name, username, owner, month, start)

    monkeypatch.setattr(store, "add_player", racing_add)
    ctx = make_ctx(ALICE)
    run(botmod.add, ctx, "raced", "chess.com")
    assert reactions(ctx) == [NO] and "already on the list" in said(ctx)[0]
    assert store.get_player("chess.com", "raced").added_by == BOB


# --- !remove ---------------------------------------------------------------


def added(owner, username="alice", site_name="chess.com"):
    store.add_player(site_name, username, owner, "2026-09", 1500)


def test_the_owner_can_remove_their_player(site):
    added(ALICE)
    ctx = make_ctx(ALICE)
    run(botmod.remove, ctx, "alice")
    assert reactions(ctx) == [OK] and said(ctx) == []
    assert store.find_active("alice") == []
    assert store.month_row("chess.com", "alice", "2026-09") is not None  # history kept


def test_removal_is_case_insensitive(site):
    added(ALICE)
    ctx = make_ctx(ALICE)
    run(botmod.remove, ctx, "ALICE")
    assert reactions(ctx) == [OK]


def test_someone_else_cannot_remove_it(site):
    added(ALICE)
    ctx = make_ctx(BOB)
    run(botmod.remove, ctx, "alice")
    assert reactions(ctx) == [NO] and "only whoever added" in said(ctx)[0]
    assert store.find_active("alice") != []


def test_an_admin_can_remove_anyones_player(site):
    added(ALICE)
    ctx = make_ctx(MATT_ADMIN)
    run(botmod.remove, ctx, "alice")
    assert reactions(ctx) == [OK]
    assert store.find_active("alice") == []


def test_removing_someone_not_on_the_list_is_rejected(site):
    ctx = make_ctx(ALICE)
    run(botmod.remove, ctx, "nobody")
    assert reactions(ctx) == [NO] and "isn't on the list" in said(ctx)[0]


def test_removing_a_player_already_removed_is_rejected(site):
    added(ALICE)
    store.remove_player("chess.com", "alice")
    ctx = make_ctx(ALICE)
    run(botmod.remove, ctx, "alice")
    assert reactions(ctx) == [NO] and "isn't on the list" in said(ctx)[0]


def test_a_name_on_both_sites_asks_which(site):
    added(ALICE, site_name="chess.com")
    added(ALICE, site_name="lichess")
    ctx = make_ctx(ALICE)
    run(botmod.remove, ctx, "alice")
    assert reactions(ctx) == [NO]
    assert "chess.com and lichess" in said(ctx)[0] and "!remove alice chess.com" in said(ctx)[0]
    assert len(store.find_active("alice")) == 2  # nothing removed


def test_naming_the_site_removes_only_that_one(site):
    added(ALICE, site_name="chess.com")
    added(ALICE, site_name="lichess")
    ctx = make_ctx(ALICE)
    run(botmod.remove, ctx, "alice", "LICHESS")
    assert reactions(ctx) == [OK]
    assert [p.site for p in store.find_active("alice")] == ["chess.com"]


def test_remove_rejects_an_unknown_site(site):
    added(ALICE)
    ctx = make_ctx(ALICE)
    run(botmod.remove, ctx, "alice", "example.org")
    assert reactions(ctx) == [NO] and "chess.com" in said(ctx)[0]
    assert store.find_active("alice") != []


# --- error handler ---------------------------------------------------------


def test_a_missing_argument_gets_the_commands_usage():
    from discord.ext import commands

    ctx = make_ctx(ALICE)
    ctx.command = SimpleNamespace(name="add")
    asyncio.run(botmod.on_command_error(ctx, commands.MissingRequiredArgument(SimpleNamespace(name="site", displayed_name=None))))
    assert reactions(ctx) == [NO] and "!add <username> <site>" in said(ctx)[0]


def test_commands_from_outside_the_allowed_channels_are_ignored_silently():
    ctx = SimpleNamespace(channel=SimpleNamespace(id=1))
    assert asyncio.run(botmod._in_allowed_channel(ctx)) is False
    ctx = SimpleNamespace(channel=SimpleNamespace(id=min(botmod.ALLOWED_CHANNEL_IDS)))
    assert asyncio.run(botmod._in_allowed_channel(ctx)) is True

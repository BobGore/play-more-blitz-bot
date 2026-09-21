""""!setowner: an admin hands an account they registered to the member it belongs to, so that "my account" (as !obit and
!mystats use it) means the member's own."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot as botmod
import obit
import store

OK, NO = "✅", "❌"
ADMIN = min(botmod.ADMIN_USER_IDS)
ALICE, BOB = 1001, 1002
MONTH = "2026-09"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")


def make_ctx(author_id=ADMIN):
    return SimpleNamespace(author=SimpleNamespace(id=author_id), channel=SimpleNamespace(id=1), message=SimpleNamespace(add_reaction=AsyncMock()),
                           send=AsyncMock(), command=MagicMock())


def run(ctx, *args):
    asyncio.run(botmod.setowner.callback(ctx, *args))


def said(ctx):
    return [c.args[0] for c in ctx.send.await_args_list]


def reactions(ctx):
    return [c.args[0] for c in ctx.message.add_reaction.await_args_list]


def member(user_id):
    return SimpleNamespace(id=user_id)


def register(name, site="lichess", owner=ADMIN):
    store.add_player(site, name, owner, MONTH, 1500)


def owner_of(name, site="lichess"):
    return store.get_player(site, name).added_by


def test_the_command_is_admin_only_and_its_usage_is_known():
    assert botmod._admin_only in botmod.setowner.checks
    assert botmod.USAGE["setowner"] == "!setowner <username> <@member> [site]"


def test_an_account_the_admin_registered_is_handed_to_its_member():
    register("alice_example")
    register("bob_example")
    ctx = make_ctx()
    run(ctx, "alice_example", member(ALICE))
    assert reactions(ctx) == [OK] and said(ctx) == []
    assert owner_of("alice_example") == ALICE and owner_of("bob_example") == ADMIN          # only that one account moved


def test_after_the_hand_over_my_account_means_the_members_own_for_obit():
    register("alice_example")
    register("bob_example")
    register("admin_example")
    run(make_ctx(), "alice_example", member(ALICE))
    run(make_ctx(), "bob_example", member(BOB))
    assert [p.username for p in store.accounts_of(ADMIN)] == ["admin_example"]
    assert [p.username for p in store.accounts_of(ALICE)] == ["alice_example"]
    assert obit.find_game(store.accounts_of(ALICE), [("lichess", "00000001")]) is None


def test_the_site_can_be_given_and_is_needed_when_the_name_is_on_both():
    register("same_name", "lichess")
    register("same_name", "chess.com")
    ctx = make_ctx()
    run(ctx, "same_name", member(ALICE))
    assert reactions(ctx) == [NO] and "is on the list for chess.com and lichess" in said(ctx)[0] and "`!setowner same_name @member chess.com`" in said(ctx)[0]
    assert owner_of("same_name", "lichess") == ADMIN and owner_of("same_name", "chess.com") == ADMIN
    ctx = make_ctx()
    run(ctx, "same_name", member(ALICE), "CHESS.COM")
    assert reactions(ctx) == [OK] and owner_of("same_name", "chess.com") == ALICE and owner_of("same_name", "lichess") == ADMIN


def test_an_unknown_name_or_site_is_refused():
    register("alice_example")
    ctx = make_ctx()
    run(ctx, "nobody_example", member(ALICE))
    assert reactions(ctx) == [NO] and said(ctx) == ["'nobody_example' isn't on the list"]
    ctx = make_ctx()
    run(ctx, "alice_example", member(ALICE), "myspace")
    assert reactions(ctx) == [NO] and "the site must be" in said(ctx)[0] and owner_of("alice_example") == ADMIN
    ctx = make_ctx()
    run(ctx, "alice_example", member(ALICE), "chess.com")                                       # on the list, but for the other site
    assert reactions(ctx) == [NO] and "isn't on the list" in said(ctx)[0]


def test_a_removed_account_is_not_found():
    register("alice_example")
    store.remove_player("lichess", "alice_example")
    ctx = make_ctx()
    run(ctx, "alice_example", member(ALICE))
    assert reactions(ctx) == [NO] and "isn't on the list" in said(ctx)[0]


def test_already_theirs_says_so_and_changes_nothing():
    register("alice_example", owner=ALICE)
    ctx = make_ctx()
    run(ctx, "alice_example", member(ALICE))
    assert reactions(ctx) == [NO] and said(ctx) == ["alice_example already belongs to that member"]


def test_a_member_keeps_to_one_account_per_site_but_an_admin_may_hold_several():
    register("alice_example", owner=ALICE)
    register("alice_alt")
    ctx = make_ctx()
    run(ctx, "alice_alt", member(ALICE))
    assert reactions(ctx) == [NO] and "one per site" in said(ctx)[0] and owner_of("alice_alt") == ADMIN
    register("alice_cc", "chess.com")
    run(make_ctx(), "alice_cc", member(ALICE))                                                  # a different site is fine
    assert owner_of("alice_cc", "chess.com") == ALICE
    other_admin = max(botmod.ADMIN_USER_IDS)
    if other_admin != ADMIN:
        register("spare_one", owner=other_admin)
        register("spare_two")
        ctx = make_ctx()
        run(ctx, "spare_two", member(other_admin))
        assert reactions(ctx) == [OK] and owner_of("spare_two") == other_admin


def test_the_admin_can_take_an_account_back():
    register("alice_example", owner=ALICE)
    ctx = make_ctx()
    run(ctx, "alice_example", member(ADMIN))
    assert reactions(ctx) == [OK] and owner_of("alice_example") == ADMIN


def test_the_store_reports_each_outcome_and_leaves_the_month_rows_alone():
    register("alice_example")
    assert store.set_owner("lichess", "alice_example", ALICE) == store.CHANGED
    assert store.set_owner("lichess", "alice_example", ALICE) == store.UNCHANGED
    assert store.set_owner("lichess", "nobody", ALICE) == store.MISSING
    assert store.set_owner("chess.com", "alice_example", ALICE) == store.MISSING
    register("alice_two")
    assert store.set_owner("lichess", "alice_two", ALICE, one_per_site=True) == store.LIMIT
    assert store.set_owner("lichess", "alice_two", ALICE) == store.CHANGED                       # without the rule it goes through
    assert store.month_row("lichess", "alice_example", MONTH) is not None
    assert store.set_owner("lichess", "ALICE_EXAMPLE", BOB) == store.CHANGED                     # the name is matched ignoring case


def test_a_removed_account_is_not_handed_over_and_does_not_count_against_the_new_owner():
    register("gone_example")
    store.remove_player("lichess", "gone_example")
    assert store.set_owner("lichess", "gone_example", ALICE) == store.MISSING and owner_of("gone_example") == ADMIN
    register("alice_old", owner=ALICE)
    store.remove_player("lichess", "alice_old")                                                  # her old account was removed
    register("alice_new")
    assert store.set_owner("lichess", "alice_new", ALICE, one_per_site=True) == store.CHANGED

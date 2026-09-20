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


@pytest.fixture(autouse=True)
def refreshes(monkeypatch):
    """Record the background refreshes !add asks for instead of really starting them."""
    started = []
    monkeypatch.setattr(botmod, "_start_refresh", lambda site_name, username: started.append((site_name, username)))
    return started


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


def test_a_successful_add_starts_a_refresh_for_that_player_so_they_show_up_promptly(site, refreshes):
    site["names"]["alice"] = "Alice"
    run(botmod.add, make_ctx(ALICE), "ALICE", "chess.com")
    assert refreshes == [("chess.com", "Alice")]  # under the site's spelling


def test_a_refused_add_starts_no_refresh(site, refreshes):
    site["error"] = sources.NoSuchUser("no chess.com account 'ghost'")
    run(botmod.add, make_ctx(ALICE), "ghost", "chess.com")
    run(botmod.add, make_ctx(ALICE), "alice", "example.org")
    assert refreshes == []


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


# --- !100gob ---------------------------------------------------------------


def registered(owner, username="alice", site_name="chess.com", month=None):
    store.add_player(site_name, username, owner, month or sources.current_month(), 1500)


def in_challenge(username="alice", site_name="chess.com"):
    return bool(store.month_row(site_name, username, sources.current_month())["in_100gob"])


def test_100gob_with_one_account_opts_it_in_with_a_tick_and_no_text():
    registered(ALICE)
    ctx = make_ctx(ALICE)
    run(botmod.gob, ctx)
    assert reactions(ctx) == [OK] and said(ctx) == []
    assert in_challenge()


def test_100gob_is_the_command_name_and_case_does_not_matter():
    assert botmod.bot.get_command("100gob") is botmod.gob
    assert botmod.bot.get_command("100GOB") is botmod.gob
    assert botmod.bot.get_command("100Gob") is botmod.gob


def test_100gob_with_no_account_says_to_add_one_first():
    ctx = make_ctx(ALICE)
    run(botmod.gob, ctx)
    assert reactions(ctx) == [NO] and "!add" in said(ctx)[0]


def test_100gob_with_two_accounts_asks_which_and_changes_nothing():
    registered(ALICE, "alice_cc", "chess.com")
    registered(ALICE, "alice_li", "lichess")
    ctx = make_ctx(ALICE)
    run(botmod.gob, ctx)
    assert reactions(ctx) == [NO]
    reply = said(ctx)[0]
    assert "alice_cc (chess.com)" in reply and "alice_li (lichess)" in reply and "`!100gob alice_cc chess.com`" in reply
    assert not in_challenge("alice_cc") and not in_challenge("alice_li", "lichess")


def test_100gob_with_a_username_picks_that_account():
    registered(ALICE, "alice_cc", "chess.com")
    registered(ALICE, "alice_li", "lichess")
    ctx = make_ctx(ALICE)
    run(botmod.gob, ctx, "alice_li")
    assert reactions(ctx) == [OK]
    assert in_challenge("alice_li", "lichess") and not in_challenge("alice_cc")


def test_100gob_username_is_case_insensitive():
    registered(ALICE, "Alice")
    ctx = make_ctx(ALICE)
    run(botmod.gob, ctx, "ALICE")
    assert reactions(ctx) == [OK] and in_challenge("Alice")


def test_100gob_for_someone_elses_account_needs_an_admin():
    registered(ALICE)
    ctx = make_ctx(BOB)
    run(botmod.gob, ctx, "alice")
    assert reactions(ctx) == [NO] and "only whoever added" in said(ctx)[0]
    assert not in_challenge()


def test_an_admin_can_put_anyone_in_100gob():
    registered(ALICE)
    ctx = make_ctx(MATT_ADMIN)
    run(botmod.gob, ctx, "alice")
    assert reactions(ctx) == [OK] and in_challenge()


def test_100gob_for_an_unknown_or_removed_account():
    registered(ALICE)
    store.remove_player("chess.com", "alice")
    for name in ("alice", "nobody"):
        ctx = make_ctx(ALICE)
        run(botmod.gob, ctx, name)
        assert reactions(ctx) == [NO] and "isn't on the list" in said(ctx)[0]


def test_100gob_twice_in_a_month_is_refused_kindly():
    registered(ALICE)
    run(botmod.gob, make_ctx(ALICE))
    ctx = make_ctx(ALICE)
    run(botmod.gob, ctx)
    assert reactions(ctx) == [NO] and "already in 100GOB" in said(ctx)[0]
    assert in_challenge()  # still in


def test_100gob_before_the_months_row_exists_keeps_the_sign_up_and_ticks():
    # As in the first hours of a new month, before its row is created: only an old row exists.
    registered(ALICE, month="2020-01")
    ctx = make_ctx(ALICE)
    run(botmod.gob, ctx)
    assert reactions(ctx) == [OK] and said(ctx) == []
    assert store.signups(sources.current_month()) == ["alice"]  # applied when the row is created


def test_100gob_for_a_closed_month_says_so():
    registered(ALICE)
    with store._transaction() as conn:
        conn.execute("UPDATE monthly_results SET closed_at = '2000-01-01T00:00:00+00:00'")
    ctx = make_ctx(ALICE)
    run(botmod.gob, ctx)
    assert reactions(ctx) == [NO] and "already closed" in said(ctx)[0]


# --- !100gobnext -----------------------------------------------------------


def next_month():
    return sources.next_month(sources.current_month())


def test_100gobnext_signs_up_for_the_month_after_the_current_one_and_leaves_this_month_alone():
    registered(ALICE)
    ctx = make_ctx(ALICE)
    run(botmod.gob_next, ctx)
    assert reactions(ctx) == [OK] and said(ctx) == []
    assert store.signups(next_month()) == ["alice"]
    assert not in_challenge()  # this month untouched


def test_100gobnext_and_100gob_are_independent_and_both_can_be_used():
    registered(ALICE)
    run(botmod.gob, make_ctx(ALICE))
    ctx = make_ctx(ALICE)
    run(botmod.gob_next, ctx)
    assert reactions(ctx) == [OK]
    assert in_challenge() and store.signups(next_month()) == ["alice"]


def test_100gobnext_twice_says_already_signed_up_and_names_the_month():
    registered(ALICE)
    run(botmod.gob_next, make_ctx(ALICE))
    ctx = make_ctx(ALICE)
    run(botmod.gob_next, ctx)
    import render

    assert reactions(ctx) == [NO]
    assert f"already in 100GOB for {render.month_title(next_month())}" in said(ctx)[0]


def test_100gobnext_is_a_command_and_case_does_not_matter():
    assert botmod.bot.get_command("100gobnext") is botmod.gob_next
    assert botmod.bot.get_command("100GOBNEXT") is botmod.gob_next


def test_100gobnext_with_two_accounts_asks_which_using_its_own_command_name():
    registered(ALICE, "alice_cc", "chess.com")
    registered(ALICE, "alice_li", "lichess")
    ctx = make_ctx(ALICE)
    run(botmod.gob_next, ctx)
    assert reactions(ctx) == [NO] and "`!100gobnext alice_cc chess.com`" in said(ctx)[0]
    assert store.signups(next_month()) == []


def test_100gobnext_follows_the_same_ownership_rule():
    registered(ALICE)
    ctx = make_ctx(BOB)
    run(botmod.gob_next, ctx, "alice")
    assert reactions(ctx) == [NO] and "only whoever added" in said(ctx)[0]
    assert store.signups(next_month()) == []
    ctx = make_ctx(MATT_ADMIN)
    run(botmod.gob_next, ctx, "alice")
    assert reactions(ctx) == [OK] and store.signups(next_month()) == ["alice"]


def test_100gobnext_with_no_account_says_to_add_one_first():
    ctx = make_ctx(ALICE)
    run(botmod.gob_next, ctx)
    assert reactions(ctx) == [NO] and "!add" in said(ctx)[0]


def test_results_lists_who_has_signed_up_for_next_month():
    registered(ALICE, "Alice")
    registered(BOB, "bob")
    run(botmod.gob_next, make_ctx(ALICE), "Alice")
    ctx = make_ctx(BOB)
    run(botmod.results, ctx)
    text = "\n".join(said(ctx))
    import render

    assert f"100GOB sign-ups for {render.month_title(next_month())}: `Alice`" in text
    assert "`bob`" not in text.split("100GOB sign-ups")[1]


def test_results_has_no_signup_line_when_nobody_has_signed_up():
    registered(ALICE)
    ctx = make_ctx(ALICE)
    run(botmod.results, ctx)
    assert "sign-ups" not in "\n".join(said(ctx))


# --- the scheduled sign-up call --------------------------------------------


class FakeChannel:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send(self, text):
        if self.fail:
            raise RuntimeError("Missing Access")
        self.sent.append(text)


@pytest.fixture
def call(monkeypatch):
    """Make the sign-up call due for October, and capture where and what would be posted."""
    state = {"channel": FakeChannel(), "asked_for": []}
    monkeypatch.setattr(botmod.announce, "signup_call_due", lambda now: "2026-10")

    def get_channel(channel_id):
        state["asked_for"].append(channel_id)
        return state["channel"]

    monkeypatch.setattr(botmod.bot, "get_channel", get_channel)
    return state


def test_the_signup_call_is_posted_to_the_configured_channel(call):
    assert asyncio.run(botmod.post_signup_call_if_due()) is True
    assert call["asked_for"] == [botmod.POST_CHANNEL_ID]
    assert len(call["channel"].sent) == 1 and "October 2026" in call["channel"].sent[0]


def test_the_signup_call_is_posted_once_even_across_restarts(call):
    assert asyncio.run(botmod.post_signup_call_if_due()) is True
    assert asyncio.run(botmod.post_signup_call_if_due()) is False  # e.g. the bot restarted
    assert len(call["channel"].sent) == 1


def test_a_failed_post_gives_the_claim_back_so_the_next_try_posts_it(call):
    call["channel"].fail = True
    assert asyncio.run(botmod.post_signup_call_if_due()) is False
    call["channel"].fail = False
    assert asyncio.run(botmod.post_signup_call_if_due()) is True
    assert len(call["channel"].sent) == 1


def test_nothing_is_posted_when_the_call_is_not_due(call, monkeypatch):
    monkeypatch.setattr(botmod.announce, "signup_call_due", lambda now: None)
    assert asyncio.run(botmod.post_signup_call_if_due()) is False
    assert call["channel"].sent == [] and call["asked_for"] == []


def test_the_scheduled_task_runs_at_9am_uk_time():
    assert botmod.daily_posts.time == [botmod.announce.POST_TIME]


def test_100gob_when_a_name_is_on_both_sites_asks_which_and_a_site_settles_it():
    registered(ALICE, "alice", "chess.com")
    registered(ALICE, "alice", "lichess")
    ctx = make_ctx(ALICE)
    run(botmod.gob, ctx, "alice")
    assert reactions(ctx) == [NO] and "chess.com and lichess" in said(ctx)[0]
    assert not in_challenge("alice", "chess.com") and not in_challenge("alice", "lichess")

    ctx = make_ctx(ALICE)
    run(botmod.gob, ctx, "alice", "LICHESS")
    assert reactions(ctx) == [OK]
    assert in_challenge("alice", "lichess") and not in_challenge("alice", "chess.com")


def test_100gob_rejects_an_unknown_site():
    registered(ALICE)
    ctx = make_ctx(ALICE)
    run(botmod.gob, ctx, "alice", "example.org")
    assert reactions(ctx) == [NO] and "chess.com" in said(ctx)[0]
    assert not in_challenge()


def test_100gob_makes_no_calls_to_the_chess_sites_and_starts_no_refresh(site, refreshes):
    registered(ALICE)
    run(botmod.gob, make_ctx(ALICE))
    assert site["calls"] == [] and refreshes == []


def test_joining_shows_up_in_the_results_table_as_progress():
    import render
    from datetime import datetime, timezone

    registered(ALICE)
    run(botmod.gob, make_ctx(ALICE))
    month = sources.current_month()
    (table,) = render.render_results(store.results(month), month, datetime.now(timezone.utc), botmod.GOB_TARGET)
    assert "100GOB 0/100" in table


def test_a_missing_argument_style_error_gets_the_100gob_usage():
    from discord.ext import commands

    ctx = make_ctx(ALICE)
    ctx.command = SimpleNamespace(name="100gob")
    asyncio.run(botmod.on_command_error(ctx, commands.BadArgument("x")))
    assert "!100gob [username] [site]" in said(ctx)[0]


# --- !mystats and !mystatsfull ---------------------------------------------


@pytest.fixture
def played(monkeypatch):
    """Fake the game cache: state["games"] is what any player's month contains."""
    from helpers import at, game

    state = {
        "calls": [],
        "error": None,
        "games": [
            game("W", when=at(2, 10), rating_after=1510, colour="white", opening="London-System", opponent="rival_a", opponent_rating=1550),
            game("L", when=at(2, 11), rating_after=1495, colour="white", opening="London-System", opponent="rival_b", opponent_rating=1400),
            game("D", when=at(3, 22), rating_after=1495, colour="black", opening="Caro-Kann-Defense", opponent="rival_c", opponent_rating=1500),
        ],
    }

    async def fake_month_games(session, site_name, username, month):
        state["calls"].append((site_name, username, month))
        if state["error"]:
            raise state["error"]
        return list(state["games"])

    monkeypatch.setattr(botmod.gamecache, "month_games", fake_month_games)
    return state


def test_mystats_with_no_name_shows_the_callers_own_account(played):
    registered(ALICE, "Alice")
    ctx = make_ctx(ALICE)
    run(botmod.mystats, ctx)
    text = "\n".join(said(ctx))
    assert reactions(ctx) == []
    assert "`Alice` · Chess.com" in text and "Games 3    W 1   D 1   L 1" in text
    assert "**As White**" in text and "London System" in text
    assert played["calls"] == [("chess.com", "Alice", sources.current_month())]


def test_mystats_says_there_is_no_best_or_worst_opening_yet_when_no_opening_has_three_games(played):
    registered(ALICE, "Alice")  # the sample month has at most two games in any opening
    ctx = make_ctx(ALICE)
    run(botmod.mystats, ctx)
    assert "No opening has 3+ games yet, so no best or worst." in "\n".join(said(ctx))


def test_mystats_names_the_best_and_worst_opening_once_there_are_enough_games(played):
    from helpers import at, game

    played["games"] = (
        [game("W", colour="white", opening="Scotch-Game", when=at(1, i + 1), rating_after=1500) for i in range(3)]
        + [game("L", colour="white", opening="London-System", when=at(2, i + 1), rating_after=1500) for i in range(3)]
    )
    registered(ALICE, "Alice")
    ctx = make_ctx(ALICE)
    run(botmod.mystats, ctx)
    assert "Best: Scotch Game 100% (3 games) · Worst: London System 0% (3 games)" in "\n".join(said(ctx))


def test_mystatsfull_does_not_carry_the_opening_verdict(played):
    registered(ALICE, "Alice")
    ctx = make_ctx(ALICE)
    run(botmod.mystatsfull, ctx)
    assert "Best:" not in "\n".join(said(ctx)) and "best or worst" not in "\n".join(said(ctx))


def test_anyone_can_look_at_any_registered_player_by_name_which_is_the_friend_check(played):
    registered(ALICE, "Alice")
    ctx = make_ctx(BOB)  # Bob added nothing, and isn't an admin
    run(botmod.mystats, ctx, "alice")
    assert "`Alice`" in "\n".join(said(ctx)) and reactions(ctx) == []


def test_mystatsfull_shows_records_and_splits(played):
    registered(ALICE, "Alice")
    ctx = make_ctx(ALICE)
    run(botmod.mystatsfull, ctx)
    text = "\n".join(said(ctx))
    assert "· full" in text and "Best win" in text and "1550  rival_a" in text
    for title in ("By opponent rating", "By colour", "By weekday", "By time of day (UTC)"):
        assert f"**{title}**" in text


def test_the_short_names_are_the_same_commands():
    assert botmod.bot.get_command("stats") is botmod.mystats
    assert botmod.bot.get_command("statsfull") is botmod.mystatsfull
    assert botmod.bot.get_command("MyStats") is botmod.mystats
    assert botmod.bot.get_command("MYSTATSFULL") is botmod.mystatsfull


def test_mystats_with_two_accounts_asks_which_using_its_own_name(played):
    registered(ALICE, "alice_cc", "chess.com")
    registered(ALICE, "alice_li", "lichess")
    ctx = make_ctx(ALICE)
    run(botmod.mystats, ctx)
    assert reactions(ctx) == [NO] and "`!mystats alice_cc chess.com`" in said(ctx)[0]
    assert played["calls"] == []  # and no site was contacted
    ctx.command.reset_cooldown.assert_called_once()  # a question isn't a use


def test_mystatsfull_asks_with_its_own_command_name_too(played):
    registered(ALICE, "alice_cc", "chess.com")
    registered(ALICE, "alice_li", "lichess")
    ctx = make_ctx(ALICE)
    run(botmod.mystatsfull, ctx)
    assert "`!mystatsfull alice_cc chess.com`" in said(ctx)[0]


def test_a_site_settles_a_name_on_both_sites(played):
    registered(ALICE, "alice", "chess.com")
    registered(ALICE, "alice", "lichess")
    ctx = make_ctx(ALICE)
    run(botmod.mystats, ctx, "alice")
    assert reactions(ctx) == [NO] and "chess.com and lichess" in said(ctx)[0]
    ctx = make_ctx(ALICE)
    run(botmod.mystats, ctx, "alice", "LICHESS")
    assert played["calls"] == [("lichess", "alice", sources.current_month())]
    assert "Lichess" in "\n".join(said(ctx))


def test_mystats_with_no_account_or_an_unknown_name_says_so(played):
    ctx = make_ctx(ALICE)
    run(botmod.mystats, ctx)
    assert reactions(ctx) == [NO] and "!add" in said(ctx)[0]
    ctx = make_ctx(ALICE)
    run(botmod.mystats, ctx, "nobody")
    assert reactions(ctx) == [NO] and "isn't on the list" in said(ctx)[0]
    assert played["calls"] == []


def test_a_removed_player_cannot_be_looked_up(played):
    registered(ALICE, "Alice")
    store.remove_player("chess.com", "Alice")
    ctx = make_ctx(BOB)
    run(botmod.mystats, ctx, "alice")
    assert reactions(ctx) == [NO]


def test_mystats_rejects_an_unknown_site(played):
    registered(ALICE, "Alice")
    ctx = make_ctx(ALICE)
    run(botmod.mystats, ctx, "alice", "example.org")
    assert reactions(ctx) == [NO] and "chess.com" in said(ctx)[0]
    assert played["calls"] == []


def test_a_site_failure_is_reported_and_does_not_burn_the_cooldown(played):
    registered(ALICE, "Alice")
    played["error"] = sources.SourceError("couldn't reach chess.com (TimeoutError)")
    ctx = make_ctx(ALICE)
    run(botmod.mystats, ctx)
    assert reactions(ctx) == [NO] and said(ctx) == ["couldn't reach chess.com (TimeoutError)"]
    ctx.command.reset_cooldown.assert_called_once()


def test_a_player_with_no_row_for_the_month_is_told_to_try_later(played):
    registered(ALICE, "Alice", month="2020-01")  # only an old row, as at the start of a new month
    ctx = make_ctx(ALICE)
    run(botmod.mystats, ctx)
    assert reactions(ctx) == [NO] and "no results for" in said(ctx)[0]
    assert played["calls"] == []


def test_a_player_with_no_games_yet_gets_a_friendly_message(played):
    registered(ALICE, "Alice")
    played["games"] = []
    ctx = make_ctx(ALICE)
    run(botmod.mystats, ctx)
    assert said(ctx)[0].endswith("No rated blitz games yet this month.")


def test_the_start_rating_comes_from_the_stored_month_row(played):
    registered(ALICE, "Alice")  # start rating 1500 in the row
    ctx = make_ctx(ALICE)
    run(botmod.mystats, ctx)
    assert "start 1500" in "\n".join(said(ctx))


def test_an_admin_asking_for_a_missing_player_has_no_cooldown_to_refund(played):
    ctx = make_ctx(MATT_ADMIN)
    run(botmod.mystats, ctx, "nobody")
    assert reactions(ctx) == [NO]
    ctx.command.reset_cooldown.assert_not_called()


def test_both_commands_carry_the_cooldown_and_the_error_handler_knows_their_usage():
    for command in (botmod.mystats, botmod.mystatsfull):
        assert command._buckets.valid  # a cooldown is attached
    assert botmod.USAGE["mystats"] == "!mystats [username] [site]"
    assert botmod.USAGE["mystatsfull"] == "!mystatsfull [username] [site]"


def test_the_help_message_lists_every_command_and_fits_in_one_discord_message():
    ctx = make_ctx(ALICE)
    run(botmod.help_blitz_bot, ctx)
    text = said(ctx)[0]
    for name in ("!add", "!remove", "!results", "!100gob", "!100gobnext", "!mystats", "!mystatsfull"):
        assert name in text
    assert len(text) < 2000


# --- !results --------------------------------------------------------------


def test_results_with_nobody_registered(site):
    ctx = make_ctx(ALICE)
    run(botmod.results, ctx)
    assert reactions(ctx) == [] and "Nobody is registered yet" in said(ctx)[0]


def test_results_shows_the_stored_totals_and_calls_no_site(site):
    month = sources.current_month()
    store.add_player("chess.com", "Alice", ALICE, month, 1500)
    store.apply_refresh("chess.com", "Alice", month, expected_watermark=None, games=12, wins=7, draws=1, losses=4,
                        end_rating=1524, last_game_at="2026-09-05T12:00:00+00:00", now="2026-09-05T12:30:00+00:00")
    ctx = make_ctx(BOB)  # anyone can ask
    run(botmod.results, ctx)
    text = "\n".join(said(ctx))
    assert "Alice" in text and "7-1-4" in text and "+24" in text
    assert site["calls"] == []  # read from the database only


def test_results_sends_every_message_of_a_long_table_in_order(site):
    month = sources.current_month()
    for i in range(120):
        store.add_player("chess.com", f"player_{i:03d}", ALICE, month, 1500)
    ctx = make_ctx(ALICE)
    run(botmod.results, ctx)
    assert len(said(ctx)) > 1 and all(len(m) <= 2000 for m in said(ctx))


# --- error handler ---------------------------------------------------------


def test_a_missing_argument_gets_the_commands_usage():
    from discord.ext import commands

    ctx = make_ctx(ALICE)
    ctx.command = SimpleNamespace(name="add")
    asyncio.run(botmod.on_command_error(ctx, commands.MissingRequiredArgument(SimpleNamespace(name="site", displayed_name=None))))
    assert reactions(ctx) == [NO] and "!add <username> <site>" in said(ctx)[0]


def test_an_unknown_command_is_silent_in_discord_but_logged(caplog):
    from discord.ext import commands

    ctx = make_ctx(ALICE)
    ctx.invoked_with, ctx.channel = "100gob", SimpleNamespace(id=555)
    with caplog.at_level("INFO", logger="playmoreblitz"):
        asyncio.run(botmod.on_command_error(ctx, commands.CommandNotFound('Command "100gob" is not found')))
    assert ctx.send.await_count == 0 and reactions(ctx) == []  # nothing said in the channel
    assert "no command called !100gob (channel 555)" in caplog.text


def test_a_command_in_a_disallowed_channel_is_silent_in_discord_but_logged(caplog):
    from discord.ext import commands

    ctx = make_ctx(ALICE)
    ctx.invoked_with, ctx.channel = "results", SimpleNamespace(id=777)
    with caplog.at_level("INFO", logger="playmoreblitz"):
        asyncio.run(botmod.on_command_error(ctx, commands.CheckFailure("nope")))
    assert ctx.send.await_count == 0 and reactions(ctx) == []
    assert "!results in channel 777 (not an allowed channel" in caplog.text


def test_every_command_that_runs_is_logged(caplog):
    ctx = make_ctx(ALICE)
    ctx.command, ctx.channel = SimpleNamespace(qualified_name="results"), SimpleNamespace(id=42)
    with caplog.at_level("INFO", logger="playmoreblitz"):
        asyncio.run(botmod.on_command(ctx))
    assert f"command !results from {ALICE} in channel 42" in caplog.text


def test_commands_from_outside_the_allowed_channels_are_ignored_silently():
    ctx = SimpleNamespace(channel=SimpleNamespace(id=1))
    assert asyncio.run(botmod._in_allowed_channel(ctx)) is False
    ctx = SimpleNamespace(channel=SimpleNamespace(id=min(botmod.ALLOWED_CHANNEL_IDS)))
    assert asyncio.run(botmod._in_allowed_channel(ctx)) is True

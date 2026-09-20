"""What the bot posts at a month's end, and !closemonth."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from helpers import at, game

import bot as botmod
import monthend
import sources
import store

OK, NO = "✅", "❌"
OWNER = 1001
ADMIN = min(botmod.ADMIN_USER_IDS)


def utc(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monthend.last_failures.clear()
    yield
    monthend.last_failures.clear()


class FakeChannel:
    def __init__(self):
        self.sent, self.fail = [], False

    async def send(self, text):
        if self.fail:
            raise RuntimeError("Missing Access")
        self.sent.append(text)


@pytest.fixture
def channel(monkeypatch):
    fake = FakeChannel()
    monkeypatch.setattr(botmod.bot, "get_channel", lambda channel_id: fake)
    return fake


@pytest.fixture
def clock(monkeypatch):
    """Freeze the bot's idea of now: clock.set(datetime)."""
    state = {"now": utc(2026, 10, 1, 8, 0)}  # 09:00 UK on 1 October

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return state["now"].astimezone(tz) if tz else state["now"]

    monkeypatch.setattr(botmod, "datetime", Frozen)
    return SimpleNamespace(set=lambda when: state.update(now=when))


def closed_september(finished=True, extra_players=()):
    """September closed at 00:30 UTC on 1 October, with alice in the challenge (finished it, or not)."""
    store.add_player("chess.com", "alice", OWNER, "2026-09", 1500)
    store.join_100gob("chess.com", "alice", "2026-09")
    for name in extra_players:
        store.add_player("chess.com", name, OWNER, "2026-09", 1400)
    finals = [dict(site="chess.com", username="alice", games=120 if finished else 60, wins=70, draws=5, losses=45,
                   end_rating=1560, last_game_at="2026-09-30T21:00:00+00:00")]
    finals += [dict(site="chess.com", username=n, games=10, wins=5, draws=0, losses=5, end_rating=1400, last_game_at=None)
               for n in extra_players]
    store.close_month("2026-09", "2026-10", finals, "2026-10-01T00:30:00+00:00")


def post():
    return asyncio.run(botmod.post_month_end_if_due())


# --- the final table ---------------------------------------------------------


def test_the_final_table_is_posted_at_9am_uk_on_the_1st_with_well_done_for_finishers(channel, clock):
    closed_september()
    assert post() is True
    assert channel.sent[0].startswith("**September 2026 final**") and "Final results" in channel.sent[0]
    assert channel.sent[-1] == "Well done to `alice` for playing 100 games of blitz and completing 100GOB!"


def test_nothing_is_posted_before_9am_uk(channel, clock):
    closed_september()
    clock.set(utc(2026, 10, 1, 7, 59))  # 08:59 BST
    assert post() is False and channel.sent == []
    clock.set(utc(2026, 10, 1, 8, 0))
    assert post() is True


def test_it_is_posted_only_once_even_if_asked_again_or_after_a_restart(channel, clock):
    closed_september()
    assert post() is True
    n = len(channel.sent)
    assert post() is False and len(channel.sent) == n


def test_someone_in_the_challenge_who_fell_short_gets_no_well_done(channel, clock):
    closed_september(finished=False)
    post()
    assert len(channel.sent) == 1 and "Well done" not in channel.sent[0]


def test_someone_who_reached_the_target_but_never_joined_gets_no_well_done(channel, clock):
    store.add_player("chess.com", "bob", OWNER, "2026-09", 1500)  # never in the challenge
    store.close_month("2026-09", "2026-10", [dict(site="chess.com", username="bob", games=150, wins=90, draws=0, losses=60,
                                                  end_rating=1600, last_game_at="2026-09-30T21:00:00+00:00")], "2026-10-01T00:30:00+00:00")
    post()
    assert all("Well done" not in m for m in channel.sent)


def test_a_failed_post_can_be_retried_and_posts_exactly_once(channel, clock):
    closed_september()
    channel.fail = True
    assert post() is False and channel.sent == []
    channel.fail = False
    assert post() is True
    assert sum("final" in m for m in channel.sent) == 1


def test_an_old_close_is_not_posted_again_after_a_bot_has_been_off_for_days(channel, clock):
    closed_september()
    clock.set(utc(2026, 10, 10, 12, 0))
    assert post() is False and channel.sent == []


def test_a_player_who_registered_after_the_month_is_not_in_its_final_table(channel, clock):
    closed_september()
    store.add_player("chess.com", "latecomer", OWNER, "2026-10", 1400)  # joined on 1 October, after the close
    post()
    table = channel.sent[0]
    assert "alice" in table and "latecomer" not in table and "not counted yet" not in table


def test_the_final_table_names_the_configured_channel(monkeypatch, clock):
    asked = []
    monkeypatch.setattr(botmod.bot, "get_channel", lambda channel_id: asked.append(channel_id) or FakeChannel())
    closed_september()
    post()
    assert asked and set(asked) == {botmod.POST_CHANNEL_ID}


# --- the failure notice ------------------------------------------------------


def unclosed_september_with_failure():
    store.add_player("chess.com", "alice", OWNER, "2026-09", 1500)
    monthend.last_failures["2026-09"] = (monthend.Failure("chess.com", "alice", "couldn't reach chess.com (TimeoutError)"),)


def test_an_unclosed_month_is_reported_naming_the_player_from_9am_uk_on_the_1st(channel, clock):
    unclosed_september_with_failure()
    clock.set(utc(2026, 10, 1, 7, 59))
    assert post() is False and channel.sent == []
    clock.set(utc(2026, 10, 1, 8, 0))
    assert post() is True
    assert "Couldn't close September 2026" in channel.sent[0] and "`alice` (chess.com)" in channel.sent[0]
    assert all("final" not in m for m in channel.sent)  # and no final table


def test_the_failure_notice_is_posted_once_a_day_not_every_half_hour(channel, clock):
    unclosed_september_with_failure()
    assert post() is True
    clock.set(utc(2026, 10, 1, 8, 30))
    assert post() is False
    clock.set(utc(2026, 10, 1, 20, 0))
    assert post() is False and len(channel.sent) == 1
    clock.set(utc(2026, 10, 2, 8, 0))  # the next day it is raised again
    assert post() is True and len(channel.sent) == 2


def test_no_notice_when_nothing_is_known_to_have_failed(channel, clock):
    store.add_player("chess.com", "alice", OWNER, "2026-09", 1500)  # unclosed, but no attempt has failed
    assert post() is False and channel.sent == []


def test_a_failure_notice_that_could_not_be_sent_is_tried_again(channel, clock):
    unclosed_september_with_failure()
    channel.fail = True
    assert post() is False
    channel.fail = False
    assert post() is True and len(channel.sent) == 1


# --- the scheduled tasks -----------------------------------------------------


def test_the_loop_closes_first_then_refreshes_then_posts(monkeypatch):
    order = []

    async def fake_close():
        order.append("close")
        return []

    async def fake_refresh(month):
        order.append("refresh")
        return {}

    async def fake_post():
        order.append("post")

    monkeypatch.setattr(monthend, "close_due_months", fake_close)
    monkeypatch.setattr(botmod.refresh, "refresh_all", fake_refresh)
    monkeypatch.setattr(botmod, "post_month_end_if_due", fake_post)
    asyncio.run(botmod.refresh_loop.coro())
    assert order == ["close", "refresh", "post"]


def test_a_crash_in_the_loop_is_swallowed_so_the_loop_keeps_running(monkeypatch):
    async def boom():
        raise RuntimeError("bug")

    monkeypatch.setattr(monthend, "close_due_months", boom)
    asyncio.run(botmod.refresh_loop.coro())  # must not raise


def test_the_daily_task_makes_both_posts_even_if_the_first_crashes(monkeypatch):
    done = []

    async def crashes():
        raise RuntimeError("bug")

    async def works():
        done.append("month_end")

    monkeypatch.setattr(botmod, "post_signup_call_if_due", crashes)
    monkeypatch.setattr(botmod, "post_month_end_if_due", works)
    asyncio.run(botmod.daily_posts.coro())
    assert done == ["month_end"]


# --- !closemonth -------------------------------------------------------------


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def make_ctx(author):
    return SimpleNamespace(author=SimpleNamespace(id=author), message=SimpleNamespace(add_reaction=AsyncMock()), send=AsyncMock(),
                           typing=lambda: Typing(), command=MagicMock())


def run_closemonth(author=ADMIN):
    ctx = make_ctx(author)
    asyncio.run(botmod.closemonth.callback(ctx))
    return ctx, [c.args[0] for c in ctx.message.add_reaction.await_args_list], [c.args[0] for c in ctx.send.await_args_list]


@pytest.fixture
def site(monkeypatch):
    state = {"games": {}, "fail": {}}

    async def fake(session, site_name, username, month, *, after=None, limit=None):
        if username in state["fail"]:
            raise state["fail"][username]
        return sorted(state["games"].get(username, []), key=lambda g: g.ended_at)

    monkeypatch.setattr(sources, "month_games", fake)
    return state


def test_only_admins_can_run_it_and_others_get_no_reply_at_all():
    assert botmod._admin_only(SimpleNamespace(author=SimpleNamespace(id=ADMIN))) is True
    assert botmod._admin_only(SimpleNamespace(author=SimpleNamespace(id=OWNER))) is False
    assert botmod._admin_only in botmod.closemonth.checks  # so discord.py refuses others; the error handler stays silent


def test_with_nothing_to_close_it_says_so(channel, clock):
    ctx, reactions, said = run_closemonth()
    assert said == ["Nothing to close: every finished month is already closed."] and reactions == []
    assert channel.sent == []


def test_it_closes_the_month_and_posts_the_final_table_whatever_the_time(channel, clock, site):
    clock.set(utc(2026, 10, 1, 1, 0))  # 02:00 BST: before the usual 9am, but an admin asked
    site["games"]["alice"] = [game("W", when=at(5, 10), rating_after=1520)]
    store.add_player("chess.com", "alice", OWNER, "2026-09", 1500)
    ctx, reactions, said = run_closemonth()
    assert reactions == [OK] and said == []
    assert channel.sent[0].startswith("**September 2026 final**")
    assert store.month_row("chess.com", "alice", "2026-09")["closed_at"] is not None
    assert store.month_row("chess.com", "alice", "2026-10")["start_rating"] == 1520


def test_running_it_again_refuses_because_the_month_is_already_closed(channel, clock, site):
    store.add_player("chess.com", "alice", OWNER, "2026-09", 1500)
    run_closemonth()
    posted = len(channel.sent)
    ctx, reactions, said = run_closemonth()
    assert said == ["Nothing to close: every finished month is already closed."]
    assert len(channel.sent) == posted  # and nothing is posted twice


def test_a_failed_fetch_posts_the_notice_naming_the_player_and_changes_nothing(channel, clock, site):
    store.add_player("chess.com", "alice", OWNER, "2026-09", 1500)
    site["fail"]["alice"] = sources.NoSuchUser("no chess.com account 'alice'")
    ctx, reactions, said = run_closemonth()
    assert reactions == [NO]
    assert len(channel.sent) == 1 and "`alice` (chess.com): no chess.com account 'alice'" in channel.sent[0]
    assert all("final" not in m for m in channel.sent)
    assert store.month_row("chess.com", "alice", "2026-09")["closed_at"] is None


def test_it_does_not_repost_a_table_that_was_already_posted(channel, clock, site):
    store.add_player("chess.com", "alice", OWNER, "2026-09", 1500)
    store.claim_announcement("month_end", "2026-09", "2026-10-01T08:00:00+00:00")  # already posted earlier
    ctx, reactions, said = run_closemonth()
    assert reactions == [OK] and channel.sent == []
    assert store.month_row("chess.com", "alice", "2026-09")["closed_at"] is not None


def test_if_the_post_fails_it_says_the_month_is_closed_but_could_not_be_posted_and_allows_a_retry(channel, clock, site):
    store.add_player("chess.com", "alice", OWNER, "2026-09", 1500)
    channel.fail = True
    ctx, reactions, said = run_closemonth()
    assert reactions == [NO] and "closed but I couldn't post it" in said[0]
    channel.fail = False
    assert post() is True  # the ordinary posting path still finds it (the claim was given back)
    assert channel.sent[0].startswith("**September 2026 final**")

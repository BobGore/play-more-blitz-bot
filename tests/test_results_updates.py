"""The results updates posted during the month (the 10th, the 20th and the sign-up call's day), and the switches that
turn the bot's own posts off."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import announce
import bot as botmod
import settings
import store

OWNER = 1001


def utc(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(settings, "ANNOUNCEMENTS", True)
    monkeypatch.setattr(settings, "RESULTS_UPDATES", True)
    monkeypatch.setattr(settings, "RESULTS_UPDATE_DAYS", (10, 20))


# --- when an update is due, and what its line says --------------------------------------------------------------

@pytest.mark.parametrize("now, due", [
    (utc(2026, 10, 5, 12), None),                        # nothing yet this month
    (utc(2026, 10, 10, 7, 59), None),                    # 08:59 UK on the 10th
    (utc(2026, 10, 10, 8, 0), ("2026-10", 10)),          # 09:00 UK on the 10th (summer time)
    (utc(2026, 10, 15, 12), ("2026-10", 10)),            # still the 10th's until the 20th
    (utc(2026, 10, 20, 8, 0), ("2026-10", 20)),
    (utc(2026, 10, 25, 9, 0), ("2026-10", 25)),          # the sign-up call's day: 1 November less 7 days (09:00 UK in winter)
    (utc(2026, 10, 31, 12), ("2026-10", 25)),
    (utc(2026, 9, 24, 8, 0), ("2026-09", 24)),           # a 30-day month: the call is on the 24th
    (utc(2027, 2, 22, 9, 0), ("2027-02", 22)),           # February: the 22nd
])
def test_the_latest_update_due_is_the_one_returned(now, due):
    assert announce.results_update_due(now) == due


def test_the_update_days_are_a_setting():
    assert announce.results_update_due(utc(2026, 10, 6, 9), days=(5,)) == ("2026-10", 5)
    assert announce.results_update_due(utc(2026, 10, 12, 9), days=(5, 15)) == ("2026-10", 5)


@pytest.mark.parametrize("now, start, middle", [
    (utc(2026, 9, 10, 8), "**September 2026: 10 days in**", "20 days to go"),
    (utc(2026, 9, 20, 8), "**September 2026: 10 days to go**", "still time to get some more games in"),
    (utc(2027, 2, 20, 9), "**February 2027: 8 days to go**", "still time"),             # exact, not "about 10"
    (utc(2026, 10, 30, 9), "**October 2026: 1 day to go**", "still time"),
    (utc(2026, 9, 1, 8), "**September 2026: 1 day in**", "29 days to go"),
])
def test_the_line_counts_exact_days_from_the_day_it_is_posted(now, start, middle):
    month = f"{now.year:04d}-{now.month:02d}"
    text = announce.results_update_text(month, now)
    assert text.startswith(start) and middle in text


def test_the_signup_call_mentions_the_slash_command_too():
    text = announce.signup_call_text("2026-10", 100)
    assert "`/100gobnext`" in text and "`!100gobnext`" in text


# --- posting ---------------------------------------------------------------------------------------------------------

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
    state = {"now": utc(2026, 9, 10, 8, 0)}  # 09:00 UK on 10 September

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return state["now"].astimezone(tz) if tz else state["now"]

    monkeypatch.setattr(botmod, "datetime", Frozen)
    return SimpleNamespace(set=lambda when: state.update(now=when))


@pytest.fixture
def players():
    store.add_player("chess.com", "alice_example", OWNER, "2026-09", 1500)
    store.add_player("lichess", "bob_example", OWNER, "2026-09", 1600)


def update():
    return asyncio.run(botmod.post_results_update_if_due())


def test_on_the_10th_the_table_is_posted_once_under_its_line(channel, clock, players):
    assert update() is True
    assert channel.sent[0].startswith("**September 2026: 10 days in** - how are we all doing? 20 days to go.")
    assert "alice_example" in "\n".join(channel.sent) and "bob_example" in "\n".join(channel.sent)
    posted = len(channel.sent)
    assert update() is False and len(channel.sent) == posted              # not again, even after a restart


def test_nothing_is_posted_between_update_days(channel, clock, players):
    clock.set(utc(2026, 9, 5, 12))
    assert update() is False and channel.sent == []


def test_a_bot_that_was_down_posts_only_the_latest_update_counted_from_the_day_it_is_back(channel, clock, players):
    clock.set(utc(2026, 9, 22, 12))                                        # down over the 10th and the 20th
    assert update() is True
    assert channel.sent[0].startswith("**September 2026: 8 days to go**")
    posted = len(channel.sent)
    clock.set(utc(2026, 9, 23, 12))
    assert update() is False and len(channel.sent) == posted              # the 10th's is never posted late


def test_results_updates_off_posts_nothing(channel, clock, players, monkeypatch):
    monkeypatch.setattr(settings, "RESULTS_UPDATES", False)
    assert update() is False and channel.sent == []


def test_with_nobody_registered_nothing_is_posted(channel, clock):
    assert update() is False and channel.sent == []


def test_a_failed_update_is_tried_again_and_posts_once(channel, clock, players):
    channel.fail = True
    assert update() is False
    channel.fail = False
    assert update() is True and channel.sent[0].startswith("**September 2026: 10 days in**")


def test_on_the_calls_day_the_call_goes_first_then_the_table_as_its_own_post(channel, clock, players):
    clock.set(utc(2026, 9, 24, 8, 0))                                      # 09:00 UK, 7 days before October
    asyncio.run(botmod.daily_posts.coro())
    assert channel.sent[0].startswith("**100GOB for October 2026: sign-ups are open**")
    assert channel.sent[1].startswith("**September 2026: 6 days to go**")


def test_announcements_off_stops_the_call_but_not_the_results_update(channel, clock, players, monkeypatch):
    monkeypatch.setattr(settings, "ANNOUNCEMENTS", False)
    clock.set(utc(2026, 9, 24, 8, 0))
    asyncio.run(botmod.daily_posts.coro())
    assert len(channel.sent) >= 1 and channel.sent[0].startswith("**September 2026: 6 days to go**")
    assert not any("sign-ups are open" in m for m in channel.sent)


def test_announcements_off_stops_the_final_table_but_the_month_still_closes(channel, clock, monkeypatch):
    store.add_player("chess.com", "alice_example", OWNER, "2026-09", 1500)
    store.close_month("2026-09", "2026-10", [dict(site="chess.com", username="alice_example", games=12, wins=6, draws=0, losses=6,
                                                  end_rating=1510, last_game_at="2026-09-30T21:00:00+00:00")], "2026-10-01T00:30:00+00:00")
    monkeypatch.setattr(settings, "ANNOUNCEMENTS", False)
    monkeypatch.setattr(settings, "RESULTS_UPDATES", False)
    clock.set(utc(2026, 10, 1, 8, 0))
    assert asyncio.run(botmod.post_month_end_if_due()) is False and channel.sent == []
    assert store.results("2026-09", True)[0].games == 12                   # the close itself stands


def test_the_daily_task_makes_all_three_posts_even_if_one_crashes(monkeypatch):
    done = []

    async def crashes():
        raise RuntimeError("bug")

    async def month_end():
        done.append("month_end")

    async def results():
        done.append("results")

    monkeypatch.setattr(botmod, "post_signup_call_if_due", crashes)
    monkeypatch.setattr(botmod, "post_month_end_if_due", month_end)
    monkeypatch.setattr(botmod, "post_results_update_if_due", results)
    asyncio.run(botmod.daily_posts.coro())
    assert done == ["month_end", "results"]


@pytest.mark.parametrize("raw, days", [("10,20", (10, 20)), (" 5 , 15,25 ", (5, 15, 25)), ("20,10,10", (10, 20))])
def test_the_update_days_setting_is_read_as_days_of_the_month(raw, days):
    assert settings.day_numbers("X", (10, 20), env={"X": raw}) == days
    assert settings.day_numbers("X", (10, 20), env={}) == (10, 20)


@pytest.mark.parametrize("raw", ["0", "29", "tenth", "10;20", ","])
def test_a_bad_update_days_setting_stops_the_bot(raw):
    with pytest.raises(SystemExit, match="days of the month from 1 to 28"):
        settings.day_numbers("X", (10, 20), env={"X": raw})

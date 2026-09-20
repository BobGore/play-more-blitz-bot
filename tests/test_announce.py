from datetime import datetime, timezone

import pytest

import announce
import monthend

UK = announce.UK


def uk(year, month, day, hour=9, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UK)


@pytest.mark.parametrize(
    "now,due",
    [
        (uk(2026, 9, 23, 23, 59), None),  # the day before
        (uk(2026, 9, 24, 8, 59), None),  # the day itself, but before 9am
        (uk(2026, 9, 24, 9, 0), "2026-10"),  # seven days before 1 October, at 9am
        (uk(2026, 9, 24, 17, 30), "2026-10"),
        (uk(2026, 9, 30, 23, 59), "2026-10"),  # the last day: a late catch-up still counts
        (uk(2026, 10, 1, 9, 0), None),  # it is October now; the next call is for November
        (uk(2026, 10, 24, 12, 0), None),
        (uk(2026, 10, 25, 9, 0), "2026-11"),  # 31-day October: 25th
        (uk(2026, 11, 24, 9, 0), "2026-12"),  # 30-day November: 24th
        (uk(2026, 12, 24, 12, 0), None),
        (uk(2026, 12, 25, 9, 0), "2027-01"),  # into the next year
        (uk(2027, 2, 21, 12, 0), None),
        (uk(2027, 2, 22, 9, 0), "2027-03"),  # 28-day February
        (uk(2028, 2, 22, 12, 0), None),
        (uk(2028, 2, 23, 9, 0), "2028-03"),  # 29-day leap February
    ],
)
def test_when_the_signup_call_is_due(now, due):
    assert announce.signup_call_due(now) == due


def test_the_time_of_day_is_uk_time_so_it_shifts_against_utc_with_the_clocks():
    # BST (summer): 9am UK is 08:00 UTC.
    assert announce.signup_call_due(datetime(2026, 9, 24, 7, 59, tzinfo=timezone.utc)) is None
    assert announce.signup_call_due(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)) == "2026-10"
    # GMT (winter): 9am UK is 09:00 UTC.
    assert announce.signup_call_due(datetime(2026, 12, 25, 8, 59, tzinfo=timezone.utc)) is None
    assert announce.signup_call_due(datetime(2026, 12, 25, 9, 0, tzinfo=timezone.utc)) == "2027-01"


def test_the_uk_date_decides_not_the_utc_date():
    # 23:30 UTC on 23 September is 00:30 on the 24th in BST, but still before 9am.
    assert announce.signup_call_due(datetime(2026, 9, 23, 23, 30, tzinfo=timezone.utc)) is None


def test_the_scheduled_time_is_9am_uk():
    assert (announce.POST_TIME.hour, announce.POST_TIME.minute) == (9, 0)
    assert announce.POST_TIME.tzinfo is announce.UK


@pytest.mark.parametrize(
    "now,due",
    [
        (uk(2026, 9, 30, 23, 59), False),  # the month has only just ended by the UTC clock, but it's still the 30th in the UK
        (uk(2026, 10, 1, 0, 30), False),  # 1 October, before 9am
        (uk(2026, 10, 1, 8, 59), False),
        (uk(2026, 10, 1, 9, 0), True),  # 9am UK on the 1st
        (uk(2026, 10, 1, 17, 0), True),
        (uk(2026, 10, 9, 12, 0), True),  # any time later
    ],
)
def test_when_a_months_ending_is_due_to_be_posted(now, due):
    assert announce.month_end_post_due("2026-09", now) is due


def test_the_month_end_post_time_follows_uk_time_against_utc():
    assert announce.month_end_post_due("2026-09", datetime(2026, 10, 1, 7, 59, tzinfo=timezone.utc)) is False  # 08:59 BST
    assert announce.month_end_post_due("2026-09", datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)) is True  # 09:00 BST
    assert announce.month_end_post_due("2026-11", datetime(2026, 12, 1, 8, 59, tzinfo=timezone.utc)) is False  # GMT: 08:59
    assert announce.month_end_post_due("2026-11", datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)) is True


def test_december_ends_into_january_of_the_next_year():
    assert announce.month_end_post_due("2026-12", uk(2026, 12, 31, 23, 0)) is False
    assert announce.month_end_post_due("2026-12", uk(2027, 1, 1, 9, 0)) is True


def test_the_uk_date_is_the_local_date_not_the_utc_one():
    assert announce.uk_date(datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc)) == "2026-10-01"  # already 00:30 BST on the 1st
    assert announce.uk_date(datetime(2026, 12, 1, 0, 30, tzinfo=timezone.utc)) == "2026-12-01"


def test_well_done_names_everyone_who_finished_or_says_nothing():
    assert announce.well_done_text([], 100) is None
    assert announce.well_done_text(["amy"], 100) == "Well done to `amy` for playing 100 games of blitz and completing 100GOB!"
    assert announce.well_done_text(["amy", "bob"], 50) == "Well done to `amy`, `bob` for playing 50 games of blitz and completing 100GOB!"


def test_the_failure_notice_names_each_player_site_and_reason_and_says_what_to_do():
    failures = [
        monthend.Failure("chess.com", "alice", "couldn't reach chess.com (TimeoutError)"),
        monthend.Failure("lichess", "bob", "no lichess account 'bob'"),
    ]
    text = announce.close_failure_text("2026-09", failures)
    assert "**Couldn't close September 2026.**" in text
    assert "`alice` (chess.com): couldn't reach chess.com (TimeoutError)" in text
    assert "`bob` (lichess): no lichess account 'bob'" in text
    assert "Nothing has been posted or changed" in text
    assert "`!remove`" in text and "`!closemonth`" in text and "every half hour" in text


def test_a_very_long_reason_is_cut():
    text = announce.close_failure_text("2026-09", [monthend.Failure("chess.com", "x", "y" * 500)])
    assert "y" * 120 in text and "y" * 121 not in text


def test_the_text_names_the_month_the_target_and_the_commands():
    text = announce.signup_call_text("2026-10", 100)
    assert "October 2026" in text and "100 rated blitz games" in text
    assert "`!100gobnext`" in text and "`!add <username> <site>`" in text
    assert len(text) < 1000

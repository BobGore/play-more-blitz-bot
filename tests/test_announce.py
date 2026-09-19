from datetime import datetime, timezone

import pytest

import announce

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


def test_the_text_names_the_month_the_target_and_the_commands():
    text = announce.signup_call_text("2026-10", 100)
    assert "October 2026" in text and "100 rated blitz games" in text
    assert "`!100gobnext`" in text and "`!add <username> <site>`" in text
    assert len(text) < 1000

"""When to post the 100GOB sign-up call and the results updates, and what they say. Pure functions: no Discord, no database."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import render
import settings

UK = ZoneInfo("Europe/London")
POST_TIME = time(hour=settings.POST_HOUR_UK, tzinfo=UK)  # the bot's scheduled posts go out at this time, UK
CALL_DAYS_BEFORE = settings.CALL_DAYS_BEFORE  # the call goes out this many days before the month starts


def first_of_next_month(today):
    return date(today.year + (today.month == 12), today.month % 12 + 1, 1)


def signup_call_due(now):
    """The month ("YYYY-MM") to invite sign-ups for, if the call is due at `now`, else None.

    `now` is an aware datetime. The call is due from 9am UK time, CALL_DAYS_BEFORE days
    before that month starts, until the month before it ends, so a bot that was down at
    the exact moment still makes the call late. Posting it only once is the caller's job.
    """
    now = now.astimezone(UK)
    start = first_of_next_month(now.date())
    due_from = datetime.combine(start - timedelta(days=CALL_DAYS_BEFORE), POST_TIME.replace(tzinfo=None), tzinfo=UK)
    if now >= due_from:
        return f"{start.year:04d}-{start.month:02d}"
    return None


def month_end_post_due(month, now):
    """Whether it is time to post about `month`'s ending: from 9am UK time on the day after
    it finishes (the 1st of the next month), and any time after that."""
    following = first_of_next_month(date(int(month[:4]), int(month[5:7]), 1))
    return now >= datetime.combine(following, POST_TIME.replace(tzinfo=None), tzinfo=UK)


def results_update_due(now, days=None):
    """The (month "YYYY-MM", day) of the results update due at `now`, or None.

    Updates go out at 9am UK on each of `days` (RESULTS_UPDATE_DAYS) and on the day of the sign-up call. Only the latest
    one that has come due is ever returned, so a bot that was down posts one late update, not a string of stale ones;
    posting it only once is the caller's job.
    """
    days = settings.RESULTS_UPDATE_DAYS if days is None else days
    now = now.astimezone(UK)
    today = now.date()
    call = first_of_next_month(today) - timedelta(days=CALL_DAYS_BEFORE)
    call_day = {call.day} if (call.year, call.month) == (today.year, today.month) else set()
    due = [d for d in set(days) | call_day
           if d <= today.day and now >= datetime.combine(today.replace(day=d), POST_TIME.replace(tzinfo=None), tzinfo=UK)]
    if not due:
        return None
    return f"{today.year:04d}-{today.month:02d}", max(due)


def _days(n):
    return f"{n} day{'' if n == 1 else 's'}"


def results_update_text(month, now):
    """The line above a results update: exact days gone, or days left, counted from the UK date it is posted."""
    today = now.astimezone(UK).date()
    last = (first_of_next_month(today) - timedelta(days=1)).day
    gone, left = today.day, last - today.day
    title = render.month_title(month)
    if gone <= left:
        return f"**{title}: {_days(gone)} in** - how are we all doing? {_days(left)} to go. Here's where everyone is:"
    return (f"**{title}: {_days(left)} to go** - still time to get some more games in before the month ends. "
            "Here's where everyone is:")


def uk_date(now):
    """The UK calendar date at `now`, as YYYY-MM-DD."""
    return now.astimezone(UK).date().isoformat()


def well_done_text(names, target):
    """The congratulation for everyone who reached the target, or None if nobody did."""
    if not names:
        return None
    listed = ", ".join(f"`{n}`" for n in names)
    return f"Well done to {listed} for playing {target} games of blitz and completing 100GOB!"


def close_failure_text(month, failures):
    """The notice that a month couldn't be closed, naming each player that failed and why."""
    lines = [
        f"**Couldn't close {render.month_title(month)}.** Nothing has been posted or changed, "
        "because these players' games couldn't be fetched:"
    ]
    lines += [f"`{f.username}` ({f.site}): {f.reason[:120]}" for f in failures]
    lines.append(
        "Once that is sorted out, or the player is taken off with `!remove`, an admin can run `!closemonth`. "
        "The bot also retries by itself every half hour."
    )
    return "\n".join(lines)


def signup_call_text(month, target):
    return (
        f"**100GOB for {render.month_title(month)}: sign-ups are open**\n"
        f"Play {target} rated blitz games in the month. Join with `/100gobnext` or `!100gobnext` "
        "(add your username if you have more than one account). "
        "Not on the list yet? Use `!add <username> <site>` first (your own account, one per site)."
    )

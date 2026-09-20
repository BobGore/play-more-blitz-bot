"""When to post the 100GOB sign-up call, and what it says. Pure functions: no Discord, no database."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import render

UK = ZoneInfo("Europe/London")
POST_TIME = time(hour=9, tzinfo=UK)  # the bot's scheduled posts go out at 9am UK time
CALL_DAYS_BEFORE = 7  # the call goes out this many days before the month starts


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
        f"Play {target} rated blitz games in the month. Join with `!100gobnext` "
        "(add your username if you have more than one account). "
        "Not on the list yet? Use `!add <username> <site>` first (your own account, one per site)."
    )

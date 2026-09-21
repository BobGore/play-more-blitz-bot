"""Reading a month from what someone types in a command: "2026-08", "august", "aug", "last".

Pure functions, no Discord and no database.
"""

import calendar
import re

_NAMES = {}
for _number in range(1, 13):
    _NAMES[calendar.month_name[_number].lower()] = _number
    _NAMES[calendar.month_abbr[_number].lower()] = _number
_NAMES["sept"] = 9

_LAST = {"last", "prev", "previous"}
_THIS = {"this", "current", "now"}


def _iso(year, month):
    return f"{year:04d}-{month:02d}"


def previous(month):
    """The "YYYY-MM" month before `month`."""
    year, number = (int(part) for part in month.split("-"))
    return _iso(year - 1, 12) if number == 1 else _iso(year, number - 1)


def parse_month(text, current):
    """The "YYYY-MM" month `text` means, or None if it isn't a month.

    `current` is the present "YYYY-MM". A month written in full (2026-08, 2026/8) is taken as it is. A month name
    alone (august, aug) means its latest occurrence that isn't in the future, so in September 2026 "august" is
    2026-08 and "october" is 2025-10. "last", "prev" and "previous" mean the month before `current`; "this",
    "current" and "now" mean `current`.
    """
    if not isinstance(text, str):
        return None
    word = text.strip().lower()
    if word in _LAST:
        return previous(current)
    if word in _THIS:
        return current
    full = re.fullmatch(r"(\d{4})[-/](\d{1,2})", word)
    if full:
        year, number = int(full[1]), int(full[2])
        return _iso(year, number) if 1 <= number <= 12 and year >= 2000 else None
    number = _NAMES.get(word.rstrip("."))
    if number is None:
        return None
    year, this_month = (int(part) for part in current.split("-"))
    return _iso(year if number <= this_month else year - 1, number)


def is_future(month, current):
    """True if `month` is after `current` (both "YYYY-MM"; they sort as text)."""
    return month > current

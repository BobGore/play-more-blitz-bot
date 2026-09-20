"""Every setting you might want to change, in one place.

Each value has a default below. To change one without touching the code, put a line with the
same name in `.env` (for instance `COOLDOWN_SECONDS=300`, or `ALLOWED_CHANNEL_IDS=123,456`) and
restart the bot. A setting that is present but not valid stops the bot at startup with a message
saying which one, rather than being guessed at. The README has a table of them all.

Not here on purpose: the Discord token and CONTACT address (private, `.env` only, read where they
are used), and constants that are facts about Discord, the two sites or the code rather than choices.
"""

import os
from pathlib import Path


def _fail(name, raw, wanted):
    raise SystemExit(f"Setting {name}={raw!r} is not valid: it must be {wanted}.")


def _number(name, default, kind, minimum, wanted, env):
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = kind(raw.strip())
    except ValueError:
        _fail(name, raw, wanted)
    if value < minimum:
        _fail(name, raw, wanted)
    return value


def whole_number(name, default, minimum=1, env=os.environ):
    return _number(name, default, int, minimum, f"a whole number, {minimum} or more", env)


def seconds(name, default, minimum=0.1, env=os.environ):
    return _number(name, default, float, minimum, f"a number of seconds, {minimum} or more", env)


def discord_ids(name, default, env=os.environ):
    raw = env.get(name)
    if raw is None or not raw.strip():
        return set(default)
    try:
        ids = {int(part) for part in raw.replace(" ", "").split(",") if part}
    except ValueError:
        _fail(name, raw, "Discord IDs separated by commas")
    if not ids or any(i <= 0 for i in ids):
        _fail(name, raw, "one or more Discord IDs separated by commas")
    return ids


# --- Who and where ---------------------------------------------------------------------------------

# Every command only works in one of these channels. A channel ID only ever belongs to one server, so
# this also keeps the bot inert everywhere else it might get invited to, and in DMs.
ALLOWED_CHANNEL_IDS = discord_ids("ALLOWED_CHANNEL_IDS", {
    1550558058793533471,  # test
})

# Where the bot's own scheduled posts go (the sign-up call and the month-end table). Should be one of
# the allowed channels.
POST_CHANNEL_ID = whole_number("POST_CHANNEL_ID", 1550558058793533471)  # test

# Can !remove any entry, add for others and run !closemonth, and are exempt from the cooldown.
ADMIN_USER_IDS = discord_ids("ADMIN_USER_IDS", {
    810486671174795274,  # Bob
    315229727629508609,  # Matt
})

# --- The challenge and the timing ------------------------------------------------------------------

GOB_TARGET = whole_number("GOB_TARGET", 100)  # games in the month that earn the tick
REFRESH_INTERVAL_MINUTES = whole_number("REFRESH_INTERVAL_MINUTES", 30)  # how often totals are brought up to date
COOLDOWN_SECONDS = whole_number("COOLDOWN_SECONDS", 600, minimum=0)  # per user, on commands that call the sites
POST_HOUR_UK = whole_number("POST_HOUR_UK", 9, minimum=0)  # scheduled posts go out at this hour, UK time (0-23)
if POST_HOUR_UK > 23:
    _fail("POST_HOUR_UK", os.environ.get("POST_HOUR_UK"), "an hour from 0 to 23")
CALL_DAYS_BEFORE = whole_number("CALL_DAYS_BEFORE", 7)  # the sign-up call goes out this many days before the month

# --- What the summaries show -----------------------------------------------------------------------

MIN_OPENING_GAMES = whole_number("MIN_OPENING_GAMES", 2)  # fewer games than this and an opening goes into "All others"
MIN_BEST_WORST_GAMES = whole_number("MIN_BEST_WORST_GAMES", 3)  # games an opening needs before it can be best or worst
SIMILAR_RATING_BAND = whole_number("SIMILAR_RATING_BAND", 50, minimum=0)  # rating points either side that count as similar
STATS_CACHE_PLAYERS = whole_number("STATS_CACHE_PLAYERS", 64)  # players' games kept in memory; least recently used dropped

# --- Being polite to Chess.com and Lichess ---------------------------------------------------------

REQUEST_TIMEOUT_SECONDS = seconds("REQUEST_TIMEOUT_SECONDS", 10.0)  # any ordinary site request
# Lichess streams a game export at about 11-12 games a second, so a busy month (375 games took 33s)
# can't have a short flat limit: a generous overall cap plus a short limit on the stream stalling.
MONTH_TIMEOUT_SECONDS = seconds("MONTH_TIMEOUT_SECONDS", 300.0)  # a whole month's fetch
MONTH_STALL_SECONDS = seconds("MONTH_STALL_SECONDS", 30.0)  # a fetch that goes quiet this long is abandoned
LICHESS_EXPORT_MIN_INTERVAL = seconds("LICHESS_EXPORT_MIN_INTERVAL", 2.0, minimum=0)  # gap between two Lichess exports

# --- Storage ---------------------------------------------------------------------------------------

# Where the database file lives; by default beside the code. To keep it on a larger disk give a file in
# a folder that already exists there: the bot refuses to start if that folder is missing.
DB_PATH = Path(os.environ.get("PLAYMOREBLITZ_DB") or Path(__file__).with_name("playmoreblitz.db"))
# Nightly backups (backup.py): the folder they go in, which must already exist (it is on another disk from the
# database, and the backup refuses to run if it is missing), and how many days of them to keep.
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR") or Path(__file__).with_name("backups"))
BACKUP_KEEP_DAYS = whole_number("BACKUP_KEEP_DAYS", 100)
DB_LOCK_TIMEOUT = seconds("DB_LOCK_TIMEOUT", 5.0)  # how long to wait for another write to finish before giving up

# The names above that can be set in `.env` (PLAYMOREBLITZ_DB is the environment name for DB_PATH).
# Tests keep the README and .env.example in step with this list.
NAMES = (
    "ALLOWED_CHANNEL_IDS", "POST_CHANNEL_ID", "ADMIN_USER_IDS",
    "GOB_TARGET", "REFRESH_INTERVAL_MINUTES", "COOLDOWN_SECONDS", "POST_HOUR_UK", "CALL_DAYS_BEFORE",
    "MIN_OPENING_GAMES", "MIN_BEST_WORST_GAMES", "SIMILAR_RATING_BAND", "STATS_CACHE_PLAYERS",
    "REQUEST_TIMEOUT_SECONDS", "MONTH_TIMEOUT_SECONDS", "MONTH_STALL_SECONDS", "LICHESS_EXPORT_MIN_INTERVAL",
    "PLAYMOREBLITZ_DB", "DB_LOCK_TIMEOUT", "BACKUP_DIR", "BACKUP_KEEP_DAYS",
)

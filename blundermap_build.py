"""Building a member's monthly blunder map for !blundermap: which months can be mapped, and the page itself.

The page is made by blundermap.py, run as a separate process at the lowest priority, so building a busy month (a minute or
two) never slows the bot. It reads the bot's database and the good-move counts (settings.BLUNDERMAP_DB) and writes
nothing but the page, in a temporary folder. One build runs at a time across the whole bot (`_one_at_a_time`).
"""

import asyncio
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

import settings

HERE = Path(__file__).resolve().parent
BUILD_TIMEOUT_SECONDS = 600
MAX_FILE_BYTES = 10 * 1024 * 1024  # what a bot may send in one Discord message
_one_at_a_time = asyncio.Semaphore(1)


class BuildError(Exception):
    pass


def games_held(site, username, month, db_path=None):
    """(games held, analysed) for one account in one month, from the bot's database."""
    path = Path(db_path or settings.DB_PATH)
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return db.execute("""SELECT count(*), coalesce(sum(status = 'done'), 0) FROM game_analysis
                             WHERE site = ? AND month = ? AND (white_username = ? COLLATE NOCASE OR black_username = ? COLLATE NOCASE)
                             AND status IN ('done', 'skipped')""", (site, month, username, username)).fetchone()
    finally:
        db.close()


def file_name(site, username, month):
    return re.sub(r"[^A-Za-z0-9._-]", "_", f"blundermap_{site}_{username}_{month}") + ".html"


def _low_priority():  # in the child process, before blundermap.py starts
    os.nice(19)


async def build(site, username, month, *, counts_path=None, db_path=None, python=None, timeout=BUILD_TIMEOUT_SECONDS):
    """(page bytes, summary) for one account's month. The summary is blundermap.py's --summary line: games, analysed,
    skipped, used (on the map), waiting (analysed, not counted yet), cards, monthOpen ... Raises BuildError."""
    counts_path = counts_path or settings.BLUNDERMAP_DB
    async with _one_at_a_time:
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "map.html"
            args = [python or sys.executable, str(HERE / "blundermap.py"), "--botdb", str(db_path or settings.DB_PATH),
                    "--counts", str(counts_path), "--site", site, "--user", username, "--month", month, "--out", str(out),
                    "--summary"]
            extra = {"preexec_fn": _low_priority} if os.name == "posix" else {}
            proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                        **extra)
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise BuildError(f"took longer than {timeout} s") from None
            if proc.returncode != 0 or not out.exists():
                detail = (stderr.decode(errors="replace").strip().splitlines() or [f"exit status {proc.returncode}"])[-1]
                raise BuildError(detail[:200])
            try:
                summary = json.loads(stdout.decode().strip().splitlines()[-1])
            except (ValueError, IndexError):
                raise BuildError("no summary from blundermap.py") from None
            return out.read_bytes(), summary


def describe(summary, month_title, whose=None):
    """The line that goes with the file: how much of the month is on the map, in plain words. `whose` is a player's name
    when an admin asked for someone else's map ("your" otherwise)."""
    used, analysed, waiting = summary["used"], summary["analysed"], summary["waiting"]
    so_far = " so far" if summary.get("monthOpen") else ""
    cards = summary["cards"]
    owner = f"{whose}'s" if whose else "your"
    text = (f"{month_title}{so_far}: {used} of {owner} {analysed} analysed games are on the map, "
            f"with {cards} blunder card{'' if cards == 1 else 's'}.")
    if waiting:
        text += f" {waiting} more {'is' if waiting == 1 else 'are'} still being counted: ask again later to see them too."
    left = analysed - used - waiting
    if left > 0:
        text += f" {left} can't be placed on it (their clock times weren't kept)."
    return text

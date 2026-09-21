"""Watching over the bot: what is worth telling an admin about, and how often.

Pure logic, with no Discord and no network: the bot's health loop and error hooks call these to decide, and send the
messages themselves. Nothing here reads anyone's data beyond counts about the machinery (the analysis queue, the backups,
the refresh cycles).
"""

import time
from datetime import date
from pathlib import Path

import analysis_queue as q
import backup
import render_analysis

WORKER_STALE_SECONDS = 15 * 60  # a worker silent this long while games wait is a problem (a reboot of the EliteDesk is shorter)
BACKUP_STALE_DAYS = 2  # the nightly backup should never be this many days behind
REFRESH_FAILING_CYCLES = 3  # refresh cycles in a row that all failed before it is reported
REPEAT_SECONDS = 6 * 3600  # how long before the same problem is reported again while it lasts
ERROR_REPEAT_SECONDS = 30 * 60  # the same unexpected error is reported at most this often
MAX_ERROR_TEXT = 300


class Alerts:
    """Remembers when each problem was last reported, so a lasting problem is reported now and then, not at every check."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._sent = {}

    def due(self, key, repeat=REPEAT_SECONDS):
        """True, and noted, if `key` hasn't been reported within the last `repeat` seconds."""
        now = self._clock()
        last = self._sent.get(key)
        if last is not None and now - last < repeat:
            return False
        self._sent[key] = now
        return True

    def clear(self, key):
        """The problem is over: if it comes back it is reported at once."""
        self._sent.pop(key, None)


def worker_problem(status):
    """Words for a stalled analysis worker (from analysis_queue.status), or None if games aren't waiting or it is alive."""
    counts = status["counts"]
    waiting = counts[q.PENDING] + counts[q.CLAIMED]
    if not waiting:
        return None
    workers = status["workers"]
    if not workers:
        return f"{waiting} games are waiting to be analysed and no analysis worker has ever asked for work."
    silent = workers[0][1]
    if silent > WORKER_STALE_SECONDS:
        return f"The analysis worker last asked for work {render_analysis.duration(silent)} ago, and {waiting} games are waiting."
    return None


def backup_problem(folder, today=None):
    """Words for a backup that hasn't happened, or None if the newest is recent enough."""
    today = today or date.today()
    folder = Path(folder)
    if not folder.is_dir():
        return f"The backup folder {folder} is missing: is its disk mounted?"
    found = backup.dated(folder)
    if not found:
        return "There are no backups in the backup folder."
    newest = found[-1][0]
    age = (today - newest).days
    if age >= BACKUP_STALE_DAYS:
        return f"The newest backup is from {newest.isoformat()}, {age} days ago: the nightly backup isn't happening."
    return None


def refresh_failed(outcomes):
    """True for a refresh cycle in which every player's refresh failed (`outcomes` is refresh.refresh_all's count by result)."""
    failed = outcomes.get("failed", 0)
    return failed > 0 and sum(outcomes.values()) == failed


def refresh_problem(consecutive):
    """Words once `consecutive` refresh cycles in a row have all failed (REFRESH_FAILING_CYCLES or more), else None."""
    if consecutive >= REFRESH_FAILING_CYCLES:
        return f"The last {consecutive} refresh cycles all failed: are Chess.com and Lichess reachable from the Minix?"
    return None


def error_alert(command, error):
    """Words for an unexpected error in a command. Names the command and the kind of error, and nothing about the person."""
    error = getattr(error, "original", error)
    detail = f"{type(error).__name__}: {error}".strip()
    return f"{command} hit an unexpected error ({detail[:MAX_ERROR_TEXT]}). The log has the details."

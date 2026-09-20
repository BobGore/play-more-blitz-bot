"""Refuse to run a second copy of the bot from the same folder.

Two copies with the same token would both answer every command and both post every
scheduled message. The operating system holds the lock and releases it when the
process ends, however it ends, so a crash never leaves a stale lock behind.
"""

import os


def _lock_windows(handle):
    import msvcrt

    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)


def _lock_posix(handle):
    import fcntl

    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def acquire(path):
    """Take the lock file at `path` or exit with a clear message. Keep the returned
    handle for the life of the process: closing it (or exiting) releases the lock."""
    handle = open(path, "a+")
    try:
        (_lock_windows if os.name == "nt" else _lock_posix)(handle)
    except OSError:
        handle.close()
        raise SystemExit(f"Another copy of the bot is already running from this folder (lock file {os.path.basename(path)}). Stop it first.") from None
    return handle

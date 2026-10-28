"""Nightly backup of the database, run by a systemd timer (see playmoreblitz-backup.service).

    venv/bin/python backup.py

Makes a consistent copy with SQLite's own backup, which is safe while the bot is running, checks the copy
and only then puts it in place as `playmoreblitz-YYYY-MM-DD.db` in BACKUP_DIR. Copies dated more than
BACKUP_KEEP_DAYS days ago are then removed, and only after a good new copy exists, so a failing backup can
never eat the older ones. Exits with an error (which systemd records) if anything is wrong.

When BLUNDERMAP_DB is set, the good-move counts (`blundermap-YYYY-MM-DD.db`) and the counting queue beside them
(`blundermap-queue-YYYY-MM-DD.db`, if there is one) are backed up the same way, after the bot's own database.

BACKUP_DIR must already exist. If the disk it lives on isn't mounted the folder is missing and the backup
refuses to run, rather than filling the wrong disk.
"""

import os
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import settings

DATED = r"-(\d{4})-(\d{2})-(\d{2})\.db$"


def make_copy(source, target):
    """Copy the database at `source` to `target` (SQLite takes a consistent snapshot) and check the copy is sound."""
    src = sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
            if dst.execute("pragma integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("the copy failed SQLite's integrity check")
            # A real database always has tables; none means the source was empty or the wrong file.
            if dst.execute("select count(*) from sqlite_master where type='table'").fetchone()[0] == 0:
                raise RuntimeError("the copy has no tables")
        finally:
            dst.close()
    finally:
        src.close()


def dated(folder, name="playmoreblitz"):
    """The backups called `name` in `folder` as (date, path), oldest first. Files with other names are ignored."""
    found = []
    pattern = re.compile("^" + re.escape(name) + DATED)
    for path in Path(folder).iterdir():
        m = pattern.match(path.name)
        if m:
            try:
                found.append((date(int(m[1]), int(m[2]), int(m[3])), path))
            except ValueError:
                pass  # looks like a backup but isn't a real date: leave it alone
    return sorted(found)


def run(source, folder, keep_days, today=None, name="playmoreblitz"):
    """Make today's backup of `source` as `name-YYYY-MM-DD.db` and remove that name's ones older than `keep_days`.
    Returns (the new file, the removed files)."""
    today = today or date.today()
    source, folder = Path(source), Path(folder)
    if not source.is_file():
        raise SystemExit(f"Backup stopped: there is no database at {source}.")
    if not folder.is_dir():
        raise SystemExit(f"Backup stopped: the backup folder {folder} does not exist. Is its disk mounted?")
    final = folder / f"{name}-{today.isoformat()}.db"
    partial = folder / f".{final.name}.partial"
    try:
        make_copy(source, partial)
        os.chmod(partial, 0o600)
        os.replace(partial, final)  # a rerun on the same day replaces that day's copy
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    cutoff = today - timedelta(days=keep_days)
    removed = [path for d, path in dated(folder, name) if d <= cutoff]
    for path in removed:
        path.unlink()
    return final, removed


def targets():
    """(source, name) for each database to back up: the bot's own, then the blunder map's counts and queue."""
    found = [(Path(settings.DB_PATH), "playmoreblitz")]
    if settings.BLUNDERMAP_DB:
        counts = Path(settings.BLUNDERMAP_DB)
        found.append((counts, "blundermap"))  # missing is an error: it is set, so the bot expects it
        queue = counts.with_name("blundermap-queue.db")
        if queue.is_file():
            found.append((queue, "blundermap-queue"))
    return found


def main():
    for source, name in targets():
        final, removed = run(source, settings.BACKUP_DIR, settings.BACKUP_KEEP_DAYS, name=name)
        kept = len(dated(settings.BACKUP_DIR, name))
        print(f"backed up to {final} ({final.stat().st_size} bytes); removed {len(removed)} older, {kept} kept")


if __name__ == "__main__":
    try:
        main()
    except (sqlite3.Error, OSError, RuntimeError) as problem:
        sys.exit(f"Backup failed: {problem}")

"""Nightly backup: a sound copy, dated names, a rolling window, and refusing to do harm."""

import os
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

import backup
import settings

TODAY = date(2026, 9, 21)


@pytest.fixture
def live(tmp_path):
    path = tmp_path / "live" / "playmoreblitz.db"
    path.parent.mkdir()
    conn = sqlite3.connect(path)
    conn.execute("create table players (name text)")
    conn.executemany("insert into players values (?)", [("alice_example",), ("bob_example",), ("carol_example",)])
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "backups"
    path.mkdir()
    return path


def names(folder):
    return sorted(p.name for p in folder.iterdir())


def test_the_backup_is_a_dated_copy_with_the_same_contents(live, folder):
    final, removed = backup.run(live, folder, 100, today=TODAY)
    assert final == folder / "playmoreblitz-2026-09-21.db" and removed == []
    conn = sqlite3.connect(final)
    assert [r[0] for r in conn.execute("select name from players order by name")] == ["alice_example", "bob_example", "carol_example"]
    conn.close()
    assert names(folder) == ["playmoreblitz-2026-09-21.db"]  # no partial file left behind


def test_the_live_database_is_not_changed(live, folder):
    before = live.read_bytes()
    backup.run(live, folder, 100, today=TODAY)
    assert live.read_bytes() == before


def test_a_second_run_on_the_same_day_replaces_that_days_copy(live, folder):
    backup.run(live, folder, 100, today=TODAY)
    conn = sqlite3.connect(live)
    conn.execute("insert into players values ('dave_example')")
    conn.commit()
    conn.close()
    backup.run(live, folder, 100, today=TODAY)
    assert names(folder) == ["playmoreblitz-2026-09-21.db"]
    copy = sqlite3.connect(folder / "playmoreblitz-2026-09-21.db")
    assert copy.execute("select count(*) from players").fetchone()[0] == 4
    copy.close()


def make_old(folder, *days_ago, today=TODAY):
    for n in days_ago:
        (folder / f"playmoreblitz-{(today - timedelta(days=n)).isoformat()}.db").write_bytes(b"old")


def test_copies_older_than_the_window_are_removed_and_newer_ones_kept(live, folder):
    make_old(folder, 1, 50, 99, 100, 101, 400)
    final, removed = backup.run(live, folder, 100, today=TODAY)
    assert sorted(p.name for p in removed) == sorted(f"playmoreblitz-{(TODAY - timedelta(days=n)).isoformat()}.db" for n in (100, 101, 400))
    # today's plus 99, 50 and 1 days ago: the last 100 days including today are kept
    assert len(names(folder)) == 4 and final.exists()
    assert (folder / f"playmoreblitz-{(TODAY - timedelta(days=99)).isoformat()}.db").exists()
    assert not (folder / f"playmoreblitz-{(TODAY - timedelta(days=100)).isoformat()}.db").exists()


def test_the_window_length_is_the_setting(live, folder):
    make_old(folder, 1, 2, 3, 4, 5)
    backup.run(live, folder, 3, today=TODAY)
    assert len(names(folder)) == 3  # today, 1 day ago and 2 days ago


def test_files_that_are_not_backups_are_never_touched(live, folder):
    for other in ("notes.txt", "playmoreblitz-2020-01-01.db.bak", "playmoreblitz-2020-13-45.db", "other-2020-01-01.db", "playmoreblitz-2020-1-1.db"):
        (folder / other).write_text("keep me")
    backup.run(live, folder, 100, today=TODAY)
    assert {"notes.txt", "playmoreblitz-2020-01-01.db.bak", "playmoreblitz-2020-13-45.db", "other-2020-01-01.db", "playmoreblitz-2020-1-1.db"} <= set(names(folder))


def test_a_missing_backup_folder_stops_the_backup_and_creates_nothing(live, tmp_path):
    with pytest.raises(SystemExit) as stopped:
        backup.run(live, tmp_path / "not_mounted" / "backups", 100, today=TODAY)
    assert "not_mounted" in str(stopped.value) and not (tmp_path / "not_mounted").exists()


def test_a_missing_database_stops_the_backup_and_removes_nothing(tmp_path, folder):
    make_old(folder, 200)
    with pytest.raises(SystemExit) as stopped:
        backup.run(tmp_path / "gone.db", folder, 100, today=TODAY)
    assert "no database" in str(stopped.value)
    assert len(names(folder)) == 1  # the old copy is still there


def test_an_empty_database_is_not_backed_up_and_does_not_cost_the_old_copies(tmp_path, folder):
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    make_old(folder, 200)
    with pytest.raises(RuntimeError):
        backup.run(empty, folder, 100, today=TODAY)
    assert len(names(folder)) == 1  # nothing new, nothing pruned, no partial file


def test_a_source_that_is_not_a_database_fails_cleanly(tmp_path, folder):
    junk = tmp_path / "junk.db"
    junk.write_text("this is not a database at all" * 100)
    make_old(folder, 200)
    with pytest.raises(sqlite3.Error):
        backup.run(junk, folder, 100, today=TODAY)
    assert len(names(folder)) == 1


@pytest.mark.skipif(os.name == "nt", reason="file permissions are not meaningful on Windows")
def test_the_backup_is_private_to_its_owner(live, folder):
    final, _ = backup.run(live, folder, 100, today=TODAY)
    assert final.stat().st_mode & 0o777 == 0o600


def test_running_it_as_a_program_uses_the_settings_and_reports(live, folder, tmp_path):
    env = {**{k: v for k, v in os.environ.items() if k not in settings.NAMES}, "PLAYMOREBLITZ_DB": str(live), "BACKUP_DIR": str(folder),
           "BACKUP_KEEP_DAYS": "100"}
    done = subprocess.run([sys.executable, "backup.py"], capture_output=True, text=True, cwd=Path(backup.__file__).parent, env=env)
    assert done.returncode == 0, done.stderr
    assert "backed up to" in done.stdout and len(names(folder)) == 1
    bad = subprocess.run([sys.executable, "backup.py"], capture_output=True, text=True, cwd=Path(backup.__file__).parent,
                         env={**env, "BACKUP_DIR": str(tmp_path / "nowhere")})
    assert bad.returncode != 0 and "does not exist" in bad.stderr and not (tmp_path / "nowhere").exists()

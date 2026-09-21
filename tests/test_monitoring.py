"""What is worth telling an admin about, and how often (monitoring.py), and the counts of use (usage.py)."""

import time
from datetime import date

import pytest

import analysis_queue as q
import monitoring
import store
import usage

NOW = 1_790_000_000  # 2026-09-21 UTC


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")


# --- reporting a problem once in a while ---------------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_a_problem_is_reported_at_once_then_not_again_until_the_repeat_time_has_passed():
    clock = Clock()
    alerts = monitoring.Alerts(clock)
    assert alerts.due("worker") is True
    assert alerts.due("worker") is False
    clock.now += monitoring.REPEAT_SECONDS - 1
    assert alerts.due("worker") is False
    clock.now += 1
    assert alerts.due("worker") is True                       # reported again, six hours on
    assert alerts.due("worker") is False


def test_each_problem_has_its_own_clock_and_a_shorter_repeat_can_be_asked_for():
    clock = Clock()
    alerts = monitoring.Alerts(clock)
    assert alerts.due("worker") and alerts.due("backup")
    assert alerts.due("error:a", repeat=60) and not alerts.due("error:a", repeat=60)
    clock.now += 60
    assert alerts.due("error:a", repeat=60) and not alerts.due("worker") and not alerts.due("backup")


def test_a_problem_that_clears_is_reported_at_once_if_it_comes_back():
    alerts = monitoring.Alerts(Clock())
    assert alerts.due("worker") and not alerts.due("worker")
    alerts.clear("worker")
    assert alerts.due("worker") is True
    alerts.clear("never-reported")                            # clearing what was never reported is harmless


def test_the_times_are_the_ones_the_documentation_promises():
    assert monitoring.REPEAT_SECONDS == 6 * 3600 and monitoring.ERROR_REPEAT_SECONDS == 30 * 60
    assert monitoring.WORKER_STALE_SECONDS == 15 * 60 and monitoring.BACKUP_STALE_DAYS == 2 and monitoring.REFRESH_FAILING_CYCLES == 3
    assert usage.KEEP_DAYS == 35


def test_the_real_clock_is_used_by_default():
    alerts = monitoring.Alerts()
    assert alerts.due("x") is True and alerts.due("x") is False


# --- the analysis worker ---------------------------------------------------------------------------------------------------------

def queue_status(pending=0, claimed=0, workers=()):
    return {"counts": {q.PENDING: pending, q.CLAIMED: claimed, q.DONE: 5, q.SKIPPED: 0, q.FAILED: 0}, "workers": list(workers)}


def test_nothing_waiting_is_never_a_problem_however_quiet_the_worker():
    assert monitoring.worker_problem(queue_status(workers=[("desk", 10 ** 6)])) is None
    assert monitoring.worker_problem(queue_status()) is None


def test_games_waiting_and_no_worker_ever_is_a_problem():
    assert monitoring.worker_problem(queue_status(pending=3)) == "3 games are waiting to be analysed and no analysis worker has ever asked for work."


def test_a_worker_silent_for_over_fifteen_minutes_while_games_wait_is_a_problem_and_at_fifteen_it_is_not():
    assert monitoring.worker_problem(queue_status(pending=2, workers=[("desk", 15 * 60)])) is None
    text = monitoring.worker_problem(queue_status(pending=2, claimed=1, workers=[("desk", 15 * 60 + 1)]))
    assert text == "The analysis worker last asked for work 15 min ago, and 3 games are waiting."


def test_it_is_the_most_recent_worker_that_counts():
    assert monitoring.worker_problem(queue_status(pending=1, workers=[("desk", 30), ("old", 10 ** 6)])) is None


def test_a_game_held_by_a_dead_worker_counts_as_waiting():
    assert monitoring.worker_problem(queue_status(claimed=1, workers=[("desk", 3600)])) == "The analysis worker last asked for work 1 h ago, and 1 games are waiting."


def test_the_real_queue_status_is_understood():
    assert monitoring.worker_problem(q.status(NOW)) is None


# --- the backups -------------------------------------------------------------------------------------------------------------------

def make_backups(folder, *days):
    for d in days:
        (folder / f"playmoreblitz-{d}.db").write_bytes(b"x")


def test_a_recent_backup_is_fine(tmp_path):
    make_backups(tmp_path, "2026-09-19", "2026-09-20")
    assert monitoring.backup_problem(tmp_path, date(2026, 9, 21)) is None                 # last night's is dated the day before
    make_backups(tmp_path, "2026-09-21")
    assert monitoring.backup_problem(tmp_path, date(2026, 9, 21)) is None


def test_a_backup_two_days_old_is_a_problem_and_the_message_says_how_old(tmp_path):
    make_backups(tmp_path, "2026-09-18", "2026-09-19")
    assert monitoring.backup_problem(tmp_path, date(2026, 9, 21)) == "The newest backup is from 2026-09-19, 2 days ago: the nightly backup isn't happening."
    make_backups(tmp_path, "2026-09-20")
    assert monitoring.backup_problem(tmp_path, date(2026, 9, 21)) is None                 # the newest one is what counts


def test_a_missing_or_empty_folder_and_stray_files_are_reported_or_ignored(tmp_path):
    assert monitoring.backup_problem(tmp_path / "gone", date(2026, 9, 21)) == f"The backup folder {tmp_path / 'gone'} is missing: is its disk mounted?"
    assert monitoring.backup_problem(tmp_path, date(2026, 9, 21)) == "There are no backups in the backup folder."
    (tmp_path / "notes.txt").write_text("hello")
    (tmp_path / "playmoreblitz-2026-09-20.db.partial").write_text("half")
    assert monitoring.backup_problem(tmp_path, date(2026, 9, 21)) == "There are no backups in the backup folder."


def test_today_is_the_real_today_by_default(tmp_path):
    make_backups(tmp_path, date.today().isoformat())
    assert monitoring.backup_problem(tmp_path) is None


# --- refreshes and errors ----------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("outcomes, failed", [
    ({"failed": 4}, True), ({"failed": 1}, True), ({}, False), ({"unchanged": 3, "updated": 1}, False),
    ({"failed": 1, "unchanged": 3}, False), ({"failed": 2, "skipped": 1}, False)])
def test_a_refresh_cycle_that_failed_throughout(outcomes, failed):
    assert monitoring.refresh_failed(outcomes) is failed


def test_failing_refreshes_are_reported_from_the_third_cycle_running():
    assert monitoring.refresh_problem(0) is None and monitoring.refresh_problem(2) is None
    assert monitoring.refresh_problem(3) == "The last 3 refresh cycles all failed: are Chess.com and Lichess reachable from the Minix?"
    assert "The last 7 refresh" in monitoring.refresh_problem(7)


def test_an_error_alert_names_the_command_and_the_error_and_is_cut_short():
    text = monitoring.error_alert("!obit", ValueError("bad value"))
    assert text == "!obit hit an unexpected error (ValueError: bad value). The log has the details."
    assert monitoring.error_alert("!obit", ValueError("bad value"), 1433) == "!obit from 1433 hit an unexpected error (ValueError: bad value). The log has the details."
    assert monitoring.error_alert("/obit", ValueError("x"), 0).startswith("/obit from 0 hit")                 # an ID of zero is still an ID
    wrapped = RuntimeError("outer")
    wrapped.original = KeyError("inner")
    assert "KeyError: 'inner'" in monitoring.error_alert("!x", wrapped) and "outer" not in monitoring.error_alert("!x", wrapped)
    long = monitoring.error_alert("!x", RuntimeError("y" * 5000))
    assert len(long) < 450 and "y" * 286 in long and "y" * 287 not in long                   # 300 characters of "Type: message"


# --- the counts of use -------------------------------------------------------------------------------------------------------------------

def day(offset=0):
    return NOW + offset * 86400


def test_each_use_of_a_command_adds_to_its_count_for_the_day_and_people_are_counted_once():
    for user in (1, 2, 1, 1):
        usage.count("results", user, now=NOW)
    usage.count("obit", 1, now=NOW)
    usage.count("results", 3, now=day(1))
    data = usage.report(2, now=day(1))
    assert data["days"][0] == {"day": "2026-09-22", "commands": 1, "people": 1, "errors": 0}
    assert data["days"][1] == {"day": "2026-09-21", "commands": 5, "people": 2, "errors": 0}
    assert data["totals"] == {"results": 5, "obit": 1}


def test_a_count_without_a_person_notes_no_one_and_errors_and_sends_are_not_commands():
    usage.count("results", 1, now=NOW)
    usage.count(usage.ERROR, now=NOW)
    usage.count(usage.ERROR, now=NOW)
    usage.count(usage.OBIT_SENT, now=NOW)
    usage.count(usage.EXPORT_SENT, now=NOW)
    (today,) = usage.report(1, now=NOW)["days"]
    assert today == {"day": "2026-09-21", "commands": 1, "people": 1, "errors": 2}
    assert usage.report(1, now=NOW)["totals"] == {"results": 1, "error": 2, "obit_sent": 1, "export_sent": 1}


def test_the_report_has_a_line_for_every_day_asked_for_newest_first_even_when_nothing_happened():
    usage.count("results", 1, now=day(-3))
    data = usage.report(5, now=NOW)
    assert [d["day"] for d in data["days"]] == ["2026-09-21", "2026-09-20", "2026-09-19", "2026-09-18", "2026-09-17"]
    assert [d["commands"] for d in data["days"]] == [0, 0, 0, 1, 0]
    assert usage.report(2, now=NOW)["totals"] == {}                                         # three days back is outside two days


def test_the_report_leaves_out_days_before_its_first():
    usage.count("results", 9, now=day(-2))
    assert usage.report(2, now=NOW)["days"][-1]["people"] == 0 and usage.report(3, now=NOW)["days"][-1]["people"] == 1


def test_the_real_clock_is_used_when_none_is_given():
    usage.count("results", 1)
    assert usage.report(1)["totals"] == {"results": 1}


def test_counts_older_than_thirty_five_days_are_dropped_and_newer_ones_stay():
    usage.count("results", 1, now=day(-usage.KEEP_DAYS - 1))
    usage.count("results", 2, now=day(-usage.KEEP_DAYS))
    usage.count("results", 3, now=day(-1))
    usage.prune(now=NOW)
    with store.transaction() as conn:
        assert [r["day"] for r in conn.execute("SELECT day FROM usage_daily ORDER BY day")] == ["2026-08-17", "2026-09-20"]
        assert [r["user_id"] for r in conn.execute("SELECT user_id FROM usage_seen ORDER BY day")] == [2, 3]
    usage.count("old", 1, now=time.time() - 100 * 86400)
    usage.count("recent", 1)
    usage.prune()                                                                        # with the real clock
    with store.transaction() as conn:
        assert {r["name"] for r in conn.execute("SELECT name FROM usage_daily")} >= {"recent"} and "old" not in {r["name"] for r in conn.execute("SELECT name FROM usage_daily")}


def test_the_report_reads_as_a_small_table_the_most_used_commands_and_the_sends():
    for name, times in (("results", 5), ("obit", 3), ("export", 3), ("add", 1)):
        for _ in range(times):
            usage.count(name, 1, now=NOW)
    usage.count(usage.OBIT_SENT, now=NOW)
    usage.count(usage.EXPORT_SENT, now=NOW)
    usage.count(usage.EXPORT_SENT, now=NOW)
    usage.count(usage.ERROR, now=NOW)
    usage.count("results", 2, now=NOW)
    usage.count("results", 3, now=NOW)
    text = usage.render_report(usage.report(2, now=NOW))
    assert text.startswith("**Usage, last 2 days** (UTC days; counts only)\n```\n")
    lines = text.split("```")[1].strip("\n").split("\n")
    assert lines[0].split() == ["Day", "Commands", "People", "Errors"]
    assert lines[1].split() == ["2026-09-21", "14", "3", "1"] and lines[2].split() == ["2026-09-20", "0", "0", "0"]
    assert len({len(line) for line in lines}) == 1                                        # the columns line up
    assert text.endswith("Most used: results 7 · export 3 · obit 3 · add 1\nReviews sent: 1 · Exports sent: 2 · Errors: 1")


def test_the_report_says_when_nothing_has_been_used_and_is_singular_for_one_day():
    text = usage.render_report(usage.report(1, now=NOW))
    assert text.startswith("**Usage, last 1 day**") and "Most used: none yet" in text and "Reviews sent: 0 · Exports sent: 0 · Errors: 0" in text


def test_only_the_eight_most_used_commands_are_listed():
    for i in range(10):
        for _ in range(i + 1):
            usage.count(f"cmd{i}", now=NOW)
    listed = usage.render_report(usage.report(1, now=NOW)).split("Most used: ")[1].split("\n")[0].split(" · ")
    assert listed == [f"cmd{i} {i + 1}" for i in range(9, 1, -1)]

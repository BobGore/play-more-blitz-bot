"""The analysis queue: which games get in and how they are numbered, claiming, results, giving games back."""

import copy
import sqlite3
import threading
import time

import pytest

from analysis_helpers import moments_for

import analysis
import analysis_queue as q
import settings
import store

MONTH = "2026-09"
NOW = 1_790_000_000


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(settings, "ANALYSIS_FULL_PRIORITY_GAMES", 500)
    monkeypatch.setattr(settings, "ANALYSIS_MAX_GAMES", 1000)
    monkeypatch.setattr(settings, "ANALYSIS_CLAIM_MINUTES", 30)
    monkeypatch.setattr(settings, "ANALYSIS_MAX_ATTEMPTS", 5)


def register(*names, site="lichess"):
    for name in names:
        store.add_player(site, name, 1001, MONTH, 1500)


def game(n, white="alice_example", black="zed_example", *, site="lichess", month=MONTH, **extra):
    return {"site": site, "game_id": f"g{n:05d}", "month": month, "ended_at": 1_000_000 + n, "result": "white",
            "white_username": white, "black_username": black, **extra}


def rows(where="1 = 1", args=()):
    with store.transaction() as conn:
        return [dict(r) for r in conn.execute(f"SELECT * FROM game_analysis WHERE {where} ORDER BY ended_at", args)]


def by_id(game_id):
    (row,) = rows("game_id = ?", (game_id,))
    return row


def tiers(monkeypatch, full, maximum):
    monkeypatch.setattr(settings, "ANALYSIS_FULL_PRIORITY_GAMES", full)
    monkeypatch.setattr(settings, "ANALYSIS_MAX_GAMES", maximum)


# --- which games get in -------------------------------------------------------------------------------------

def test_a_members_game_is_queued_as_pending_with_what_the_refresher_knew():
    register("alice_example")
    g = game(1, time_control="180+0", ending="resigned", opening_site="Sicilian-Defense", eco_site="B20", white_rating=1510,
             black_rating=1490, white_rating_change=8, black_rating_change=-8)
    assert q.queue_games([g], NOW) == {q.QUEUED: 1}
    row = by_id("g00001")
    assert (row["status"], row["priority"], row["attempts"], row["queued_at"], row["skip_reason"]) == ("pending", 0, 0, NOW, None)
    assert (row["month"], row["ended_at"], row["result"], row["time_control"], row["ending"]) == (MONTH, 1_000_001, "white", "180+0", "resigned")
    assert (row["opening_site"], row["eco_site"], row["white_rating"], row["black_rating"]) == ("Sicilian-Defense", "B20", 1510, 1490)
    assert (row["white_rating_change"], row["black_rating_change"]) == (8, -8)
    assert row["white_accuracy"] is None and row["evals"] is None and row["analysed_at"] is None


def test_a_game_with_no_member_in_it_is_not_stored():
    register("alice_example")
    assert q.queue_games([game(1, "nobody_example", "stranger_example")], NOW) == {q.NOT_A_MEMBER: 1}
    assert rows() == []


def test_either_side_can_be_the_member():
    register("zed_example")
    assert q.queue_games([game(1)], NOW) == {q.QUEUED: 1}


def test_a_removed_player_s_games_are_not_queued():
    register("alice_example")
    store.remove_player("lichess", "alice_example")
    assert q.queue_games([game(1)], NOW) == {q.NOT_A_MEMBER: 1}


def test_usernames_match_whatever_their_capitals_and_the_game_keeps_its_own_spelling():
    register("Alice_Example")
    assert q.queue_games([game(1, "ALICE_EXAMPLE", "zed_example")], NOW) == {q.QUEUED: 1}
    assert by_id("g00001")["white_username"] == "ALICE_EXAMPLE"


def test_a_player_on_the_other_site_is_not_a_member_here():
    register("alice_example", site="chess.com")
    assert q.queue_games([game(1, site="lichess")], NOW) == {q.NOT_A_MEMBER: 1}
    assert q.queue_games([game(2, site="chess.com")], NOW) == {q.QUEUED: 1}


def test_queuing_the_same_games_again_changes_nothing_even_after_they_have_moved_on():
    register("alice_example")
    q.queue_games([game(1), game(2)], NOW)
    (claimed, *_) = q.claim("w", 1, NOW + 5)
    before = rows()
    assert q.queue_games([game(1), game(2)], NOW + 60) == {q.ALREADY_QUEUED: 2}
    assert rows() == before and by_id(claimed["game_id"])["status"] == "claimed"


def test_the_first_call_to_a_new_database_creates_the_table_and_its_indexes():
    q.queue_games([], NOW)
    with store.transaction() as conn:
        names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert {"game_analysis", "analysis_workers", "game_analysis_queue", "game_analysis_month",
            "game_analysis_white", "game_analysis_black"} <= names


def test_a_database_made_before_analysis_existed_gets_the_new_tables(tmp_path):
    old = sqlite3.connect(store.DB_PATH)
    old.executescript("CREATE TABLE players (site TEXT NOT NULL, username TEXT NOT NULL COLLATE NOCASE, added_by INTEGER NOT NULL, "
                      "added_at TEXT NOT NULL DEFAULT (datetime('now')), active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (site, username));"
                      "INSERT INTO players (site, username, added_by) VALUES ('lichess', 'alice_example', 1);")
    old.commit()
    old.close()
    assert q.queue_games([game(1)], NOW) == {q.QUEUED: 1}


def test_the_table_refuses_a_status_or_result_it_does_not_know():
    q.queue_games([], NOW)
    for column, value in (("status", "lost"), ("result", "both")):
        good = {"status": "pending", "result": "white", column: value}
        with pytest.raises(sqlite3.IntegrityError):
            with store.transaction() as conn:
                conn.execute("INSERT INTO game_analysis (site, game_id, month, ended_at, result, white_username, black_username, "
                             "status, queued_at) VALUES ('lichess', 'x', '2026-09', 1, ?, 'a', 'b', ?, 1)", (good["result"], good["status"]))


# --- numbering a member's games: normal, low priority, not analysed ---------------------------------------------------

def test_a_members_games_are_numbered_in_the_order_played_however_they_arrive(monkeypatch):
    tiers(monkeypatch, 3, 5)
    register("alice_example")
    scrambled = [game(n) for n in (6, 1, 8, 3, 5, 2, 7, 4)]
    assert q.queue_games(scrambled, NOW) == {q.QUEUED: 3, q.QUEUED_LOW: 2, q.OVER_LIMIT: 3}
    got = {r["game_id"]: (r["status"], r["priority"], r["skip_reason"]) for r in rows()}
    for n in (1, 2, 3):
        assert got[f"g{n:05d}"] == ("pending", 0, None)
    for n in (4, 5):
        assert got[f"g{n:05d}"] == ("pending", 1, None)
    for n in (6, 7, 8):
        assert got[f"g{n:05d}"] == ("skipped", 1, "over_monthly_limit")


def test_the_edges_of_the_bands_are_the_settings_themselves(monkeypatch):
    tiers(monkeypatch, 2, 3)
    register("alice_example")
    q.queue_games([game(n) for n in range(1, 5)], NOW)
    assert [(r["status"], r["priority"]) for r in rows()] == [("pending", 0), ("pending", 0), ("pending", 1), ("skipped", 1)]


def test_the_count_carries_across_calls_and_counts_both_colours(monkeypatch):
    tiers(monkeypatch, 3, 4)
    register("alice_example")
    q.queue_games([game(1), game(2, white="zed_example", black="alice_example")], NOW)
    q.queue_games([game(3, black="alice_example", white="other_example")], NOW + 60)
    assert q.queue_games([game(4)], NOW + 120) == {q.QUEUED_LOW: 1}
    assert q.queue_games([game(5)], NOW + 180) == {q.OVER_LIMIT: 1}


def test_a_new_month_starts_the_count_again(monkeypatch):
    tiers(monkeypatch, 1, 1)
    register("alice_example")
    q.queue_games([game(1), game(2)], NOW)
    assert q.queue_games([game(3, month="2026-10")], NOW) == {q.QUEUED: 1}


def test_each_member_has_their_own_count(monkeypatch):
    tiers(monkeypatch, 1, 1)
    register("alice_example", "bob_example")
    assert q.queue_games([game(1, "alice_example", "x_example"), game(2, "bob_example", "y_example")], NOW) == {q.QUEUED: 2}


def test_a_game_between_two_members_takes_the_better_priority(monkeypatch):
    tiers(monkeypatch, 1, 2)
    register("alice_example", "bob_example")
    # alice has played three games (normal, low, over); bob none.
    q.queue_games([game(n, "alice_example", "x_example") for n in (1, 2, 3)], NOW)
    assert q.queue_games([game(4, "alice_example", "bob_example")], NOW) == {q.QUEUED: 1}  # bob's first game: normal
    row = by_id("g00004")
    assert (row["status"], row["priority"]) == ("pending", 0)


def test_a_game_between_two_members_is_skipped_only_if_both_are_over(monkeypatch):
    tiers(monkeypatch, 1, 1)
    register("alice_example", "bob_example")
    q.queue_games([game(1, "alice_example", "x_example"), game(2, "bob_example", "y_example")], NOW)   # each has one
    q.queue_games([game(3, "alice_example", "x_example")], NOW)   # alice: 2nd, over
    assert q.queue_games([game(4, "alice_example", "bob_example")], NOW) == {q.OVER_LIMIT: 1}  # alice 3rd, bob 2nd: both over
    tiers(monkeypatch, 3, 3)   # with more room bob (two games so far, the skipped one counting) is fine again
    q.queue_games([game(5, "bob_example", "z_example")], NOW)
    assert by_id("g00005")["status"] == "pending"


def test_a_skipped_game_still_counts_towards_the_number(monkeypatch):
    tiers(monkeypatch, 1, 2)
    register("alice_example")
    q.queue_games([game(n) for n in range(1, 4)], NOW)
    assert by_id("g00003")["status"] == "skipped"
    assert q.queue_games([game(4)], NOW) == {q.OVER_LIMIT: 1}


def test_a_thousand_games_go_in_quickly():
    register("alice_example")
    start = time.monotonic()
    result = q.queue_games([game(n) for n in range(1, 1201)], NOW)
    assert result == {q.QUEUED: 500, q.QUEUED_LOW: 500, q.OVER_LIMIT: 200}
    assert time.monotonic() - start < 10


# --- claiming ------------------------------------------------------------------------------------------------------

def test_claiming_takes_normal_before_low_and_newest_first_within_each(monkeypatch):
    tiers(monkeypatch, 3, 6)
    register("alice_example")
    q.queue_games([game(n) for n in range(1, 7)], NOW)   # 1-3 normal, 4-6 low
    got = [g["game_id"] for g in q.claim("w", 10, NOW + 1)]
    assert got == ["g00003", "g00002", "g00001", "g00006", "g00005", "g00004"]


def test_a_claim_returns_what_the_worker_needs_and_marks_the_games(monkeypatch):
    register("alice_example")
    q.queue_games([game(1, black="Zed_Example")], NOW)
    (job,) = q.claim("desk", 5, NOW + 10)
    assert job == {"site": "lichess", "game_id": "g00001", "month": MONTH, "ended_at": 1_000_001, "white_username": "alice_example",
                   "black_username": "Zed_Example", "attempts": 1}
    row = by_id("g00001")
    assert (row["status"], row["claimed_by"], row["claimed_at"], row["attempts"]) == ("claimed", "desk", NOW + 10, 1)


def test_a_claim_respects_its_limit_and_leaves_the_rest_pending():
    register("alice_example")
    q.queue_games([game(n) for n in range(1, 6)], NOW)
    assert len(q.claim("w", 2, NOW)) == 2
    assert [r["status"] for r in rows()].count("pending") == 3
    assert q.claim("w", 0, NOW) == [] and q.claim("w", -3, NOW) == []


def test_only_pending_games_are_handed_out(monkeypatch):
    tiers(monkeypatch, 1, 1)
    register("alice_example")
    q.queue_games([game(1), game(2)], NOW)   # 1 pending, 2 skipped
    assert [g["game_id"] for g in q.claim("w", 10, NOW)] == ["g00001"]
    assert q.claim("w", 10, NOW) == []


def test_two_workers_never_get_the_same_game():
    register("alice_example")
    q.queue_games([game(n) for n in range(1, 61)], NOW)
    got = {"a": [], "b": []}
    start = threading.Barrier(2)

    def work(name):
        start.wait()
        for _ in range(20):
            got[name] += [g["game_id"] for g in q.claim(name, 3, NOW)]

    threads = [threading.Thread(target=work, args=(n,)) for n in got]
    [t.start() for t in threads]
    [t.join() for t in threads]
    everything = got["a"] + got["b"]
    assert len(everything) == len(set(everything)) == 60   # none handed out twice, none missed (how they split it is up to the scheduler)


def test_asking_for_work_is_noted_even_when_there_is_none():
    q.claim("desk", 5, NOW)
    q.claim("desk", 5, NOW + 100)
    q.claim("other", 5, NOW + 50)
    assert q.status(NOW + 160)["workers"] == [("desk", 60), ("other", 110)]


# --- a claim that goes stale --------------------------------------------------------------------------------------

def test_a_claim_expires_after_the_lease_and_the_game_goes_back_in_the_queue():
    register("alice_example")
    q.queue_games([game(1)], NOW)
    q.claim("dead", 1, NOW)
    lease = settings.ANALYSIS_CLAIM_MINUTES * 60
    assert q.claim("live", 5, NOW + lease - 1) == []          # one second short: still theirs
    (job,) = q.claim("live", 5, NOW + lease)                  # exactly the lease: expired
    assert job["game_id"] == "g00001" and job["attempts"] == 2
    assert by_id("g00001")["claimed_by"] == "live"


def test_a_game_that_keeps_going_stale_is_finally_failed(monkeypatch):
    monkeypatch.setattr(settings, "ANALYSIS_MAX_ATTEMPTS", 2)
    register("alice_example")
    q.queue_games([game(1)], NOW)
    lease = settings.ANALYSIS_CLAIM_MINUTES * 60
    q.claim("w", 1, NOW)
    q.claim("w", 1, NOW + lease)                              # second try
    assert q.claim("w", 1, NOW + 2 * lease) == []             # no third: failed
    row = by_id("g00001")
    assert row["status"] == "failed" and "too many times" in row["last_error"] and row["claimed_by"] is None


# --- results -------------------------------------------------------------------------------------------------------

def side(**over):
    return {"accuracy": 91.5, "acc_opening": 95.0, "acc_middle": 88.25, "acc_end": None, "inaccuracies": 3, "mistakes": 1,
            "blunders": 0, "acpl": 42, **over}


def result(game_id="g00001", **over):
    scores = [("cp", 20 + i) for i in range(60)]
    r = {"site": "lichess", "game_id": game_id, "method_version": 1, "engine": "Stockfish 19", "nodes": 200000, "plies": 60,
         "middle_ply": 14, "end_ply": None, "eval_ply20": 33, "evals": analysis.pack_evals(scores),
         "white": side(), "black": side(accuracy=77.0, inaccuracies=5, mistakes=2, blunders=1, acpl=88),
         "site_white_accuracy": 90.1, "site_black_accuracy": None}
    r.update(over)
    r.setdefault("moments", moments_for(r["white"], r["black"]))
    return r


def claimed_game(worker="desk", n=1):
    register("alice_example")
    q.queue_games([game(n)], NOW)
    q.claim(worker, 5, NOW + 1)
    return f"g{n:05d}"


def test_a_good_result_is_stored_in_full_and_the_game_is_done():
    claimed_game()
    assert q.submit("desk", [result()], NOW + 99) == [("lichess", "g00001", q.ACCEPTED, None)]
    row = by_id("g00001")
    assert (row["status"], row["analysed_at"], row["claimed_by"], row["last_error"]) == ("done", NOW + 99, None, None)
    assert (row["engine"], row["nodes"], row["method_version"], row["plies"]) == ("Stockfish 19", 200000, 1, 60)
    assert (row["middle_ply"], row["end_ply"], row["eval_ply20"]) == (14, None, 33)
    assert (row["white_accuracy"], row["white_acc_opening"], row["white_acc_middle"], row["white_acc_end"]) == (91.5, 95.0, 88.25, None)
    assert (row["white_inaccuracies"], row["white_mistakes"], row["white_blunders"], row["white_acpl"]) == (3, 1, 0, 42)
    assert (row["black_accuracy"], row["black_inaccuracies"], row["black_mistakes"], row["black_blunders"], row["black_acpl"]) == (77.0, 5, 2, 1, 88)
    assert (row["site_white_accuracy"], row["site_black_accuracy"]) == (90.1, None)
    assert analysis.unpack_evals(row["evals"]) == [("cp", 20 + i) for i in range(60)]


def test_sending_a_result_twice_changes_nothing_the_second_time():
    claimed_game()
    q.submit("desk", [result()], NOW + 10)
    changed = result(white=side(accuracy=10.0))
    assert q.submit("desk", [changed], NOW + 20) == [("lichess", "g00001", q.ALREADY_DONE, None)]
    row = by_id("g00001")
    assert row["white_accuracy"] == 91.5 and row["analysed_at"] == NOW + 10


def test_a_newer_method_replaces_an_older_result_but_not_the_other_way_round():
    claimed_game()
    q.submit("desk", [result()], NOW + 10)
    assert q.submit("desk", [result(method_version=2, white=side(accuracy=50.0))], NOW + 20)[0][2] == q.ACCEPTED
    assert by_id("g00001")["white_accuracy"] == 50.0 and by_id("g00001")["method_version"] == 2
    assert q.submit("desk", [result(method_version=1)], NOW + 30)[0][2] == q.ALREADY_DONE
    assert by_id("g00001")["method_version"] == 2


def test_results_for_games_that_are_not_theirs_are_refused_and_change_nothing():
    claimed_game(worker="desk")
    register("bob_example")
    q.queue_games([game(2, "bob_example", "y_example")], NOW)   # pending, never claimed
    reports = q.submit("intruder", [result("g00001"), result("g00002"), result("g99999")], NOW + 5)
    assert [r[2] for r in reports] == [q.REJECTED] * 3
    assert "not claimed by intruder" in reports[0][3] and "pending" in reports[1][3] and reports[2][3] == "no such game"
    assert by_id("g00001")["status"] == "claimed" and by_id("g00001")["white_accuracy"] is None


BAD = {
    "an accuracy over 100": lambda r: r["white"].update(accuracy=100.5),
    "a negative accuracy": lambda r: r["black"].update(acc_opening=-1),
    "a count that is not a number": lambda r: r["white"].update(mistakes="3"),
    "a fractional count": lambda r: r["white"].update(blunders=1.5),
    "a boolean count": lambda r: r["white"].update(inaccuracies=True),
    "more blunders than plies": lambda r: r["white"].update(blunders=61),
    "a missing count": lambda r: r["black"].update(blunders=None),
    "a huge average loss": lambda r: r["white"].update(acpl=5000),
    "NaN accuracy": lambda r: r["white"].update(accuracy=float("nan")),
    "infinite accuracy": lambda r: r["white"].update(accuracy=float("inf")),
    "no plies": lambda r: r.update(plies=0),
    "too many plies": lambda r: r.update(plies=1001),
    "evals of the wrong length": lambda r: r.update(evals=b"\x00\x00"),
    "evals that are too long": lambda r: r.update(evals=r["evals"] + b"\x00\x00"),
    "no plies at all": lambda r: r.update(plies=0, evals=b"", middle_ply=None, eval_ply20=None),
    "a thousand and one plies": lambda r: r.update(plies=1001, evals=b"\x00\x00" * 1001),
    "evals that are text": lambda r: r.update(evals="abc"),
    "a middlegame after the end": lambda r: r.update(middle_ply=61),
    "an endgame with no middlegame": lambda r: r.update(middle_ply=None, end_ply=20),
    "an endgame before the middlegame": lambda r: r.update(middle_ply=30, end_ply=20),
    "an evaluation beyond the cap": lambda r: r.update(eval_ply20=1500),
    "no engine name": lambda r: r.update(engine=""),
    "zero nodes": lambda r: r.update(nodes=0),
    "method version zero": lambda r: r.update(method_version=0),
    "a site accuracy out of range": lambda r: r.update(site_black_accuracy=120),
    "a missing field": lambda r: r.pop("engine"),
    "a missing side": lambda r: r.pop("black"),
}


@pytest.mark.parametrize("what", list(BAD))
def test_a_result_that_fails_its_checks_is_refused_and_the_game_stays_claimed(what):
    claimed_game()
    r = copy.deepcopy(result())
    BAD[what](r)
    ((_, _, outcome, why),) = q.submit("desk", [r], NOW + 5)
    assert outcome == q.REJECTED and why
    row = by_id("g00001")
    assert row["status"] == "claimed" and row["analysed_at"] is None and row["white_accuracy"] is None and row["evals"] is None


def test_one_bad_result_does_not_spoil_the_others_in_the_batch():
    register("alice_example")
    q.queue_games([game(1), game(2)], NOW)
    q.claim("desk", 5, NOW)
    reports = q.submit("desk", [result("g00001", plies=0), result("g00002"), "not an object"], NOW + 5)
    assert [r[2] for r in reports] == [q.REJECTED, q.ACCEPTED, q.REJECTED]
    assert by_id("g00001")["status"] == "claimed" and by_id("g00002")["status"] == "done"


def test_a_skipped_game_cannot_be_given_a_result():
    register("alice_example")
    q.queue_games([game(1)], NOW)
    q.claim("desk", 1, NOW)
    q.release("desk", "lichess", "g00001", skip_reason=q.TOO_SHORT)
    assert q.submit("desk", [result()], NOW + 5)[0][2] == q.REJECTED


# --- giving a game back ---------------------------------------------------------------------------------------------

def test_a_game_given_back_to_retry_goes_to_the_queue_with_its_error_noted():
    claimed_game()
    assert q.release("desk", "lichess", "g00001", error="the site timed out")
    row = by_id("g00001")
    assert (row["status"], row["claimed_by"], row["claimed_at"], row["attempts"], row["last_error"]) == ("pending", None, None, 1, "the site timed out")
    assert q.claim("desk", 1, NOW + 60)[0]["attempts"] == 2


def test_a_game_that_has_had_all_its_tries_is_failed_when_given_back(monkeypatch):
    monkeypatch.setattr(settings, "ANALYSIS_MAX_ATTEMPTS", 2)
    claimed_game()
    q.release("desk", "lichess", "g00001", error="oops")
    q.claim("desk", 1, NOW + 60)
    q.release("desk", "lichess", "g00001", error="oops again")
    assert by_id("g00001")["status"] == "failed" and by_id("g00001")["last_error"] == "oops again"
    assert q.claim("desk", 1, NOW + 120) == []


@pytest.mark.parametrize("reason", q.WORKER_SKIP_REASONS)
def test_a_game_the_worker_cannot_analyse_can_be_skipped_for_a_stated_reason(reason):
    claimed_game()
    assert q.release("desk", "lichess", "g00001", skip_reason=reason)
    row = by_id("g00001")
    assert (row["status"], row["skip_reason"], row["claimed_by"]) == ("skipped", reason, None)


def test_a_worker_cannot_invent_a_skip_reason():
    claimed_game()
    with pytest.raises(ValueError):
        q.release("desk", "lichess", "g00001", skip_reason="over_monthly_limit")
    with pytest.raises(ValueError):
        q.release("desk", "lichess", "g00001", skip_reason="whatever")
    assert by_id("g00001")["status"] == "claimed"


def test_only_the_worker_holding_a_game_can_give_it_back():
    claimed_game(worker="desk")
    assert not q.release("intruder", "lichess", "g00001")
    assert not q.release("desk", "lichess", "g99999")
    assert by_id("g00001")["status"] == "claimed"
    assert q.release("desk", "lichess", "g00001")
    assert not q.release("desk", "lichess", "g00001")   # already given back


def test_a_long_error_message_is_cut_short():
    claimed_game()
    q.release("desk", "lichess", "g00001", error="x" * 1000)
    assert len(by_id("g00001")["last_error"]) == 200


# --- the admin picture ----------------------------------------------------------------------------------------------

def test_the_status_counts_every_kind_of_game(monkeypatch):
    tiers(monkeypatch, 2, 3)
    register("alice_example")
    q.queue_games([game(n) for n in range(1, 6)], NOW)   # 2 normal, 1 low, 2 over
    (job,) = q.claim("desk", 1, NOW + 100)               # claims the newest normal one (game 2)
    q.submit("desk", [result(job["game_id"])], NOW + 150)
    s = q.status(NOW + 200)
    assert s["counts"] == {"pending": 2, "claimed": 0, "done": 1, "skipped": 2, "failed": 0}
    assert s["low_priority_pending"] == 1 and s["over_limit"] == 2
    assert s["oldest_pending_seconds"] == 200 and s["workers"] == [("desk", 100)]


def test_an_empty_queue_has_an_empty_status():
    s = q.status(NOW)
    assert s["counts"] == {"pending": 0, "claimed": 0, "done": 0, "skipped": 0, "failed": 0}
    assert s["oldest_pending_seconds"] is None and s["workers"] == [] and s["low_priority_pending"] == 0


# --- the clocks --------------------------------------------------------------------------------------------------------------------------

def test_a_result_with_clocks_stores_them_and_one_without_stores_none():
    claimed_game()
    packed = analysis.pack_clocks([300.0 - i for i in range(60)])
    assert q.submit("desk", [result(clocks=packed)], NOW + 5)[0][2] == q.ACCEPTED
    assert by_id("g00001")["clocks"] == packed
    claimed_game(n=2)
    assert q.submit("desk", [result("g00002")], NOW + 5)[0][2] == q.ACCEPTED
    assert by_id("g00002")["clocks"] is None


@pytest.mark.parametrize("clocks", [b"", b"\x00" * 118, b"\x00" * 122, "text", [1, 2]])
def test_clocks_of_the_wrong_size_or_kind_are_refused(clocks):
    claimed_game()
    (report,) = q.submit("desk", [result(clocks=clocks)], NOW + 5)
    assert report[2] == q.REJECTED and report[3] == "clocks must be two bytes for each ply, or null" and by_id("g00001")["status"] == q.CLAIMED


def test_a_newer_method_replaces_an_older_result_and_brings_its_clocks():
    claimed_game()
    q.submit("desk", [result(method_version=2)], NOW + 5)
    assert by_id("g00001")["clocks"] is None
    packed = analysis.pack_clocks([200.0] * 60)
    assert q.submit("desk", [result(method_version=3, clocks=packed)], NOW + 9)[0][2] == q.ACCEPTED
    assert by_id("g00001")["clocks"] == packed and by_id("g00001")["method_version"] == 3


def test_the_clocks_column_is_added_to_a_table_made_before_it_existed(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(store.SCHEMA)
    conn.execute("ALTER TABLE game_analysis DROP COLUMN clocks")
    conn.commit()
    conn.close()
    store.DB_PATH = path
    with store.transaction() as conn:
        assert "clocks" in {r["name"] for r in conn.execute("PRAGMA table_info(game_analysis)")}

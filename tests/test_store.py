import sqlite3

import pytest

import store


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")


ALICE, BOB = 1001, 1002


def test_a_new_player_is_added_with_a_month_row():
    assert store.add_player("chess.com", "alice", ALICE, "2026-09", 1500) == store.ADDED
    assert store.get_player("chess.com", "alice") == store.Player("chess.com", "alice", ALICE, True)
    row = store.month_row("chess.com", "alice", "2026-09")
    assert (row["start_rating"], row["end_rating"], row["games"], row["in_100gob"]) == (1500, 1500, 0, 0)
    assert row["last_game_at"] is None and row["closed_at"] is None


def test_one_per_site_refuses_a_second_account_on_the_same_site_and_writes_nothing():
    assert store.add_player("chess.com", "alice", ALICE, "2026-09", 1500, one_per_site=True) == store.ADDED
    assert store.add_player("chess.com", "alice_alt", ALICE, "2026-09", 1400, one_per_site=True) == store.LIMIT
    assert store.get_player("chess.com", "alice_alt") is None
    assert store.month_row("chess.com", "alice_alt", "2026-09") is None


def test_one_per_site_allows_one_account_on_each_site_and_leaves_other_owners_alone():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500, one_per_site=True)
    assert store.add_player("lichess", "alice_li", ALICE, "2026-09", 1400, one_per_site=True) == store.ADDED
    assert store.add_player("chess.com", "bobs", BOB, "2026-09", 1300, one_per_site=True) == store.ADDED


def test_the_limit_is_not_applied_unless_asked_for_which_is_how_admins_are_exempt():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)
    assert store.add_player("chess.com", "alice_alt", ALICE, "2026-09", 1400) == store.ADDED
    assert len(store.accounts_of(ALICE)) == 2


def test_removing_the_account_frees_the_slot():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500, one_per_site=True)
    store.remove_player("chess.com", "alice")
    assert store.add_player("chess.com", "alice_new", ALICE, "2026-09", 1450, one_per_site=True) == store.ADDED


def test_bringing_back_a_removed_account_is_refused_if_the_owner_now_has_another_on_that_site():
    store.add_player("chess.com", "old", ALICE, "2026-09", 1500, one_per_site=True)
    store.remove_player("chess.com", "old")
    store.add_player("chess.com", "new", ALICE, "2026-09", 1450, one_per_site=True)
    assert store.add_player("chess.com", "old", ALICE, "2026-09", 1500, one_per_site=True) == store.LIMIT
    assert store.get_player("chess.com", "old").active is False


def test_an_already_registered_account_reports_exists_not_limit():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500, one_per_site=True)
    store.add_player("lichess", "alice_li", ALICE, "2026-09", 1400, one_per_site=True)
    assert store.add_player("chess.com", "alice", ALICE, "2026-09", 1500, one_per_site=True) == store.EXISTS


def test_the_limit_compares_names_without_regard_to_case():
    store.add_player("chess.com", "Alice", ALICE, "2026-09", 1500, one_per_site=True)
    assert store.add_player("chess.com", "ALICE", ALICE, "2026-09", 1500, one_per_site=True) == store.EXISTS  # the same account
    assert store.add_player("chess.com", "other", ALICE, "2026-09", 1500, one_per_site=True) == store.LIMIT


def test_simultaneous_adds_by_one_owner_cannot_both_get_through(monkeypatch):
    """Force the worst interleaving: both adds finish checking and reach the write at the same
    moment. Only the write lock taken at the start keeps the second one from passing its check."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    both_at_the_write = threading.Barrier(2)
    real_connect = sqlite3.connect

    class Gate(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.lstrip().startswith("INSERT INTO players"):
                try:
                    both_at_the_write.wait(timeout=1.5)  # released if the other add gets here too
                except threading.BrokenBarrierError:
                    pass  # the other add is waiting for the write lock, which is the correct behaviour
            return super().execute(sql, *args)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: real_connect(*a, factory=Gate, **k))
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda n: store.add_player("chess.com", n, ALICE, "2026-09", 1500, one_per_site=True), ["acct_a", "acct_b"]))
    assert sorted(outcomes) == [store.ADDED, store.LIMIT]
    assert len(store.accounts_of(ALICE)) == 1


def test_adding_an_active_player_again_changes_nothing():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)
    assert store.add_player("chess.com", "alice", BOB, "2026-09", 1999) == store.EXISTS
    assert store.get_player("chess.com", "alice").added_by == ALICE  # not taken over
    assert store.month_row("chess.com", "alice", "2026-09")["start_rating"] == 1500


def test_usernames_match_case_insensitively():
    store.add_player("chess.com", "Alice", ALICE, "2026-09", 1500)
    assert store.add_player("chess.com", "ALICE", BOB, "2026-09", 1500) == store.EXISTS
    assert store.get_player("chess.com", "alice").username == "Alice"  # stored as first typed
    assert [p.username for p in store.find_active("aLiCe")] == ["Alice"]


def test_the_same_username_on_two_sites_is_two_players():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)
    assert store.add_player("lichess", "alice", ALICE, "2026-09", 1400) == store.ADDED
    assert [p.site for p in store.find_active("alice")] == ["chess.com", "lichess"]
    assert [p.site for p in store.find_active("alice", "lichess")] == ["lichess"]


def test_removing_marks_inactive_and_keeps_history():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)
    assert store.remove_player("chess.com", "alice") is True
    assert store.find_active("alice") == []
    assert store.active_players() == []
    assert store.get_player("chess.com", "alice").active is False
    assert store.month_row("chess.com", "alice", "2026-09") is not None  # history survives


def test_removing_twice_or_removing_a_stranger_is_false():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)
    assert store.remove_player("chess.com", "alice") is True
    assert store.remove_player("chess.com", "alice") is False
    assert store.remove_player("chess.com", "nobody") is False


def test_readding_a_removed_player_reactivates_and_passes_ownership():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)
    store.remove_player("chess.com", "alice")
    assert store.add_player("chess.com", "alice", BOB, "2026-09", 1700) == store.REACTIVATED
    player = store.get_player("chess.com", "alice")
    assert (player.active, player.added_by) == (True, BOB)


def test_readding_refreshes_the_spelling_and_the_month_rows_stay_attached():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)  # stored as typed
    store.remove_player("chess.com", "alice")
    assert store.add_player("chess.com", "Alice", ALICE, "2026-09", 1500) == store.REACTIVATED
    assert store.get_player("chess.com", "alice").username == "Alice"
    row = store.month_row("chess.com", "ALICE", "2026-09")  # found whatever the case
    assert row is not None and row["start_rating"] == 1500
    with store._transaction() as conn:  # the foreign key is still satisfied
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_readding_in_the_same_month_keeps_that_months_totals():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)
    with store._transaction() as conn:
        conn.execute("UPDATE monthly_results SET games = 12, wins = 7, end_rating = 1530 WHERE username = 'alice'")
    store.remove_player("chess.com", "alice")
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1600)
    row = store.month_row("chess.com", "alice", "2026-09")
    assert (row["games"], row["wins"], row["start_rating"], row["end_rating"]) == (12, 7, 1500, 1530)


def test_readding_in_a_later_month_gets_a_fresh_row_and_keeps_the_old_one():
    store.add_player("chess.com", "alice", ALICE, "2026-08", 1500)
    store.remove_player("chess.com", "alice")
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1620)
    assert store.month_row("chess.com", "alice", "2026-08")["start_rating"] == 1500
    assert store.month_row("chess.com", "alice", "2026-09")["start_rating"] == 1620


def test_find_active_skips_removed_players_and_active_players_lists_the_rest():
    store.add_player("chess.com", "bob", BOB, "2026-09", 1400)
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)
    store.add_player("lichess", "carol", ALICE, "2026-09", 1300)
    store.remove_player("chess.com", "bob")
    assert [(p.username, p.site) for p in store.active_players()] == [("alice", "chess.com"), ("carol", "lichess")]
    assert store.find_active("bob") == []


def test_unknown_lookups_return_none_or_empty():
    assert store.get_player("chess.com", "nobody") is None
    assert store.find_active("nobody") == []
    assert store.month_row("chess.com", "nobody", "2026-09") is None


OLD_SCHEMA = """
CREATE TABLE players (
    site TEXT NOT NULL, username TEXT NOT NULL COLLATE NOCASE, added_by INTEGER NOT NULL,
    added_at TEXT NOT NULL DEFAULT (datetime('now')), active INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (site, username));
CREATE TABLE monthly_results (
    site TEXT NOT NULL, username TEXT NOT NULL COLLATE NOCASE, month TEXT NOT NULL,
    start_rating INTEGER NOT NULL, end_rating INTEGER NOT NULL,
    games INTEGER NOT NULL DEFAULT 0, wins INTEGER NOT NULL DEFAULT 0, draws INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0, in_100gob INTEGER NOT NULL DEFAULT 0, last_game_at TEXT, closed_at TEXT,
    PRIMARY KEY (site, username, month), FOREIGN KEY (site, username) REFERENCES players (site, username));
INSERT INTO players (site, username, added_by) VALUES ('chess.com', 'Alice', 1001);
INSERT INTO monthly_results (site, username, month, start_rating, end_rating, games, wins)
    VALUES ('chess.com', 'Alice', '2026-09', 1500, 1510, 4, 3);
"""


def test_a_database_from_the_first_version_is_upgraded_in_place_and_keeps_its_data():
    conn = sqlite3.connect(store.DB_PATH)
    conn.executescript(OLD_SCHEMA)
    conn.commit()
    conn.close()

    row = store.month_row("chess.com", "alice", "2026-09")  # the first connection migrates
    assert (row["games"], row["wins"], row["end_rating"]) == (4, 3, 1510)  # nothing lost
    assert row["refreshed_at"] is None and row["refresh_error"] is None  # the new columns exist
    assert store.get_player("chess.com", "alice").active is True

    assert store.month_row("chess.com", "alice", "2026-09")["games"] == 4  # a second connection is fine too
    (result,) = store.results("2026-09")
    assert (result.username, result.games) == ("Alice", 4)


def add_alice(month="2026-09"):
    store.add_player("chess.com", "alice", ALICE, month, 1500)


def refresh_args(**overrides):
    args = dict(expected_watermark=None, games=2, wins=1, draws=0, losses=1, end_rating=1490,
                last_game_at="2026-09-05T12:00:00+00:00", now="2026-09-05T12:30:00+00:00")
    args.update(overrides)
    return args


def test_apply_refresh_adds_to_the_totals_and_moves_the_watermark():
    add_alice()
    assert store.apply_refresh("chess.com", "alice", "2026-09", **refresh_args()) is True
    assert store.apply_refresh(
        "chess.com", "alice", "2026-09",
        **refresh_args(expected_watermark="2026-09-05T12:00:00+00:00", games=3, wins=2, draws=1, losses=0,
                       end_rating=1520, last_game_at="2026-09-06T09:00:00+00:00"),
    ) is True
    row = store.month_row("chess.com", "alice", "2026-09")
    assert (row["games"], row["wins"], row["draws"], row["losses"]) == (5, 3, 1, 1)  # added, not replaced
    assert (row["end_rating"], row["last_game_at"]) == (1520, "2026-09-06T09:00:00+00:00")
    assert row["start_rating"] == 1500  # never moves


def test_a_refresh_with_a_stale_watermark_is_refused_so_games_are_never_counted_twice():
    add_alice()
    assert store.apply_refresh("chess.com", "alice", "2026-09", **refresh_args()) is True
    # A second refresh that read the row before the first one finished still expects "no watermark".
    assert store.apply_refresh("chess.com", "alice", "2026-09", **refresh_args()) is False
    assert store.month_row("chess.com", "alice", "2026-09")["games"] == 2


def test_apply_refresh_clears_the_previous_error_and_stamps_the_time():
    add_alice()
    store.record_refresh_error("chess.com", "alice", "2026-09", "couldn't reach chess.com")
    assert store.month_row("chess.com", "alice", "2026-09")["refresh_error"] == "couldn't reach chess.com"
    store.apply_refresh("chess.com", "alice", "2026-09", **refresh_args(games=0, wins=0, losses=0, end_rating=1500, last_game_at=None))
    row = store.month_row("chess.com", "alice", "2026-09")
    assert row["refresh_error"] is None and row["refreshed_at"] == "2026-09-05T12:30:00+00:00"


def test_an_error_leaves_the_totals_and_last_success_time_alone():
    add_alice()
    store.apply_refresh("chess.com", "alice", "2026-09", **refresh_args())
    store.record_refresh_error("chess.com", "alice", "2026-09", "x" * 500)
    row = store.month_row("chess.com", "alice", "2026-09")
    assert (row["games"], row["refreshed_at"]) == (2, "2026-09-05T12:30:00+00:00")
    assert len(row["refresh_error"]) == 200  # capped


def test_a_closed_month_is_never_touched():
    add_alice()
    with store._transaction() as conn:
        conn.execute("UPDATE monthly_results SET closed_at = '2026-10-01T09:00:00+00:00'")
    assert store.apply_refresh("chess.com", "alice", "2026-09", **refresh_args()) is False
    store.record_refresh_error("chess.com", "alice", "2026-09", "late")
    row = store.month_row("chess.com", "alice", "2026-09")
    assert (row["games"], row["refresh_error"]) == (0, None)


def test_refreshing_a_player_with_no_row_writes_nothing():
    assert store.apply_refresh("chess.com", "ghost", "2026-09", **refresh_args()) is False


def test_results_lists_active_players_including_ones_with_no_row_for_the_month():
    add_alice("2026-09")
    store.add_player("lichess", "bob", BOB, "2026-08", 1400)  # a row for August only
    store.add_player("chess.com", "gone", BOB, "2026-09", 1300)
    store.remove_player("chess.com", "gone")
    by_name = {r.username: r for r in store.results("2026-09")}
    assert set(by_name) == {"alice", "bob"}  # removed players are not shown
    assert by_name["alice"].has_row is True and by_name["alice"].start_rating == 1500
    assert by_name["bob"].has_row is False and (by_name["bob"].games, by_name["bob"].start_rating) == (0, None)


def test_results_can_be_limited_to_players_who_have_a_row_for_the_month():
    add_alice("2026-09")
    store.add_player("lichess", "late", BOB, "2026-10", 1400)  # registered in October: no September row
    assert {r.username for r in store.results("2026-09")} == {"alice", "late"}
    only = store.results("2026-09", require_row=True)
    assert [r.username for r in only] == ["alice"] and only[0].has_row is True


def test_results_carries_the_counts_the_flag_and_the_refresh_state():
    add_alice()
    store.apply_refresh("chess.com", "alice", "2026-09", **refresh_args(games=10, wins=6, draws=1, losses=3, end_rating=1530))
    with store._transaction() as conn:
        conn.execute("UPDATE monthly_results SET in_100gob = 1")
    store.record_refresh_error("chess.com", "alice", "2026-09", "site down")
    (r,) = store.results("2026-09")
    assert (r.games, r.wins, r.draws, r.losses, r.end_rating, r.in_100gob) == (10, 6, 1, 3, 1530, True)
    assert (r.refreshed_at, r.refresh_error) == ("2026-09-05T12:30:00+00:00", "site down")


def test_joining_100gob_sets_the_flag_once():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)
    assert store.month_row("chess.com", "alice", "2026-09")["in_100gob"] == 0
    assert store.join_100gob("chess.com", "alice", "2026-09") == store.JOINED
    assert store.month_row("chess.com", "alice", "2026-09")["in_100gob"] == 1
    assert store.join_100gob("chess.com", "alice", "2026-09") == store.ALREADY


def test_joining_matches_the_username_case_insensitively():
    store.add_player("chess.com", "Alice", ALICE, "2026-09", 1500)
    assert store.join_100gob("chess.com", "ALICE", "2026-09") == store.JOINED


def test_joining_only_touches_that_player_and_that_month():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)
    store.add_player("lichess", "alice", ALICE, "2026-09", 1400)
    store.add_player("chess.com", "bob", BOB, "2026-09", 1300)
    store.join_100gob("chess.com", "alice", "2026-09")
    flags = {(r.site, r.username): r.in_100gob for r in store.results("2026-09")}
    assert flags == {("chess.com", "alice"): True, ("lichess", "alice"): False, ("chess.com", "bob"): False}


def test_a_new_months_row_starts_out_of_the_challenge_so_players_opt_in_afresh():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)
    store.join_100gob("chess.com", "alice", "2026-09")
    store.remove_player("chess.com", "alice")
    store.add_player("chess.com", "alice", ALICE, "2026-10", 1520)  # a later month gets a fresh row
    assert store.month_row("chess.com", "alice", "2026-10")["in_100gob"] == 0
    assert store.month_row("chess.com", "alice", "2026-09")["in_100gob"] == 1  # history untouched


def test_joining_a_closed_month_or_as_an_unregistered_or_removed_player_is_refused():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)
    assert store.join_100gob("chess.com", "ghost", "2026-09") == store.NO_ROW  # not registered
    with store._transaction() as conn:
        conn.execute("UPDATE monthly_results SET closed_at = '2026-10-01T09:00:00+00:00'")
    assert store.join_100gob("chess.com", "alice", "2026-09") == store.NO_ROW  # closed
    assert store.month_row("chess.com", "alice", "2026-09")["in_100gob"] == 0
    store.remove_player("chess.com", "alice")
    assert store.join_100gob("chess.com", "alice", "2026-10") == store.NO_ROW  # removed


def test_joining_a_month_with_no_row_yet_remembers_the_sign_up():
    store.add_player("chess.com", "Alice", ALICE, "2026-09", 1500)
    assert store.signups("2026-10") == []
    assert store.join_100gob("chess.com", "alice", "2026-10") == store.JOINED
    assert store.join_100gob("chess.com", "alice", "2026-10") == store.ALREADY
    assert store.signups("2026-10") == ["Alice"]
    assert store.month_row("chess.com", "alice", "2026-10") is None  # no row is invented


def test_a_sign_up_is_applied_and_used_up_when_the_months_row_is_created():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)
    store.add_player("chess.com", "bob", BOB, "2026-09", 1400)
    store.join_100gob("chess.com", "alice", "2026-10")
    with store._transaction() as conn:
        assert store._open_month(conn, "chess.com", "alice", "2026-10", 1525) is True
        assert store._open_month(conn, "chess.com", "bob", "2026-10", 1410) is True
    assert store.month_row("chess.com", "alice", "2026-10")["in_100gob"] == 1  # signed up: starts in
    assert store.month_row("chess.com", "bob", "2026-10")["in_100gob"] == 0  # not: starts out
    assert store.signups("2026-10") == []  # consumed
    assert store.month_row("chess.com", "alice", "2026-10")["start_rating"] == 1525


def test_opening_a_month_that_already_has_a_row_changes_nothing():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)
    with store._transaction() as conn:
        assert store._open_month(conn, "chess.com", "alice", "2026-09", 9999) is False
    assert store.month_row("chess.com", "alice", "2026-09")["start_rating"] == 1500


def test_a_player_who_signed_up_gets_the_flag_when_readded_into_that_month():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)
    store.join_100gob("chess.com", "alice", "2026-10")
    store.remove_player("chess.com", "alice")
    assert store.signups("2026-10") == []  # a removed player isn't listed
    store.add_player("chess.com", "alice", ALICE, "2026-10", 1520)  # back in October
    assert store.month_row("chess.com", "alice", "2026-10")["in_100gob"] == 1


def test_sign_ups_for_different_months_are_independent():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)
    store.join_100gob("chess.com", "alice", "2026-10")
    store.join_100gob("chess.com", "alice", "2026-11")
    assert store.signups("2026-10") == ["alice"]
    assert store.signups("2026-11") == ["alice"]
    with store._transaction() as conn:
        store._open_month(conn, "chess.com", "alice", "2026-10", 1500)
    assert store.signups("2026-10") == [] and store.signups("2026-11") == ["alice"]  # only October's was used up


def test_signups_are_listed_in_name_order_across_sites():
    for site_name, name in (("lichess", "zed"), ("chess.com", "Amy"), ("lichess", "bob")):
        store.add_player(site_name, name, ALICE, "2026-09", 1500)
        store.join_100gob(site_name, name, "2026-10")
    assert store.signups("2026-10") == ["Amy", "bob", "zed"]


def final(site_name, username, games=10, wins=6, draws=1, losses=3, end_rating=1530, last_game_at="2026-09-30T20:00:00+00:00"):
    return dict(site=site_name, username=username, games=games, wins=wins, draws=draws, losses=losses,
                end_rating=end_rating, last_game_at=last_game_at)


NOW_ISO = "2026-10-01T00:30:00+00:00"


def test_unclosed_months_lists_finished_months_with_an_open_row_oldest_first():
    store.add_player("chess.com", "alice", ALICE, "2026-08", 1500)
    store.add_player("chess.com", "bob", BOB, "2026-09", 1400)
    store.add_player("lichess", "carol", ALICE, "2026-10", 1300)
    assert store.unclosed_months("2026-10") == ["2026-08", "2026-09"]  # October is the current month
    assert store.unclosed_months("2026-09") == ["2026-08"]
    assert store.unclosed_months("2026-08") == []


def test_unclosed_months_ignores_removed_players_and_closed_rows():
    store.add_player("chess.com", "alice", ALICE, "2026-08", 1500)
    store.add_player("chess.com", "bob", BOB, "2026-08", 1400)
    store.remove_player("chess.com", "alice")
    assert store.unclosed_months("2026-09") == ["2026-08"]  # bob's row is still open
    store.close_month("2026-08", "2026-09", [final("chess.com", "bob")], NOW_ISO)
    assert store.unclosed_months("2026-09") == []  # August is closed, and September hasn't finished
    assert store.unclosed_months("2026-10") == ["2026-09"]  # closing August opened September for bob


def test_open_players_are_the_active_players_with_an_open_row_for_the_month():
    store.add_player("chess.com", "Zed", ALICE, "2026-09", 1500)
    store.add_player("lichess", "amy", ALICE, "2026-09", 1400)
    store.add_player("chess.com", "old", BOB, "2026-09", 1300)
    store.add_player("chess.com", "elsewhere", BOB, "2026-08", 1300)
    store.remove_player("chess.com", "old")
    assert [(p.username, p.site) for p in store.open_players("2026-09")] == [("amy", "lichess"), ("Zed", "chess.com")]
    assert [p.username for p in store.open_players("2026-08")] == ["elsewhere"]


def test_closing_a_month_writes_the_final_figures_and_opens_the_next_month_from_the_closing_rating():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)
    assert store.close_month("2026-09", "2026-10", [final("chess.com", "alice", 40, 22, 3, 15, 1547)], NOW_ISO) == 1
    r = store.month_row("chess.com", "alice", "2026-09")
    assert (r["games"], r["wins"], r["draws"], r["losses"], r["end_rating"]) == (40, 22, 3, 15, 1547)
    assert (r["closed_at"], r["refreshed_at"], r["refresh_error"]) == (NOW_ISO, NOW_ISO, None)
    assert r["start_rating"] == 1500  # never moves
    nxt = store.month_row("chess.com", "alice", "2026-10")
    assert (nxt["start_rating"], nxt["end_rating"], nxt["games"], nxt["closed_at"], nxt["in_100gob"]) == (1547, 1547, 0, None, 0)


def test_closing_overwrites_any_running_totals_with_the_authoritative_ones():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)
    store.apply_refresh("chess.com", "alice", "2026-09", expected_watermark=None, games=99, wins=99, draws=0, losses=0,
                        end_rating=9999, last_game_at="2026-09-05T00:00:00+00:00", now="2026-09-05T00:00:00+00:00")
    store.close_month("2026-09", "2026-10", [final("chess.com", "alice", 3, 1, 1, 1, 1490)], NOW_ISO)
    r = store.month_row("chess.com", "alice", "2026-09")
    assert (r["games"], r["wins"], r["end_rating"]) == (3, 1, 1490)


def test_a_sign_up_for_the_next_month_is_applied_when_it_is_opened():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)
    store.add_player("chess.com", "bob", BOB, "2026-09", 1400)
    store.join_100gob("chess.com", "alice", "2026-10")
    store.close_month("2026-09", "2026-10", [final("chess.com", "alice"), final("chess.com", "bob")], NOW_ISO)
    assert store.month_row("chess.com", "alice", "2026-10")["in_100gob"] == 1
    assert store.month_row("chess.com", "bob", "2026-10")["in_100gob"] == 0
    assert store.signups("2026-10") == []  # used up


def test_the_challenge_flag_of_the_closed_month_is_kept_and_not_carried_over():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)
    store.join_100gob("chess.com", "alice", "2026-09")
    store.close_month("2026-09", "2026-10", [final("chess.com", "alice")], NOW_ISO)
    assert store.month_row("chess.com", "alice", "2026-09")["in_100gob"] == 1
    assert store.month_row("chess.com", "alice", "2026-10")["in_100gob"] == 0  # opt in afresh


def test_a_removed_players_open_row_is_closed_as_it_stands_without_needing_figures():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)
    store.add_player("chess.com", "gone", BOB, "2026-09", 1400)
    store.apply_refresh("chess.com", "gone", "2026-09", expected_watermark=None, games=4, wins=2, draws=0, losses=2,
                        end_rating=1410, last_game_at="2026-09-03T00:00:00+00:00", now="2026-09-03T00:00:00+00:00")
    store.remove_player("chess.com", "gone")
    store.close_month("2026-09", "2026-10", [final("chess.com", "alice")], NOW_ISO)
    gone = store.month_row("chess.com", "gone", "2026-09")
    assert (gone["closed_at"], gone["games"], gone["end_rating"]) == (NOW_ISO, 4, 1410)  # closed, figures untouched
    assert store.month_row("chess.com", "gone", "2026-10") is None  # and not carried into October
    assert store.unclosed_months("2026-10") == []


def test_closing_twice_changes_nothing_the_second_time():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)
    assert store.close_month("2026-09", "2026-10", [final("chess.com", "alice", end_rating=1547)], NOW_ISO) == 1
    assert store.close_month("2026-09", "2026-10", [final("chess.com", "alice", 999, end_rating=1)], "2026-10-02T00:00:00+00:00") == 0
    r = store.month_row("chess.com", "alice", "2026-09")
    assert (r["games"], r["end_rating"], r["closed_at"]) == (10, 1547, NOW_ISO)
    assert store.month_row("chess.com", "alice", "2026-10")["start_rating"] == 1547


def test_an_existing_next_month_row_is_kept_not_overwritten():
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)
    with store._transaction() as conn:  # October's row already exists, with a different start and some games
        store._open_month(conn, "chess.com", "alice", "2026-10", 1500)
        conn.execute("UPDATE monthly_results SET games = 3 WHERE month = '2026-10'")
    store.close_month("2026-09", "2026-10", [final("chess.com", "alice", end_rating=1547)], NOW_ISO)
    nxt = store.month_row("chess.com", "alice", "2026-10")
    assert (nxt["start_rating"], nxt["games"]) == (1500, 3)


def test_closing_is_all_or_nothing_if_the_write_fails_part_way(monkeypatch):
    store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)
    store.add_player("chess.com", "bob", BOB, "2026-09", 1400)
    real = store._open_month
    calls = []

    def flaky(conn, site_name, username, month, start):
        calls.append(username)
        if len(calls) == 2:
            raise sqlite3.OperationalError("disk full")  # the second player's next-month row fails
        return real(conn, site_name, username, month, start)

    monkeypatch.setattr(store, "_open_month", flaky)
    with pytest.raises(sqlite3.OperationalError):
        store.close_month("2026-09", "2026-10", [final("chess.com", "alice"), final("chess.com", "bob")], NOW_ISO)
    monkeypatch.setattr(store, "_open_month", real)
    for name in ("alice", "bob"):
        assert store.month_row("chess.com", name, "2026-09")["closed_at"] is None  # nothing was closed
        assert store.month_row("chess.com", name, "2026-10") is None  # and nothing was opened
    assert store.unclosed_months("2026-10") == ["2026-09"]


def test_closed_months_since_finds_recent_closes_only():
    store.add_player("chess.com", "alice", ALICE, "2026-08", 1500)
    store.close_month("2026-08", "2026-09", [final("chess.com", "alice")], "2026-09-01T00:30:00+00:00")
    store.close_month("2026-09", "2026-10", [final("chess.com", "alice")], "2026-10-01T00:30:00+00:00")
    assert store.closed_months_since("2026-09-28T00:00:00+00:00") == ["2026-09"]
    assert store.closed_months_since("2026-08-01T00:00:00+00:00") == ["2026-08", "2026-09"]
    assert store.closed_months_since("2026-11-01T00:00:00+00:00") == []


def test_an_announcement_can_be_claimed_only_once_and_released_to_try_again():
    assert store.claim_announcement("signup_call", "2026-10", "2026-09-24T08:00:00+00:00") is True
    assert store.claim_announcement("signup_call", "2026-10", "2026-09-24T08:05:00+00:00") is False
    assert store.claim_announcement("signup_call", "2026-11", "2026-10-25T09:00:00+00:00") is True  # another month
    assert store.claim_announcement("something_else", "2026-10", "2026-09-24T08:00:00+00:00") is True  # another kind
    store.release_announcement("signup_call", "2026-10")
    assert store.claim_announcement("signup_call", "2026-10", "2026-09-24T08:10:00+00:00") is True


def test_accounts_of_lists_only_that_owners_active_accounts_in_order():
    store.add_player("lichess", "zoe_li", ALICE, "2026-09", 1400)
    store.add_player("chess.com", "amy_cc", ALICE, "2026-09", 1500)
    store.add_player("chess.com", "bobs", BOB, "2026-09", 1300)
    store.add_player("chess.com", "gone", ALICE, "2026-09", 1200)
    store.remove_player("chess.com", "gone")
    assert [(p.username, p.site) for p in store.accounts_of(ALICE)] == [("amy_cc", "chess.com"), ("zoe_li", "lichess")]
    assert [p.username for p in store.accounts_of(BOB)] == ["bobs"]
    assert store.accounts_of(999) == []


def test_a_month_row_needs_its_player():
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as conn:
            conn.execute(
                "INSERT INTO monthly_results (site, username, month, start_rating, end_rating) VALUES ('chess.com', 'ghost', '2026-09', 1, 1)"
            )


def test_a_failed_add_leaves_no_half_written_player(monkeypatch):
    # If the month row can't be written, the player must not be left behind without it.
    real_connect = sqlite3.connect

    class Flaky(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.lstrip().startswith("INSERT OR IGNORE INTO monthly_results"):
                raise sqlite3.OperationalError("disk full")
            return super().execute(sql, *args)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: real_connect(*a, factory=Flaky, **k))
    with pytest.raises(sqlite3.OperationalError):
        store.add_player("chess.com", "alice", ALICE, "2026-09", 1500)

    monkeypatch.setattr(sqlite3, "connect", real_connect)  # back to a healthy database
    assert store.get_player("chess.com", "alice") is None


def test_check_location_passes_when_the_database_folder_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    store.check_location()  # no exception


def test_check_location_refuses_when_the_database_folder_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "not_mounted" / "test.db")
    with pytest.raises(SystemExit) as stopped:
        store.check_location()
    assert "not_mounted" in str(stopped.value)
    assert not (tmp_path / "not_mounted").exists()  # and it never made the folder


def test_a_missing_database_folder_is_not_quietly_replaced_by_an_empty_database(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "not_mounted" / "test.db")
    with pytest.raises(sqlite3.OperationalError):
        store.get_player("chess.com", "alice")
    assert not (tmp_path / "not_mounted").exists()


def test_the_database_path_can_be_set_from_the_environment(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    def path_seen(**env):
        base = {k: v for k, v in os.environ.items() if k != "PLAYMOREBLITZ_DB"}
        code = "import store; print(store.DB_PATH)"
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                             cwd=Path(store.__file__).parent, env={**base, **env})
        return Path(out.stdout.strip())

    target = tmp_path / "elsewhere" / "pmb.db"
    assert path_seen(PLAYMOREBLITZ_DB=str(target)) == target
    assert path_seen().name == "playmoreblitz.db"
    assert path_seen(PLAYMOREBLITZ_DB="").name == "playmoreblitz.db"  # an empty setting means "not set"

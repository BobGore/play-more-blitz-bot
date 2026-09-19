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


def test_results_carries_the_counts_the_flag_and_the_refresh_state():
    add_alice()
    store.apply_refresh("chess.com", "alice", "2026-09", **refresh_args(games=10, wins=6, draws=1, losses=3, end_rating=1530))
    with store._transaction() as conn:
        conn.execute("UPDATE monthly_results SET in_100gob = 1")
    store.record_refresh_error("chess.com", "alice", "2026-09", "site down")
    (r,) = store.results("2026-09")
    assert (r.games, r.wins, r.draws, r.losses, r.end_rating, r.in_100gob) == (10, 6, 1, 3, 1530, True)
    assert (r.refreshed_at, r.refresh_error) == ("2026-09-05T12:30:00+00:00", "site down")


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

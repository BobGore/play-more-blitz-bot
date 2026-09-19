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

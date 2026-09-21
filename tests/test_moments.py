"""The flagged moves (inaccuracies, mistakes, blunders) kept for each analysed game: worked out, validated, stored,
and used to bring older games up to date."""

import copy
import json
import sqlite3

import pytest
from analysis_helpers import MONTH, NOW, moments_for, register, side, spec

import analysis
import analysis_queue as q
import game_records
import store


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")


def cp(*values):
    return [("cp", v) for v in values]


# The ten-ply game worked by hand in test_analysis.py: White blunders on ply 5, Black gives 70 back on ply 8.
GAME = cp(30, 30, 30, 30, -170, -170, -170, -100, -100, -100)


# --- working the moments out ---------------------------------------------------------------------------------------

def test_the_moments_of_a_worked_game_are_the_blunder_and_the_inaccuracy_with_what_they_cost():
    s = analysis.summarise(GAME, middle=4, end=8)
    assert [(m.ply, m.verdict) for m in s.moments] == [(5, "blunder"), (8, "inaccuracy")]
    assert s.moments[0].lost == pytest.approx(17.9, abs=0.05)      # 50 * (tanh(30 k / 2) - tanh(-170 k / 2)) = 17.9 points
    assert s.moments[1].lost == pytest.approx(6.1, abs=0.05)


def test_a_white_move_is_on_an_odd_ply_and_a_black_move_on_an_even_one_and_the_counts_agree():
    s = analysis.summarise(GAME, 4, 8)
    for colour, side_ in (("white", s.white), ("black", s.black)):
        mine = [m for m in s.moments if (m.ply % 2 == 1) == (colour == "white")]
        assert [sum(m.verdict == v for m in mine) for v in ("inaccuracy", "mistake", "blunder")] == [side_.inaccuracies, side_.mistakes, side_.blunders]


def test_a_move_that_was_the_engines_own_choice_is_not_a_moment():
    played = ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6", "d2d3", "f8c5", "c2c3", "d7d6"]
    bests = list(played)
    bests[7] = "c5f2"
    s = analysis.summarise(GAME, 4, 8, bests=bests, played=played)
    assert [(m.ply, m.verdict) for m in s.moments] == [(8, "inaccuracy")]


def test_the_moments_come_in_the_order_they_were_played():
    # Black throws away a good position on ply 4 (0 to +200 for White), then White does the same on ply 5 (200 to 0).
    scores = cp(0, -200, 0, 200, 0, -300)
    assert [(m.ply, m.verdict) for m in analysis.summarise(scores, None, None).moments] == [(4, "blunder"), (5, "blunder")]


def test_a_game_with_no_errors_has_no_moments():
    assert analysis.summarise(cp(15, 15, 15, 15), None, None).moments == ()


def test_a_forced_mate_walked_into_is_a_moment_that_cost_a_lot():
    scores = cp(10, 10, 10, 10) + [("mate", -3), ("mate", -2), ("mate", -1)]
    (m,) = analysis.summarise(scores, None, None).moments
    assert (m.ply, m.verdict) == (5, "blunder") and m.lost > 40


def test_the_cost_is_never_negative_and_never_over_a_hundred():
    scores = cp(0, 900, -900, 900, -900, 900, -900, 900)
    for m in analysis.summarise(scores, None, None).moments:
        assert 0 <= m.lost <= 100


# --- JSON -----------------------------------------------------------------------------------------------------------------

def test_moments_survive_a_round_trip_through_json():
    moments = analysis.summarise(GAME, 4, 8).moments
    assert analysis.moments_from_json(analysis.moments_to_json(moments)) == moments
    assert json.loads(analysis.moments_to_json(moments)) == [[5, "b", moments[0].lost], [8, "i", moments[1].lost]]
    assert analysis.moments_to_lists(moments)[0][:2] == [5, "b"]


def test_a_game_analysed_before_moments_were_kept_has_none():
    assert analysis.moments_from_json(None) == () and analysis.moments_from_json("") == ()
    assert analysis.moments_to_json(()) == "[]" and analysis.moments_from_json("[]") == ()


# --- links to a ply -----------------------------------------------------------------------------------------------------------

def test_a_lichess_link_can_open_the_game_at_a_ply_and_a_chesscom_one_does_not_yet():
    assert game_records.game_url("lichess", "abcd1234", 34) == "https://lichess.org/abcd1234#34"
    assert game_records.game_url("lichess", "abcd1234") == "https://lichess.org/abcd1234"
    assert game_records.game_url("chess.com", "live/99", 34) == "https://www.chess.com/game/live/99"


# --- validating what the worker sends -----------------------------------------------------------------------------------------

def result(game_id="00000001", **over):
    w, b = side(), side(inaccuracies=3, mistakes=1, blunders=0)
    r = {"site": "lichess", "game_id": game_id, "method_version": analysis.METHOD_VERSION, "engine": "Stockfish 19", "nodes": 200000, "plies": 40,
         "middle_ply": 14, "end_ply": None, "eval_ply20": 10, "evals": analysis.pack_evals(cp(*range(40))), "white": w, "black": b}
    r["moments"] = moments_for(w, b)
    r.update(over)
    return r


def claimed(worker="desk"):
    register("alice_example")
    q.queue_games([spec(1)], NOW)
    q.claim(worker, 5, NOW + 1)


def _swap_first_black(r, ply):
    """Move Black's first flagged move to `ply`, an even number (or something that only pretends to be), so that the
    counts of each side still agree and only the check on the ply itself can refuse it."""
    index = next(i for i, m in enumerate(r["moments"]) if m[0] % 2 == 0)
    r["moments"][index] = [ply, r["moments"][index][1], r["moments"][index][2]]


BAD = {
    "moments that are not a list": lambda r: r.update(moments="none"),
    "moments missing": lambda r: r.pop("moments"),
    "more moments than plies": lambda r: r.update(moments=[[i % 40 + 1, "i", 1.0] for i in range(41)]),
    "a moment that is not a list": lambda r: r["moments"].__setitem__(0, "x"),
    "a moment with two parts": lambda r: r["moments"].__setitem__(0, [1, "i"]),
    "a ply of zero": lambda r: r["moments"].__setitem__(0, [0, "i", 5.0]),
    "a ply beyond the game": lambda r: r["moments"].__setitem__(0, [41, "i", 5.0]),
    "a fractional ply": lambda r: r["moments"].__setitem__(0, [1.5, "i", 5.0]),
    "a code that is not i, m or b": lambda r: r["moments"].__setitem__(0, [1, "x", 5.0]),
    "a negative loss": lambda r: r["moments"].__setitem__(0, [1, "i", -1.0]),
    "a loss over a hundred": lambda r: r["moments"].__setitem__(0, [1, "i", 101.0]),
    "a loss that is text": lambda r: r["moments"].__setitem__(0, [1, "i", "5"]),
    "a black ply of zero": lambda r: _swap_first_black(r, 0),
    "a black ply beyond the game": lambda r: _swap_first_black(r, 42),
    "a whole-number ply written as a float": lambda r: _swap_first_black(r, 2.0 if r["moments"][1][0] != 2 else 4.0),
    "the same ply twice": lambda r: r["moments"].__setitem__(1, list(r["moments"][0])),
    "fewer moments than the count": lambda r: r["moments"].pop(),
    "an extra moment": lambda r: r["moments"].append([39, "b", 20.0]),
    "a moment on the wrong side": lambda r: r["moments"].__setitem__(0, [r["moments"][0][0] + 1, r["moments"][0][1], r["moments"][0][2]]),
    "a moment of the wrong kind": lambda r: r["moments"].__setitem__(0, [r["moments"][0][0], "b" if r["moments"][0][1] == "i" else "i", 5.0]),
}


@pytest.mark.parametrize("what", list(BAD))
def test_moments_that_do_not_hold_together_are_refused_and_the_game_stays_claimed(what):
    claimed()
    r = copy.deepcopy(result())
    BAD[what](r)
    ((_, _, outcome, why),) = q.submit("desk", [r], NOW + 5)
    assert outcome == q.REJECTED and why
    with store.transaction() as conn:
        row = conn.execute("SELECT status, moments FROM game_analysis").fetchone()
    assert row["status"] == "claimed" and row["moments"] is None


def test_the_moments_are_stored_as_json_in_ply_order_and_read_back():
    claimed()
    r = result()
    r["moments"] = list(reversed(r["moments"]))               # the order they arrive in does not matter
    assert q.submit("desk", [r], NOW + 5)[0][2] == q.ACCEPTED
    with store.transaction() as conn:
        text = conn.execute("SELECT moments FROM game_analysis").fetchone()["moments"]
    stored = analysis.moments_from_json(text)
    assert [m.ply for m in stored] == sorted(m.ply for m in stored) and len(stored) == 8 + 4
    assert {m.verdict for m in stored} == {"inaccuracy", "mistake", "blunder"}


def test_a_game_with_no_errors_stores_an_empty_list_not_nothing():
    claimed()
    quiet = side(inaccuracies=0, mistakes=0, blunders=0)
    assert q.submit("desk", [result(white=quiet, black=quiet, moments=[])], NOW + 5)[0][2] == q.ACCEPTED
    with store.transaction() as conn:
        assert conn.execute("SELECT moments FROM game_analysis").fetchone()["moments"] == "[]"


# --- a table made before moments existed --------------------------------------------------------------------------------------

def test_a_game_table_without_the_moments_column_gets_it_and_keeps_its_rows(tmp_path):
    old = sqlite3.connect(store.DB_PATH)
    old.executescript("CREATE TABLE players (site TEXT NOT NULL, username TEXT NOT NULL COLLATE NOCASE, added_by INTEGER NOT NULL, "
                      "added_at TEXT NOT NULL DEFAULT (datetime('now')), active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (site, username));"
                      "INSERT INTO players (site, username, added_by) VALUES ('lichess', 'alice_example', 1);"
                      "CREATE TABLE game_analysis (site TEXT NOT NULL, game_id TEXT NOT NULL, month TEXT NOT NULL, ended_at INTEGER NOT NULL, "
                      "result TEXT NOT NULL, white_username TEXT NOT NULL, black_username TEXT NOT NULL, status TEXT NOT NULL, "
                      "priority INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, queued_at INTEGER NOT NULL, "
                      "method_version INTEGER, evals BLOB, PRIMARY KEY (site, game_id));"
                      "INSERT INTO game_analysis (site, game_id, month, ended_at, result, white_username, black_username, status, queued_at, method_version) "
                      "VALUES ('lichess', 'oldgame1', '2026-09', 5, 'white', 'alice_example', 'x', 'done', 1, 1);")
    old.commit()
    old.close()
    with store.transaction() as conn:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(game_analysis)")}
        assert "moments" in columns and conn.execute("SELECT moments FROM game_analysis").fetchone()["moments"] is None
    with store.transaction() as conn:                          # a second connection changes nothing
        assert conn.execute("SELECT COUNT(*) FROM game_analysis").fetchone()[0] == 1


# --- bringing older games up to date --------------------------------------------------------------------------------------------

def old_done_game(n, method=1):
    """A game finished by an older method: figures but no moments."""
    q.queue_games([spec(n)], NOW)
    with store.transaction() as conn:
        conn.execute("UPDATE game_analysis SET status = 'done', method_version = ?, white_accuracy = 77.0, evals = ? WHERE game_id = ?",
                     (method, analysis.pack_evals(cp(*range(40))), f"{n:08d}"))


def test_older_games_are_handed_out_after_everything_else_and_only_when_asked_for():
    register("alice_example")
    old_done_game(1)
    old_done_game(2)
    q.queue_games([spec(3)], NOW)                              # a new game, pending
    assert [j["game_id"] for j in q.claim("w", 10, NOW)] == ["00000003"]                     # not asked: no reruns
    q.release("w", "lichess", "00000003", error="again")
    got = [j["game_id"] for j in q.claim("w", 10, NOW + 1, rerun_below=2)]
    assert got == ["00000003", "00000002", "00000001"]         # the new game first, then the older ones newest first


def test_a_game_already_analysed_by_the_current_method_is_not_redone():
    register("alice_example")
    old_done_game(1, method=2)
    assert q.claim("w", 10, NOW, rerun_below=2) == []


def test_a_rerun_fills_only_the_space_left_in_the_batch():
    register("alice_example")
    for n in range(1, 6):
        old_done_game(n)
    q.queue_games([spec(9)], NOW)
    assert [j["game_id"] for j in q.claim("w", 3, NOW, rerun_below=2)] == ["00000009", "00000005", "00000004"]


def test_the_old_figures_stay_until_the_new_ones_replace_them_and_then_the_moments_arrive():
    register("alice_example")
    old_done_game(1)
    (job,) = q.claim("desk", 5, NOW, rerun_below=2)
    with store.transaction() as conn:
        row = conn.execute("SELECT status, white_accuracy, moments FROM game_analysis").fetchone()
    assert (row["status"], row["white_accuracy"], row["moments"]) == ("claimed", 77.0, None)
    assert q.submit("desk", [result("00000001", white=side(accuracy=91.0))], NOW + 9)[0][2] == q.ACCEPTED
    with store.transaction() as conn:
        row = conn.execute("SELECT status, white_accuracy, method_version, moments FROM game_analysis").fetchone()
    assert (row["status"], row["white_accuracy"], row["method_version"]) == ("done", 91.0, analysis.METHOD_VERSION) and row["moments"]
    assert job["attempts"] == 1


def test_a_rerun_that_fails_goes_back_in_the_queue_like_any_other_game():
    register("alice_example")
    old_done_game(1)
    q.claim("desk", 5, NOW, rerun_below=2)
    assert q.release("desk", "lichess", "00000001", error="the site was down")
    assert [j["game_id"] for j in q.claim("desk", 5, NOW + 10)] == ["00000001"]


def test_the_status_says_how_many_games_are_still_on_an_older_method():
    register("alice_example")
    old_done_game(1)
    old_done_game(2)
    old_done_game(3, method=2)
    assert q.status(NOW)["older_method"] == 0                  # not asked
    assert q.status(NOW, method_version=2)["older_method"] == 2
    assert q.status(NOW, method_version=1)["older_method"] == 0


def test_a_huge_list_of_moments_is_refused_by_size_before_it_is_looked_at():
    claimed()
    r = result(moments=[[1, "i", 1.0]] * 100_000)
    ((_, _, outcome, why),) = q.submit("desk", [r], NOW + 5)
    assert outcome == q.REJECTED and "no more entries than plies" in why


def test_a_game_finished_by_an_old_version_with_no_method_recorded_is_redone_too():
    register("alice_example")
    old_done_game(1)
    with store.transaction() as conn:
        conn.execute("UPDATE game_analysis SET method_version = NULL")
    assert [j["game_id"] for j in q.claim("w", 5, NOW, rerun_below=2)] == ["00000001"]
    assert q.status(NOW, method_version=2)["older_method"] == 0        # claimed now, so no longer counted as done


def test_the_gateway_offers_older_games_to_the_worker_because_it_knows_the_current_method():
    import io

    import worker_gateway as gw
    register("alice_example")
    old_done_game(1)
    jobs = gw.handle("desk", "claim 5", io.StringIO(""), NOW, q)
    assert [j["game_id"] for j in jobs] == ["00000001"]

"""!obit: reading a game link or id, finding the member's own game, jumping the analysis queue, the requests waiting for a
result, the review itself, and the command and its delivery by DM, run against a fake Discord context."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from analysis_helpers import MONTH, NOW, OWNER, analysed, moments_for, register, side, spec

import analysis
import analysis_queue as q
import bot as botmod
import obit
import refresh
import render_obit
import settings
import store

OK, NO = "✅", "❌"
ALICE, BOB = OWNER, 1002
CHANNEL = 555


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(settings, "ANALYSIS_ENABLED", True)


def curve(*cps, plies=40):
    """An evaluation curve as the analysis result carries it: the values given, then held level to `plies` positions."""
    values = list(cps) + [cps[-1]] * (plies - len(cps))
    return analysis.pack_evals([("cp", v) for v in values])


def row_of(n=1, *, site="lichess", **result_over):
    """The analysis-table row of game n after analysing it (both sides at the default figures unless overridden)."""
    if store.get_player(site, "alice_example") is None:
        register("alice_example", site=site)
    extra = {k: result_over.pop(k) for k in ("ending", "result", "opening_site", "eco_site", "white", "black") if k in result_over}
    spec_extra = {k: extra[k] for k in ("ending", "result", "opening_site", "eco_site") if k in extra}
    analysed(spec(n, site=site, **spec_extra), white=extra.get("white"), black=extra.get("black"), **result_over)
    game_id = f"{n:08d}" if site == "lichess" else f"live/{n}"
    return obit.game_row(site, game_id)


# --- reading a link or an id ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("https://lichess.org/q7ZvsdUF", [("lichess", "q7ZvsdUF")]),
    ("https://lichess.org/q7ZvsdUF/black", [("lichess", "q7ZvsdUF")]),
    ("https://lichess.org/q7ZvsdUF#45", [("lichess", "q7ZvsdUF")]),
    ("https://lichess.org/q7ZvsdUFabcd", [("lichess", "q7ZvsdUF")]),                       # the 12-character form with the player's id
    ("lichess.org/q7ZvsdUF", [("lichess", "q7ZvsdUF")]),
    ("<https://www.lichess.org/q7ZvsdUF?x=1>", [("lichess", "q7ZvsdUF")]),
    ("https://www.chess.com/game/live/12345678901", [("chess.com", "live/12345678901")]),
    ("https://www.chess.com/game/daily/555666?tab=analysis", [("chess.com", "daily/555666")]),
    ("chess.com/game/live/12345678901", [("chess.com", "live/12345678901")]),
    ("q7ZvsdUF", [("lichess", "q7ZvsdUF")]),
    ("12345678901", [("chess.com", "live/12345678901"), ("chess.com", "daily/12345678901")]),
    ("12345678", [("lichess", "12345678"), ("chess.com", "live/12345678"), ("chess.com", "daily/12345678")]),     # ambiguous: both are tried
])
def test_a_link_or_id_is_read_as_the_games_it_could_be(text, expected):
    assert obit.candidates(text) == expected


@pytest.mark.parametrize("text", ["", "   ", None, "hello", "q7Zvsd", "q7ZvsdUFF", "https://example.com/q7ZvsdUF", "https://chess.com/member/x",
                                  "12345", "https://www.chess.com/game/live/abc", "x" * 300, "q7Zv sdUF", "1" * 16])
def test_anything_else_is_not_a_game(text):
    assert obit.candidates(text) == []


# --- finding the member's own game --------------------------------------------------------------------------------------------------

def accounts_of(owner):
    return store.accounts_of(owner)


def test_a_game_is_found_only_among_the_members_own_accounts():
    register("alice_example")
    store.add_player("lichess", "bob_example", BOB, MONTH, 1500)
    analysed(spec(1, "alice_example", "rival_example"), spec(2, "rival_example", "bob_example"))
    row, account, side_ = obit.find_game(accounts_of(ALICE), [("lichess", "00000001")])
    assert row["game_id"] == "00000001" and account.username == "alice_example" and side_ == "white"
    assert obit.find_game(accounts_of(ALICE), [("lichess", "00000002")]) is None           # Bob's game
    assert obit.find_game(accounts_of(BOB), [("lichess", "00000002")])[2] == "black"
    assert obit.find_game(accounts_of(ALICE), [("lichess", "99999999")]) is None
    assert obit.find_game([], [("lichess", "00000001")]) is None


def test_a_game_on_the_other_site_is_not_the_members_even_if_the_name_matches():
    register("alice_example", site="chess.com")
    store.add_player("lichess", "alice_example", BOB, MONTH, 1500)                       # the same name on Lichess is someone else's account
    analysed(spec(1, "alice_example", "rival_example", site="lichess"))
    assert obit.game_row("lichess", "00000001") is not None                                  # the game is held
    assert obit.find_game(accounts_of(ALICE), [("lichess", "00000001")]) is None
    assert obit.find_game(accounts_of(BOB), [("lichess", "00000001")])[2] == "white"


def test_of_several_candidates_the_one_that_is_theirs_is_found():
    register("alice_example", site="chess.com")
    analysed(spec(5, "alice_example", "rival_example", site="chess.com"))
    got = obit.find_game(accounts_of(ALICE), obit.candidates("5" * 6 + "5"))
    assert got is None                                                                    # 5555555 isn't a game we hold
    got = obit.find_game(accounts_of(ALICE), [("chess.com", "daily/5"), ("chess.com", "live/5")])
    assert got[0]["game_id"] == "live/5"


def test_the_side_is_matched_ignoring_case():
    register("Alice_Example")
    analysed(spec(1, "rival_example", "ALICE_EXAMPLE"))
    assert obit.find_game(accounts_of(ALICE), [("lichess", "00000001")])[2] == "black"
    assert obit.side_of({"white_username": "A", "black_username": "b"}, "B") == "black"
    assert obit.side_of({"white_username": "A", "black_username": "b"}, "a") == "white"
    assert obit.side_of({"white_username": "A", "black_username": "b"}, "c") is None


def test_the_latest_game_is_the_most_recent_one_that_is_or_can_be_analysed():
    register("alice_example")
    register("alice_cc", site="chess.com")
    analysed(spec(1, "alice_example", "x_example"))
    q.queue_games([spec(3, "alice_example", "y_example"), spec(2, "alice_cc", "z_example", site="chess.com")], NOW)   # pending, newest is 3
    row, account, side_ = obit.latest_game(accounts_of(ALICE))
    assert row["game_id"] == "00000003" and row["status"] == q.PENDING and account.username == "alice_example" and side_ == "white"


def test_the_latest_game_across_two_sites_is_the_newer_one():
    register("alice_example")
    register("alice_cc", site="chess.com")
    q.queue_games([spec(1, "alice_example", "x_example", ended_at=1_781_000_000), spec(2, "z_example", "alice_cc", site="chess.com", ended_at=1_782_000_000)], NOW)
    row, account, side_ = obit.latest_game(accounts_of(ALICE))
    assert row["game_id"] == "live/2" and account.site == "chess.com" and side_ == "black"


def test_the_latest_game_passes_over_games_that_cannot_be_analysed_but_not_over_the_limit(monkeypatch):
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example"), spec(2, "alice_example", "y_example")], NOW)
    q.claim("desk", 5, NOW)
    q.release("desk", "lichess", "00000002", skip_reason=q.NOT_STANDARD_START)
    assert obit.latest_game(accounts_of(ALICE))[0]["game_id"] == "00000001"
    monkeypatch.setattr(settings, "ANALYSIS_FULL_PRIORITY_GAMES", 0)
    monkeypatch.setattr(settings, "ANALYSIS_MAX_GAMES", 0)
    q.queue_games([spec(3, "alice_example", "w_example")], NOW)
    got = obit.latest_game(accounts_of(ALICE))[0]
    assert got["game_id"] == "00000003" and got["skip_reason"] == q.OVER_MONTHLY_LIMIT


def test_a_failed_game_can_be_the_latest_because_asking_retries_it():
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example"), spec(2, "alice_example", "y_example")], NOW)
    with store.transaction() as conn:
        conn.execute("UPDATE game_analysis SET status = 'failed' WHERE game_id = '00000002'")
    assert obit.latest_game(accounts_of(ALICE))[0]["game_id"] == "00000002"


def test_a_request_is_given_up_after_a_day():
    assert obit.GIVE_UP_SECONDS == 24 * 3600


def test_no_games_means_no_latest_game():
    register("alice_example")
    assert obit.latest_game(accounts_of(ALICE)) is None and obit.latest_game([]) is None


# --- moving a game to the front of the queue --------------------------------------------------------------------------------------

def statuses():
    with store.transaction() as conn:
        return {r["game_id"]: (r["status"], r["priority"], r["skip_reason"], r["attempts"]) for r in conn.execute("SELECT * FROM game_analysis")}


def test_a_requested_game_is_claimed_before_the_rest():
    register("alice_example")
    q.queue_games([spec(n, "alice_example", "x_example") for n in (1, 2, 3)], NOW)                # 3 is newest, so first in the normal order
    assert q.prioritise("lichess", "00000001") == (q.PENDING, None)
    assert [g["game_id"] for g in q.claim("desk", 3, NOW)] == ["00000001", "00000003", "00000002"]


def test_prioritising_lifts_the_monthly_limit_and_retries_a_failed_game():
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example"), spec(2, "alice_example", "y_example")], NOW)
    with store.transaction() as conn:
        conn.execute("UPDATE game_analysis SET status = 'skipped', skip_reason = ? WHERE game_id = '00000001'", (q.OVER_MONTHLY_LIMIT,))
        conn.execute("UPDATE game_analysis SET status = 'failed', attempts = 5, last_error = 'boom' WHERE game_id = '00000002'")
    assert q.prioritise("lichess", "00000001") == (q.PENDING, None) and q.prioritise("lichess", "00000002") == (q.PENDING, None)
    assert statuses() == {"00000001": (q.PENDING, q.URGENT, None, 0), "00000002": (q.PENDING, q.URGENT, None, 0)}


def test_prioritising_leaves_a_done_claimed_or_unanalysable_game_alone():
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example"))
    q.queue_games([spec(2, "alice_example", "y_example"), spec(3, "alice_example", "z_example")], NOW)
    q.claim("desk", 1, NOW)                                                                     # claims 3 (newest)
    before = statuses()
    assert q.prioritise("lichess", "00000001") == (q.DONE, None) and q.prioritise("lichess", "00000003") == (q.CLAIMED, None)
    assert statuses() == before
    q.release("desk", "lichess", "00000003", skip_reason=q.TOO_SHORT)
    before = statuses()
    assert q.prioritise("lichess", "00000003") == (q.SKIPPED, q.TOO_SHORT) and statuses() == before
    assert q.prioritise("lichess", "nosuchgame") is None


# --- the requests waiting ---------------------------------------------------------------------------------------------------------

def test_a_request_is_kept_renewed_counted_and_closed_once():
    obit.add_request(ALICE, "lichess", "00000001", "alice_example", CHANNEL, 100)
    obit.add_request(ALICE, "lichess", "00000002", "alice_example", CHANNEL, 200)
    obit.add_request(BOB, "lichess", "00000001", "bob_example", CHANNEL, 150)
    obit.add_request(ALICE, "lichess", "00000001", "alice_example", 777, 300)                  # asking again renews, it doesn't add
    assert [(r["user_id"], r["game_id"], r["requested_at"], r["channel_id"]) for r in obit.outstanding()] == [
        (BOB, "00000001", 150, CHANNEL), (ALICE, "00000002", 200, CHANNEL), (ALICE, "00000001", 300, 777)]
    assert obit.waiting_count(ALICE) == 2 and obit.waiting_count(ALICE, ("lichess", "00000001")) == 1 and obit.waiting_count(1234) == 0
    assert obit.close_request(ALICE, "lichess", "00000001") is True and obit.close_request(ALICE, "lichess", "00000001") is False
    assert obit.waiting_count(ALICE) == 1 and obit.waiting_count(BOB) == 1


@pytest.mark.parametrize("status, skip_reason, age, expected", [
    (q.DONE, None, 0, obit.SEND), (q.DONE, None, 10 * obit.GIVE_UP_SECONDS, obit.SEND),
    (q.PENDING, None, 0, obit.WAIT), (q.CLAIMED, None, 60, obit.WAIT), (q.PENDING, None, obit.GIVE_UP_SECONDS, obit.WAIT),
    (q.PENDING, None, obit.GIVE_UP_SECONDS + 1, obit.LATE), (q.CLAIMED, None, obit.GIVE_UP_SECONDS + 1, obit.LATE),
    (q.FAILED, None, 0, obit.CANT), (q.SKIPPED, q.TOO_SHORT, 0, obit.CANT),
])
def test_what_to_do_with_a_request(status, skip_reason, age, expected):
    request = {"requested_at": 1000}
    assert obit.action_for(request, {"status": status, "skip_reason": skip_reason}, 1000 + age) == expected


def test_a_request_for_a_game_that_has_gone_cannot_be_answered():
    assert obit.action_for({"requested_at": 0}, None, 5) == obit.CANT and "no longer hold" in obit.cant_reason(None)


def test_every_reason_a_game_cannot_be_analysed_has_words():
    for reason in q.WORKER_SKIP_REASONS:
        assert obit.cant_reason({"status": q.SKIPPED, "skip_reason": reason}) == obit.SKIP_WORDS[reason] and obit.SKIP_WORDS[reason]
    assert "failed" in obit.cant_reason({"status": q.FAILED, "skip_reason": None})


# --- the review ---------------------------------------------------------------------------------------------------------------------

def review(row, side_="white", site="lichess", username="alice_example"):
    messages = render_obit.render_obit(username, site, row, side_)
    assert all(len(m) <= 2000 for m in messages)
    return "\n".join(messages)


def test_the_review_has_the_four_parts_in_order_and_names_the_game():
    row = row_of(1, opening_site="London System, Kamsky Variation", eco_site="D02")
    text = review(row)
    assert text.index("**OBIT**") < text.index("**O · Opening**") < text.index("**B · Blunders**") < text.index("**I · Interesting**") < text.index("**T · Takeaway**")
    assert "`alice_example` · Lichess · " in text and "· 5+5" in text
    assert "You won by resigned as White against rival_example" in text and "<https://lichess.org/00000001>" in text
    assert "London System (D02)" in text
    assert "the bot's own estimate" in text and "sent to you alone" in text


def test_the_review_is_from_the_players_own_side():
    row = row_of(1, result="black")
    white, black = review(row, "white"), review(row, "black", username="rival_example")
    assert "You lost by resigned as White against rival_example" in white
    assert "You won by resigned as Black against alice_example" in black
    assert "1 blunder · 2 mistakes · 5 inaccuracies   (them: 2 blunders · 3 mistakes · 7 inaccuracies)" in white
    assert "2 blunders · 3 mistakes · 7 inaccuracies   (them: 1 blunder · 2 mistakes · 5 inaccuracies)" in black


def test_a_draw_and_the_opponents_rating_are_worded_sensibly():
    register("alice_example")
    analysed(spec(1, result="draw", ending="repetition", black_rating=1523))
    text = review(obit.game_row("lichess", "00000001"))
    assert "You drew (repetition) as White against rival_example (1523)" in text


def test_the_opening_part_gives_accuracy_by_phase_the_weakest_phase_and_the_score_after_ten_moves():
    row = row_of(1, white=side(accuracy=80.0, acc_opening=95.0, acc_middle=70.0, acc_end=85.0), eval_ply20=40)
    text = review(row)
    assert "Accuracy 80%: opening 95%, middlegame 70%, endgame 85%. Your weakest phase here was the middlegame" in text
    assert "After 10 moves each the engine had you at +0.4." in text
    black = review(row, "black", username="rival_example")
    assert "After 10 moves each the engine had you at -0.4." in black                     # the same score seen from Black's side


def test_the_weakest_phase_is_named_from_a_gap_of_eight_points_and_from_two_phases():
    eight = review(row_of(1, white=side(acc_opening=80.0, acc_middle=72.0, acc_end=None)))
    assert "Your weakest phase here was the middlegame" in eight
    seven = review(row_of(2, white=side(acc_opening=80.0, acc_middle=73.0, acc_end=None)))
    assert "weakest" not in seven


def test_the_opening_part_leaves_out_what_it_does_not_know():
    row = row_of(1, white=side(acc_opening=None, acc_middle=None, acc_end=None), eval_ply20=None)
    text = review(row)
    assert "After 10 moves" not in text and "weakest" not in text and "opening -, middlegame -, endgame -" in text
    even = row_of(2, white=side(accuracy=80.0, acc_opening=80.0, acc_middle=77.0, acc_end=None))
    assert "weakest" not in review(even)                                                   # too close to call one


def test_the_worst_moments_are_listed_biggest_first_at_most_five_with_links_to_the_position_before():
    row = row_of(1)
    text = review(row)
    lines = [l for l in text.split("\n") if l.startswith("• ") and "%" in l and "→" in l]
    assert [l.split("  <")[0] for l in lines] == [
        "• 8. blunder, −25% (+0.1 → +0.1)", "• 6. mistake, −12% (+0.1 → +0.1)", "• 7. mistake, −12% (+0.1 → +0.1)",
        "• 1. inaccuracy, −6% (+0.1 → +0.0)", "• 2. inaccuracy, −6% (+0.0 → +0.0)"]
    assert lines[0].endswith("<https://lichess.org/00000001#14>") and lines[3].endswith("<https://lichess.org/00000001>")   # ply 1: the start, no anchor
    assert "Each link opens the position before the move." in text


def test_black_sees_only_black_moves_and_black_style_move_numbers():
    row = row_of(1)
    text = review(row, "black", username="rival_example")
    lines = [l for l in text.split("\n") if l.startswith("• ") and "→" in l]
    assert [l.split(",")[0] for l in lines] == ["• 11... blunder", "• 12... blunder", "• 8... mistake", "• 9... mistake", "• 10... mistake"]


def test_evaluations_are_shown_from_the_players_side():
    evals = curve(*([0] * 12), -300, 300)                   # before Black's move at ply 14 White is at -3.0, after it +3.0
    row = row_of(1, evals=evals, white=side(inaccuracies=0, mistakes=0, blunders=0), black=side(inaccuracies=0, mistakes=0, blunders=1))
    with store.transaction() as conn:
        conn.execute("UPDATE game_analysis SET moments = '[[14, \"b\", 40.0]]'")
    black = review(obit.game_row("lichess", "00000001"), "black", username="rival_example")
    assert "• 7... blunder, −40% (+3.0 → -3.0)" in black


def test_a_clean_game_says_so():
    quiet = side(inaccuracies=0, mistakes=0, blunders=0)
    text = review(row_of(1, white=quiet, black=quiet, evals=curve(0)))
    assert "No inaccuracies, mistakes or blunders by you. A clean game." in text
    assert "0 blunders · 0 mistakes · 0 inaccuracies" in text and "Nothing unusual: a steady game." in text and "Your worst moments" not in text


def test_chess_com_games_get_the_link_once_and_no_move_links():
    row = row_of(1, site="chess.com")
    text = review(row, site="chess.com")
    assert "<https://www.chess.com/game/live/1>" in text and text.count("chess.com/game") == 1
    assert "lichess.org" not in text and "Each link opens" not in text and "`alice_example` · Chess.com" in text


def test_a_game_analysed_before_moments_were_kept_still_gets_a_review():
    row = row_of(1)
    with store.transaction() as conn:
        conn.execute("UPDATE game_analysis SET moments = NULL")
    text = review(obit.game_row("lichess", "00000001"))
    assert "**B · Blunders**" in text and "Your worst moments" not in text


def test_a_damaged_evaluation_curve_does_not_stop_the_review():
    row = row_of(1)
    row["evals"] = b"\x01"
    text = review(row)
    assert "**I · Interesting**" in text and "→" not in text


def test_a_moment_beyond_the_curve_is_listed_without_scores():
    row = row_of(1)
    row["evals"] = curve(0, plies=10)
    assert "• 8. blunder, −25%  <" in review(row)                                                # ply 15 has no position stored


# what stands out

def test_losing_on_time_in_an_equal_position_is_called_out():
    row = row_of(1, ending="timeout", result="black", evals=curve(20))
    text = review(row)
    assert "You lost on time in a position the engine had at +0.2 for you: equal or better. The clock, not the position, decided this one." in text


def test_losing_on_time_in_a_worse_position_is_not_a_clock_story():
    text = review(row_of(1, ending="timeout", result="black", evals=curve(-150)))
    assert "lost on time" not in text


def test_the_edge_of_equal_or_better_is_45_percent():
    assert render_obit._win(("cp", -50), "white") >= render_obit.NOT_WORSE > render_obit._win(("cp", -60), "white")


def test_the_clock_story_and_the_curve_stories_are_told_from_blacks_side_too():
    lost_equal = review(row_of(1, ending="timeout", result="white", evals=curve(-20)), "black", username="rival_example")
    assert "You lost on time in a position the engine had at +0.2 for you: equal or better." in lost_equal
    assert "lost on time" not in review(row_of(2, ending="timeout", result="white", evals=curve(300)), "black", username="rival_example")
    won_worse = review(row_of(3, ending="timeout", result="black", evals=curve(300)), "black", username="rival_example")
    assert "You won on time from a position the engine had at -3.0 for you" in won_worse
    winning = review(row_of(4, result="white", evals=curve(*([0] * 9), -400, 0)), "black", username="rival_example")
    assert "You were clearly winning (+4.0 after 5...) and lost." in winning
    trouble = review(row_of(5, result="black", evals=curve(*([0] * 9), 400, 0)), "black", username="rival_example")
    assert "You were in real trouble (-4.0 after 5...) and won anyway." in trouble


def test_winning_on_time_from_a_worse_position_is_called_out():
    text = review(row_of(1, ending="timeout", result="white", evals=curve(-150)))
    assert "You won on time from a position the engine had at -1.5 for you" in text
    assert "won on time" not in review(row_of(2, ending="timeout", result="white", evals=curve(50)))


def test_a_win_thrown_away_is_called_out_at_its_peak():
    evals = curve(*([0] * 19), 400, *([0] * 10), -100)
    text = review(row_of(1, result="black", evals=evals))
    assert "You were clearly winning (+4.0 after 10...) and lost." in text
    drawn = review(row_of(2, result="draw", evals=evals))
    assert "and drew." in drawn
    assert "clearly winning" not in review(row_of(3, result="white", evals=evals))


def test_a_narrow_advantage_is_not_clearly_winning():
    assert "clearly winning" not in review(row_of(1, result="black", evals=curve(*([0] * 19), 250, 0)))


def test_a_lost_position_saved_is_called_out_at_its_low():
    evals = curve(*([0] * 9), -400, 0)
    text = review(row_of(1, result="white", evals=evals))
    assert "You were in real trouble (-4.0 after 5...) and won anyway." in text
    assert "and drew anyway" in review(row_of(2, result="draw", evals=evals))
    assert "real trouble" not in review(row_of(3, result="black", evals=evals))


def test_the_opponents_mistakes_are_counted_and_the_biggest_named():
    text = review(row_of(1))
    assert "Your opponent made 5 mistakes or blunders; the biggest, 11..., cost them 25%. Did you see it, and use it?" in text
    one = side(inaccuracies=0, mistakes=0, blunders=1)
    single = review(row_of(2, black=one, white=side(inaccuracies=0, mistakes=0, blunders=0)))
    assert "Your opponent made 1 mistake or blunder;" in single
    inaccuracies_only = review(row_of(3, black=side(inaccuracies=4, mistakes=0, blunders=0)))
    assert "Your opponent made" not in inaccuracies_only


def test_the_most_that_can_stand_out_is_three_and_all_three_are_shown():
    evals = curve(*([0] * 9), -400, *([0] * 9), 400, 20)
    text = review(row_of(1, ending="timeout", result="black", evals=evals))
    section = text.split("**I · Interesting**")[1].split("**T · Takeaway**")[0]
    assert section.count("• ") == 3


def test_the_score_is_shown_in_pawns_with_mates_and_the_players_sign():
    assert render_obit._eval(("cp", 35), "white") == "+0.3" and render_obit._eval(("cp", 35), "black") == "-0.3"
    assert render_obit._eval(("cp", -150), "white") == "-1.5" and render_obit._eval(("cp", 0), "black") == "+0.0"
    assert render_obit._eval(("mate", 3), "white") == "+M3" and render_obit._eval(("mate", 3), "black") == "-M3"
    assert render_obit._eval(("mate", -2), "white") == "-M2" and render_obit._eval(("mate", 0), "white") == "#"


def test_move_numbers_follow_chess_notation():
    assert [render_obit.move_label(p) for p in (1, 2, 3, 4, 45, 46)] == ["1.", "1...", "2.", "2...", "23.", "23..."]


def test_a_long_username_is_shortened_in_the_review():
    register("alice_example")
    analysed(spec(1, "alice_example", "r" * 60))
    assert "r" * 60 not in review(obit.game_row("lichess", "00000001"))


# --- the command ------------------------------------------------------------------------------------------------------------------

class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def make_ctx(author_id):
    return SimpleNamespace(author=SimpleNamespace(id=author_id), channel=SimpleNamespace(id=CHANNEL), message=SimpleNamespace(add_reaction=AsyncMock()),
                           send=AsyncMock(), typing=lambda: Typing(), command=MagicMock())


def ask(ctx, *args):
    asyncio.run(botmod.obit_command.callback(ctx, *args))


def said(ctx):
    return [c.args[0] for c in ctx.send.await_args_list]


def reactions(ctx):
    return [c.args[0] for c in ctx.message.add_reaction.await_args_list]


@pytest.fixture
def dms(monkeypatch):
    sent = []

    async def fake(user_id, messages):
        sent.append((user_id, list(messages)))
    monkeypatch.setattr(botmod, "_dm", fake)
    return sent


@pytest.fixture
def sites(monkeypatch):
    """Stand-in for the chess sites: each account's refresh is recorded and does whatever `sites.effect` says."""
    state = SimpleNamespace(calls=[], effect=None)

    async def fake(site, username, month=None):
        state.calls.append((site, username))
        if state.effect:
            state.effect()
    monkeypatch.setattr(refresh, "refresh_one", fake)
    return state


def forbidden():
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Cannot send messages to this user")


def test_asking_for_a_game_already_analysed_sends_the_review_by_dm_and_ticks_the_channel(dms, sites):
    register("alice_example")
    analysed(spec(1, "alice_example", "rival_example"))
    ctx = make_ctx(ALICE)
    ask(ctx, "https://lichess.org/00000001")
    assert reactions(ctx) == [OK] and said(ctx) == ["I've sent you the review by DM."]
    assert len(dms) == 1 and dms[0][0] == ALICE and "**OBIT**" in dms[0][1][0] and "<https://lichess.org/00000001>" in "\n".join(dms[0][1])
    assert obit.outstanding() == [] and sites.calls == []                                 # nothing left waiting, and no need to look at the sites


def test_the_channel_never_sees_the_review(dms, sites):
    register("alice_example")
    analysed(spec(1, "alice_example", "rival_example"))
    ctx = make_ctx(ALICE)
    ask(ctx)
    assert all("OBIT" not in t and "blunder" not in t and "Opening" not in t for t in said(ctx))


def test_no_game_means_the_latest_one(dms, sites):
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example"), spec(2, "y_example", "alice_example"))
    ask(make_ctx(ALICE))
    assert "You" in dms[0][1][0] and "as Black against y_example" in dms[0][1][0]


def test_a_game_not_yet_analysed_goes_to_the_front_of_the_queue_and_is_answered_when_it_is_done(dms, sites):
    register("alice_example")
    q.queue_games([spec(n, "alice_example", "x_example") for n in (1, 2, 3)], NOW)
    ctx = make_ctx(ALICE)
    ask(ctx, "00000001")
    assert reactions(ctx) == [OK] and said(ctx) == ["Analysing that game now: it's at the front of the queue. I'll DM you the review when it's done."]
    assert dms == [] and [(r["user_id"], r["game_id"], r["username"], r["channel_id"]) for r in obit.outstanding()] == [(ALICE, "00000001", "alice_example", CHANNEL)]
    assert [g["game_id"] for g in q.claim("desk", 1, NOW)] == ["00000001"]                 # ahead of the newer games

    asyncio.run(botmod._process_obit(obit.outstanding()[0]))                               # still with the worker: nothing yet
    assert dms == [] and len(obit.outstanding()) == 1
    q.release("desk", "lichess", "00000001")                                                # (put back; now it is analysed)
    q.claim("desk", 1, NOW)
    q.submit("desk", [{"site": "lichess", "game_id": "00000001", "method_version": analysis.METHOD_VERSION, "engine": "Stockfish 19", "nodes": 200_000,
                       "plies": 40, "middle_ply": 14, "end_ply": None, "eval_ply20": 10, "evals": curve(0), "white": side(), "black": side(),
                       "moments": moments_for(side(), side())}], NOW + 5)
    asyncio.run(botmod.obit_loop.coro())
    assert len(dms) == 1 and dms[0][0] == ALICE and obit.outstanding() == []
    asyncio.run(botmod.obit_loop.coro())
    assert len(dms) == 1                                                                    # answered once


def test_a_game_skipped_for_the_monthly_limit_is_queued_anyway(dms, sites):
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example")], NOW)
    with store.transaction() as conn:
        conn.execute("UPDATE game_analysis SET status = 'skipped', skip_reason = ?, priority = 1", (q.OVER_MONTHLY_LIMIT,))
    ctx = make_ctx(ALICE)
    ask(ctx, "00000001")
    assert reactions(ctx) == [OK] and statuses()["00000001"] == (q.PENDING, q.URGENT, None, 0)


def test_a_game_that_cannot_be_analysed_is_refused_with_the_reason_and_the_cooldown_is_refunded(dms, sites):
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example")], NOW)
    q.claim("desk", 1, NOW)
    q.release("desk", "lichess", "00000001", skip_reason=q.NOT_STANDARD_START)
    ctx = make_ctx(ALICE)
    ask(ctx, "00000001")
    assert reactions(ctx) == [NO] and "chess960" in said(ctx)[0] and obit.outstanding() == [] and dms == []
    ctx.command.reset_cooldown.assert_called_once_with(ctx)


def test_someone_elses_game_is_refused_and_nothing_is_queued_or_sent(dms, sites):
    register("alice_example")
    store.add_player("lichess", "bob_example", BOB, MONTH, 1500)
    q.queue_games([spec(1, "bob_example", "x_example")], NOW)
    ctx = make_ctx(ALICE)
    ask(ctx, "https://lichess.org/00000001")
    assert reactions(ctx) == [NO] and said(ctx) == ["I can't find that among your games. I only hold games played since you registered."]
    assert statuses()["00000001"][1] == q.NORMAL and obit.outstanding() == [] and dms == []
    ctx.command.reset_cooldown.assert_called_once_with(ctx)


def test_a_game_not_seen_yet_is_looked_for_on_the_sites_once_and_then_answered(dms, sites):
    register("alice_example")
    register("alice_cc", site="chess.com")
    sites.effect = lambda: q.queue_games([spec(7, "alice_example", "x_example")], NOW) if not statuses() else None
    ctx = make_ctx(ALICE)
    ask(ctx)
    assert sites.calls == [("chess.com", "alice_cc"), ("lichess", "alice_example")]                # both accounts, once each
    assert reactions(ctx) == [OK] and "front of the queue" in said(ctx)[0]


def test_when_the_sites_have_nothing_either_it_says_so(dms, sites):
    register("alice_example")
    ctx = make_ctx(ALICE)
    ask(ctx)
    assert reactions(ctx) == [NO] and said(ctx) == ["I don't hold any games of yours yet."] and len(sites.calls) == 1
    ctx = make_ctx(ALICE)
    ask(ctx, "00000042")
    assert "I can't find that among your games" in said(ctx)[0] and len(sites.calls) == 2


def test_a_game_that_is_found_at_once_makes_no_site_calls(dms, sites):
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example"))
    ask(make_ctx(ALICE), "00000001")
    assert sites.calls == []


def test_the_command_needs_an_account_and_analysis_switched_on(dms, sites, monkeypatch):
    ctx = make_ctx(ALICE)
    ask(ctx)
    assert reactions(ctx) == [NO] and "haven't added an account" in said(ctx)[0]
    register("alice_example")
    monkeypatch.setattr(settings, "ANALYSIS_ENABLED", False)
    ctx = make_ctx(ALICE)
    ask(ctx)
    assert reactions(ctx) == [NO] and "isn't switched on" in said(ctx)[0] and sites.calls == []
    ctx.command.reset_cooldown.assert_called_once_with(ctx)


def test_something_that_is_not_a_game_is_refused_before_anything_else(dms, sites):
    register("alice_example")
    ctx = make_ctx(ALICE)
    ask(ctx, "my last game please")
    assert reactions(ctx) == [NO] and "I can't read 'my last game please' as a game" in said(ctx)[0] and sites.calls == []
    ctx.command.reset_cooldown.assert_called_once_with(ctx)


def test_a_long_argument_is_cut_short_in_the_reply(dms, sites):
    register("alice_example")
    ctx = make_ctx(ALICE)
    ask(ctx, "x" * 500)
    assert len(said(ctx)[0]) < 250


def test_at_most_three_reviews_can_be_waiting(dms, sites):
    register("alice_example")
    q.queue_games([spec(n, "alice_example", "x_example") for n in range(1, 6)], NOW)
    for n in (1, 2, 3):
        ask(make_ctx(ALICE), f"{n:08d}")
    ctx = make_ctx(ALICE)
    ask(ctx, "00000004")
    assert reactions(ctx) == [NO] and "already have 3 reviews waiting" in said(ctx)[0] and obit.waiting_count(ALICE) == 3
    assert statuses()["00000004"][1] == q.NORMAL                                           # and that game wasn't moved up
    ctx.command.reset_cooldown.assert_called_once_with(ctx)
    again = make_ctx(ALICE)
    ask(again, "00000001")                                                                  # asking again about one already waiting is fine
    assert reactions(again) == [OK] and obit.waiting_count(ALICE) == 3


def test_a_member_who_cannot_receive_dms_is_told_in_the_channel_and_the_request_is_dropped(monkeypatch, sites):
    async def refuse(user_id, messages):
        raise forbidden()
    monkeypatch.setattr(botmod, "_dm", refuse)
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example"))
    ctx = make_ctx(ALICE)
    ask(ctx)
    assert reactions(ctx) == [NO] and "Allow direct messages" in said(ctx)[0] and obit.outstanding() == []


def test_a_waiting_request_whose_dm_is_refused_is_reported_to_the_channel_it_came_from(monkeypatch):
    async def refuse(user_id, messages):
        raise forbidden()
    channel = SimpleNamespace(send=AsyncMock())
    monkeypatch.setattr(botmod, "_dm", refuse)
    monkeypatch.setattr(botmod.bot, "get_channel", lambda channel_id: channel if channel_id == CHANNEL else None)
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example"))
    obit.add_request(ALICE, "lichess", "00000001", "alice_example", CHANNEL, 10 ** 10)
    asyncio.run(botmod.obit_loop.coro())
    (call,) = channel.send.await_args_list
    assert f"<@{ALICE}>" in call.args[0] and "Allow direct messages" in call.args[0] and obit.outstanding() == []
    assert [u.id for u in call.kwargs["allowed_mentions"].users] == [ALICE]                # pings only the person asking


def test_a_failed_send_is_retried_later_not_lost(monkeypatch):
    calls = []

    async def flaky(user_id, messages):
        calls.append(1)
        if len(calls) == 1:
            raise discord.HTTPException(SimpleNamespace(status=500, reason="oops"), "server error")
    monkeypatch.setattr(botmod, "_dm", flaky)
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example"))
    obit.add_request(ALICE, "lichess", "00000001", "alice_example", CHANNEL, 10 ** 10)
    asyncio.run(botmod.obit_loop.coro())
    assert len(obit.outstanding()) == 1 and obit.outstanding()[0]["requested_at"] == 10 ** 10
    asyncio.run(botmod.obit_loop.coro())
    assert len(calls) == 2 and obit.outstanding() == []


def test_two_answers_at_once_send_only_one_review(dms):
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example"))
    obit.add_request(ALICE, "lichess", "00000001", "alice_example", CHANNEL, 10 ** 10)
    request = obit.outstanding()[0]

    async def both():
        return await asyncio.gather(botmod._process_obit(request), botmod._process_obit(request))
    results = asyncio.run(both())
    assert sorted(results) == ["closed", "sent"] and len(dms) == 1


def test_a_request_for_a_game_that_failed_or_is_too_late_gets_an_apology_by_dm(dms):
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example"), spec(2, "alice_example", "y_example")], NOW)
    with store.transaction() as conn:
        conn.execute("UPDATE game_analysis SET status = 'failed' WHERE game_id = '00000001'")
    obit.add_request(ALICE, "lichess", "00000001", "alice_example", CHANNEL, 10 ** 10)
    obit.add_request(ALICE, "lichess", "00000002", "alice_example", CHANNEL, 1)                # asked long ago, still not analysed
    late, failed = obit.outstanding()
    assert asyncio.run(botmod._process_obit(late)) == "closed" and asyncio.run(botmod._process_obit(failed)) == "closed"
    assert [m[0] for _, m in dms] == ["I couldn't get to that game's review in time (the analysis is behind). Ask again in a while.",
                                      "I couldn't review that game: the analysis failed (the bot's admin can see why)."]
    assert obit.outstanding() == []


def test_the_loop_leaves_a_request_that_is_not_ready_and_survives_a_bad_one(dms, monkeypatch):
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example")], NOW)
    obit.add_request(ALICE, "lichess", "00000001", "alice_example", CHANNEL, 10 ** 10)
    asyncio.run(botmod.obit_loop.coro())
    assert dms == [] and len(obit.outstanding()) == 1
    analysed(spec(2, "alice_example", "y_example"))
    obit.add_request(ALICE, "lichess", "00000002", "alice_example", CHANNEL, 10 ** 10)
    real = botmod._process_obit

    async def explode_on_the_first(request, **kw):
        if request["game_id"] == "00000001":
            raise RuntimeError("bug")
        return await real(request, **kw)
    monkeypatch.setattr(botmod, "_process_obit", explode_on_the_first)
    asyncio.run(botmod.obit_loop.coro())
    assert len(dms) == 1 and [r["game_id"] for r in obit.outstanding()] == ["00000001"]


def test_the_command_carries_the_cooldown_and_a_usage_line_and_help_mentions_it():
    assert botmod.USAGE["obit"] == "!obit [game link or id]"
    assert botmod.obit_command._buckets is not None                                        # the same cooldown as !mystats
    ctx = make_ctx(ALICE)
    asyncio.run(botmod.help_blitz_bot.callback(ctx))
    assert "`!obit [game link or id]`" in said(ctx)[0] and len(said(ctx)[0]) < 2000


def statuses():
    with store.transaction() as conn:
        return {r["game_id"]: (r["status"], r["priority"], r["skip_reason"], r["attempts"]) for r in conn.execute("SELECT * FROM game_analysis")}


def test_a_refused_dm_on_the_spot_is_answered_once_by_the_command_not_also_posted_by_the_delivery(monkeypatch, sites):
    async def refuse(user_id, messages):
        raise forbidden()
    channel = SimpleNamespace(send=AsyncMock())
    monkeypatch.setattr(botmod, "_dm", refuse)
    monkeypatch.setattr(botmod.bot, "get_channel", lambda channel_id: channel)
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example"))
    ask(make_ctx(ALICE))
    channel.send.assert_not_awaited()


def test_the_delivery_loop_is_started_with_the_bot(monkeypatch):
    started = []
    for name in ("obit_loop", "refresh_loop", "daily_posts"):
        loop = getattr(botmod, name)
        monkeypatch.setattr(loop, "is_running", lambda: False)
        monkeypatch.setattr(loop, "start", lambda name=name: started.append(name))
    monkeypatch.setattr(botmod, "post_signup_call_if_due", AsyncMock())
    asyncio.run(botmod.on_ready())
    assert "obit_loop" in started
    assert botmod.obit_loop.seconds == 60


def test_the_delivery_loop_is_not_started_twice(monkeypatch):
    started = []
    for name in ("obit_loop", "refresh_loop", "daily_posts"):
        monkeypatch.setattr(getattr(botmod, name), "is_running", lambda: True)
        monkeypatch.setattr(getattr(botmod, name), "start", lambda name=name: started.append(name))
    asyncio.run(botmod.on_ready())
    assert started == []

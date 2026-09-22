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
DM_CHANNEL = 777
WAITING = "Analysing that game now: it's at the front of the queue. I'll DM you the review when it's done."


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(settings, "ANALYSIS_ENABLED", True)
    botmod._lookups.clear()
    monkeypatch.setitem(botmod._slash, "synced", False)


def curve(*cps, plies=40):
    """An evaluation curve as the analysis result carries it: the values given, then held level to `plies` positions."""
    values = list(cps) + [cps[-1]] * (plies - len(cps))
    return analysis.pack_evals([("cp", v) for v in values])


def row_of(n=1, *, site="lichess", **result_over):
    """The analysis-table row of game n after analysing it (both sides at the default figures unless overridden)."""
    if store.get_player(site, "alice_example") is None:
        register("alice_example", site=site)
    extra = {k: result_over.pop(k) for k in ("ending", "result", "opening_site", "eco_site", "time_control", "white", "black") if k in result_over}
    spec_extra = {k: extra[k] for k in ("ending", "result", "opening_site", "eco_site", "time_control") if k in extra}
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


def with_moments(row, moments):
    """`row` with its flagged moves replaced by `moments` ([[ply, "i"|"m"|"b", lost], ...]), as if the analysis had found those."""
    import json
    with store.transaction() as conn:
        conn.execute("UPDATE game_analysis SET moments = ? WHERE site = ? AND game_id = ?", (json.dumps(moments), row["site"], row["game_id"]))
    return obit.game_row(row["site"], row["game_id"])


def section(text, letter):
    """One part of a review, from its heading to the next: "B" for Blunders, "I" for Interesting."""
    order = ["**OBIT**", "**O ·", "**B ·", "**I ·", "**T ·"]
    start = text.index(f"**{letter} ·")
    later = [text.index(h) for h in order if h in text and text.index(h) > start]
    return text[start:min(later)] if later else text[start:]


def test_the_worst_moments_are_listed_biggest_first_at_most_five_with_links_to_the_position_before():
    row = row_of(1)
    text = review(row)
    lines = [l for l in section(text, "B").split("\n") if l.startswith("• ") and "%" in l and "→" in l]
    assert [l.split("  <")[0] for l in lines] == [
        "• 8. blunder, −25% (+0.1 → +0.1)", "• 6. mistake, −12% (+0.1 → +0.1)", "• 7. mistake, −12% (+0.1 → +0.1)",
        "• 1. inaccuracy, −6% (+0.1 → +0.0)", "• 2. inaccuracy, −6% (+0.0 → +0.0)"]
    assert lines[0].endswith("<https://lichess.org/00000001#14>") and lines[3].endswith("<https://lichess.org/00000001>")   # ply 1: the start, no anchor
    assert "Each link opens the position before the move." in text


def test_black_sees_only_black_moves_and_black_style_move_numbers():
    row = row_of(1)
    text = review(row, "black", username="rival_example")
    lines = [l for l in section(text, "B").split("\n") if l.startswith("• ") and "→" in l]
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
    assert "won on time" not in review(row_of(2, ending="timeout", result="white", evals=curve(100)))            # a pawn better: the board decided it


def test_winning_on_time_in_a_level_position_is_called_out():
    text = review(row_of(1, ending="timeout", result="white", evals=curve(9)))                                # your own game's shape: +0.1 at the end
    assert "You won on time in a level position (the engine had it at +0.1 for you): the clock, not the board, decided this one." in text
    black = review(row_of(2, ending="timeout", result="black", evals=curve(-20)), "black", username="rival_example")
    assert "You won on time in a level position (the engine had it at +0.2 for you)" in black


def test_a_level_position_is_from_45_up_to_55_percent():
    assert render_obit.LEVEL == 55 and render_obit.NOT_WORSE == 45
    edge_in = review(row_of(1, ending="timeout", result="white", evals=curve(50)))                              # 54.6%: still level
    assert "in a level position" in edge_in
    edge_out = review(row_of(2, ending="timeout", result="white", evals=curve(60)))                             # 55.5%: a little better, no note
    assert "won on time" not in edge_out
    lower = review(row_of(3, ending="timeout", result="white", evals=curve(-50)))                               # 45.4%: level at its lower edge
    assert "in a level position" in lower
    below = review(row_of(4, ending="timeout", result="white", evals=curve(-60)))                               # 44.5%: worse, the other note
    assert "from a position the engine had at -0.6 for you: your opponent's clock did the work" in below and "level position" not in below


def test_winning_a_level_game_by_resignation_is_not_a_clock_story():
    assert "level position" not in review(row_of(1, ending="resigned", result="white", evals=curve(9)))


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
    assert "Your opponent made 5 mistakes or blunders; the biggest, 11..., cost them 25% (+0.2 → +0.2). Did you see it, and use it?" in text
    one = side(inaccuracies=0, mistakes=0, blunders=1)
    single = review(row_of(2, black=one, white=side(inaccuracies=0, mistakes=0, blunders=0)))
    assert "Your opponent made 1 mistake or blunder;" in single
    inaccuracies_only = review(row_of(3, black=side(inaccuracies=4, mistakes=0, blunders=0)))
    assert "Your opponent made" not in inaccuracies_only


def test_the_most_that_can_stand_out_is_five_and_all_five_are_shown():
    # lost on time, equal at the end (1); clearly winning earlier (2); the opponent's blunder (4); your own worse one is the turning point (5);
    # and your reply to their blunder was a slip (6). Test 3 can't hold as well: you lost.
    evals = curve(*([0] * 9), -400, *([0] * 9), 400, 20)
    row = with_moments(row_of(1, ending="timeout", result="black", evals=evals), [[14, "b", 15.0], [15, "m", 12.0], [21, "b", 30.0]])
    notes = section(review(row), "I").count("• ")
    assert notes == 5


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
    """A direct message to the bot from `author_id` (no guild)."""
    return SimpleNamespace(author=SimpleNamespace(id=author_id), channel=SimpleNamespace(id=DM_CHANNEL), guild=None,
                           message=SimpleNamespace(add_reaction=AsyncMock()), send=AsyncMock(), typing=lambda: Typing(), command=MagicMock())


def ask(ctx, *args):
    asyncio.run(botmod.obit_command.callback(ctx, *args))


def said(ctx):
    return [c.args[0] for c in ctx.send.await_args_list]


def reactions(ctx):
    return [c.args[0] for c in ctx.message.add_reaction.await_args_list]


def the_server(*member_ids, error=None, guild_id=1):
    """A stand-in server whose member lookup finds `member_ids` and otherwise raises `error` (NotFound by default)."""
    async def fetch_member(user_id):
        if user_id in member_ids:
            return SimpleNamespace(id=user_id)
        raise error or discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Member")
    return SimpleNamespace(id=guild_id, fetch_member=AsyncMock(side_effect=fetch_member))


@pytest.fixture(autouse=True)
def on_the_server(monkeypatch):
    """The bot sees the blitz channel, in a server that Alice and Bob are members of."""
    channel = SimpleNamespace(id=CHANNEL, guild=the_server(ALICE, BOB), send=AsyncMock())
    monkeypatch.setattr(botmod.bot, "get_channel", lambda channel_id: channel if channel_id == CHANNEL else None)
    return channel


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


def test_asking_for_a_game_already_analysed_sends_the_review_by_dm_and_only_reacts_in_the_channel(dms, sites):
    register("alice_example")
    analysed(spec(1, "alice_example", "rival_example"))
    ctx = make_ctx(ALICE)
    ask(ctx, "https://lichess.org/00000001")
    assert reactions(ctx) == [OK] and said(ctx) == []                                     # a reaction and no text: quiet
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
    assert reactions(ctx) == ["⏳"] and said(ctx) == [WAITING]                              # queued: an hourglass and a line, and the DM follows
    assert dms == [] and [(r["user_id"], r["game_id"], r["username"], r["channel_id"]) for r in obit.outstanding()] == [(ALICE, "00000001", "alice_example", DM_CHANNEL)]
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
    assert reactions(ctx) == ["⏳"] and statuses()["00000001"] == (q.PENDING, q.URGENT, None, 0)


def test_a_game_that_cannot_be_analysed_is_refused_with_the_reason(dms, sites):
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example")], NOW)
    q.claim("desk", 1, NOW)
    q.release("desk", "lichess", "00000001", skip_reason=q.NOT_STANDARD_START)
    ctx = make_ctx(ALICE)
    ask(ctx, "00000001")
    assert reactions(ctx) == [NO] and "chess960" in said(ctx)[0] and obit.outstanding() == [] and dms == []
    assert ctx.send.await_args.kwargs == {}                                                # private: the text stays


def test_someone_elses_game_is_refused_and_nothing_is_queued_or_sent(dms, sites):
    register("alice_example")
    store.add_player("lichess", "bob_example", BOB, MONTH, 1500)
    q.queue_games([spec(1, "bob_example", "x_example")], NOW)
    ctx = make_ctx(ALICE)
    ask(ctx, "https://lichess.org/00000001")
    assert reactions(ctx) == [NO] and said(ctx) == ["I can't find that among your games. I only hold games played since you registered."]
    assert statuses()["00000001"][1] == q.NORMAL and obit.outstanding() == [] and dms == []


def test_a_game_not_seen_yet_is_looked_for_on_the_sites_once_and_then_answered(dms, sites):
    register("alice_example")
    register("alice_cc", site="chess.com")
    sites.effect = lambda: q.queue_games([spec(7, "alice_example", "x_example")], NOW) if not statuses() else None
    ctx = make_ctx(ALICE)
    ask(ctx)
    assert sites.calls == [("chess.com", "alice_cc"), ("lichess", "alice_example")]                # both accounts, once each
    assert reactions(ctx) == ["⏳"] and said(ctx) == [WAITING]


def test_when_the_sites_have_nothing_either_it_says_so_and_looks_again_only_after_a_couple_of_minutes(dms, sites, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(botmod, "_monotonic", lambda: clock[0])
    register("alice_example")
    ctx = make_ctx(ALICE)
    ask(ctx)
    assert reactions(ctx) == [NO] and said(ctx) == ["I don't hold any games of yours yet."] and len(sites.calls) == 1
    ctx = make_ctx(ALICE)
    ask(ctx, "00000042")                                                                    # a moment later: not looked for again
    assert "I looked on the sites for your games a moment ago" in said(ctx)[0] and len(sites.calls) == 1
    clock[0] += botmod.LOOKUP_GAP_SECONDS - 1
    ask(make_ctx(ALICE), "00000042")
    assert len(sites.calls) == 1
    clock[0] += 1
    ctx = make_ctx(ALICE)
    ask(ctx, "00000042")
    assert "I can't find that among your games" in said(ctx)[0] and len(sites.calls) == 2


def test_with_no_game_named_the_sites_are_looked_at_first_so_a_game_just_played_is_the_latest(dms, sites):
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example"))                                          # analysed long ago
    sites.effect = lambda: q.queue_games([spec(9, "alice_example", "new_opponent")], NOW) if "00000009" not in statuses() else None
    ctx = make_ctx(ALICE)
    ask(ctx)
    assert sites.calls == [("lichess", "alice_example")] and reactions(ctx) == ["⏳"]
    assert [r["game_id"] for r in obit.outstanding()] == ["00000009"]                        # the new one, not the analysed old one


def test_the_sites_are_looked_at_once_per_person_per_couple_of_minutes_and_the_review_still_comes(dms, sites, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(botmod, "_monotonic", lambda: clock[0])
    register("alice_example")
    store.add_player("lichess", "bob_example", BOB, MONTH, 1500)
    analysed(spec(1, "alice_example", "x_example"), spec(2, "bob_example", "y_example"))
    ask(make_ctx(ALICE))
    ask(make_ctx(ALICE))
    assert sites.calls == [("lichess", "alice_example")] and len(dms) == 2                  # the second review used what is held
    ask(make_ctx(BOB))
    assert sites.calls == [("lichess", "alice_example"), ("lichess", "bob_example")]        # someone else's throttle is their own
    clock[0] += botmod.LOOKUP_GAP_SECONDS
    ask(make_ctx(ALICE))
    assert len(sites.calls) == 3


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


def test_something_that_is_not_a_game_is_refused_before_anything_else(dms, sites):
    register("alice_example")
    ctx = make_ctx(ALICE)
    ask(ctx, "my last game please")
    assert reactions(ctx) == [NO] and "I can't read 'my last game please' as a game" in said(ctx)[0] and sites.calls == []


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
    again = make_ctx(ALICE)
    ask(again, "00000001")                                                                  # asking again about one already waiting is fine
    assert reactions(again) == ["⏳"] and obit.waiting_count(ALICE) == 3


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


def test_the_command_has_a_usage_line_no_cooldown_of_its_own_and_help_mentions_both_forms():
    assert botmod.USAGE["obit"] == "!obit [game link or id]"
    assert botmod.obit_command._buckets._cooldown is None                                   # the throttle on looking at the sites replaces it
    ctx = make_ctx(ALICE)
    asyncio.run(botmod.help_blitz_bot.callback(ctx))
    text = said(ctx)[0]
    assert "`/obit [game link or id]`" in text and "Or `!obit` by DM" in text and "Registered members on the server only" in text and len(text) < 2000


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
    for name in ("obit_loop", "refresh_loop", "daily_posts", "health_loop"):
        loop = getattr(botmod, name)
        monkeypatch.setattr(loop, "is_running", lambda: False)
        monkeypatch.setattr(loop, "start", lambda name=name: started.append(name))
    monkeypatch.setattr(botmod, "post_signup_call_if_due", AsyncMock())
    monkeypatch.setattr(botmod, "sync_slash_commands", AsyncMock())
    asyncio.run(botmod.on_ready())
    assert "obit_loop" in started
    assert botmod.obit_loop.seconds == 60


def test_the_delivery_loop_is_not_started_twice(monkeypatch):
    started = []
    for name in ("obit_loop", "refresh_loop", "daily_posts", "health_loop"):
        monkeypatch.setattr(getattr(botmod, name), "is_running", lambda: True)
        monkeypatch.setattr(getattr(botmod, name), "start", lambda name=name: started.append(name))
    monkeypatch.setattr(botmod, "sync_slash_commands", AsyncMock())
    asyncio.run(botmod.on_ready())
    assert started == []


# --- the Delete button on the bot's DMs --------------------------------------------------------------------------------------

def test_every_dm_carries_a_delete_button(monkeypatch):
    sent = []

    class FakeUser:
        async def send(self, content, **kwargs):
            sent.append((content, kwargs))
    monkeypatch.setattr(botmod.bot, "get_user", lambda user_id: FakeUser() if user_id == ALICE else None)
    asyncio.run(botmod._dm(ALICE, ["first part", "second part"]))
    assert [c for c, _ in sent] == ["first part", "second part"]
    assert all(isinstance(kw["view"], botmod.DeleteButton) for _, kw in sent)


def test_the_delete_button_is_persistent_with_a_fixed_id():
    async def build():
        view = botmod.DeleteButton()
        return view.timeout, view.is_persistent(), [item.custom_id for item in view.children], [str(item.emoji) for item in view.children]
    timeout, persistent, ids, emoji = asyncio.run(build())
    assert timeout is None and persistent and ids == ["pmb:delete_dm"] and emoji == ["🗑️"]


def press(guild, message):
    interaction = SimpleNamespace(guild=guild, message=message, response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()))

    async def go():
        view = botmod.DeleteButton()
        await view.delete.callback(interaction)
    asyncio.run(go())
    return interaction


def test_pressing_delete_in_a_dm_deletes_that_message():
    message = SimpleNamespace(delete=AsyncMock())
    interaction = press(None, message)
    message.delete.assert_awaited_once()
    interaction.response.defer.assert_awaited_once()
    interaction.response.send_message.assert_not_awaited()


def test_the_button_never_deletes_a_message_in_a_channel():
    message = SimpleNamespace(delete=AsyncMock())
    interaction = press(SimpleNamespace(id=1), message)
    message.delete.assert_not_awaited()
    assert interaction.response.send_message.await_args.kwargs == {"ephemeral": True}


def test_a_button_with_no_message_is_refused_and_a_failed_delete_is_survived():
    interaction = press(None, None)
    interaction.response.send_message.assert_awaited_once()
    failing = SimpleNamespace(delete=AsyncMock(side_effect=discord.HTTPException(SimpleNamespace(status=404, reason="gone"), "Unknown Message")))
    press(None, failing)                                                                     # already deleted: no error escapes
    failing.delete.assert_awaited_once()


def test_the_delete_button_is_registered_when_the_bot_starts(monkeypatch):
    added = []
    for name in ("obit_loop", "refresh_loop", "daily_posts", "health_loop"):
        monkeypatch.setattr(getattr(botmod, name), "is_running", lambda: True)
    monkeypatch.setattr(botmod.bot, "add_view", lambda view: added.append(view))
    monkeypatch.setattr(botmod, "sync_slash_commands", AsyncMock())
    asyncio.run(botmod.on_ready())
    assert len(added) == 1 and isinstance(added[0], botmod.DeleteButton)


# --- /obit: the same, with nothing in the channel ------------------------------------------------------------------------------

def make_interaction(user_id=ALICE, channel_id=CHANNEL, done=False):
    state = SimpleNamespace(done=done)

    async def defer(**kwargs):
        state.done = True
    response = SimpleNamespace(defer=AsyncMock(side_effect=defer), send_message=AsyncMock(), is_done=lambda: state.done)
    return SimpleNamespace(user=SimpleNamespace(id=user_id), channel_id=channel_id, response=response, followup=SimpleNamespace(send=AsyncMock()))


def slash(interaction, *args):
    asyncio.run(botmod.obit_slash.callback(interaction, *args))


@pytest.fixture(autouse=True)
def the_blitz_channel(monkeypatch):
    monkeypatch.setattr(botmod, "ALLOWED_CHANNEL_IDS", {CHANNEL})


def test_slash_obit_sends_the_review_by_dm_and_answers_only_the_person_asking(dms, sites):
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example"))
    interaction = make_interaction()
    slash(interaction, "00000001")
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.followup.send.assert_awaited_once_with("I've sent you the review by DM.", ephemeral=True)
    interaction.response.send_message.assert_not_awaited()
    assert len(dms) == 1 and dms[0][0] == ALICE and "**OBIT**" in dms[0][1][0]


def test_slash_obit_for_a_game_still_being_analysed_says_so_privately(dms, sites):
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example")], NOW)
    interaction = make_interaction()
    slash(interaction, "00000001")
    interaction.followup.send.assert_awaited_once_with("Analysing that game now: it's at the front of the queue. I'll DM you the review when it's done.",
                                                       ephemeral=True)
    assert dms == [] and len(obit.outstanding()) == 1


def test_slash_obit_errors_are_private_too(dms, sites):
    register("alice_example")
    interaction = make_interaction()
    slash(interaction, "not a game")
    (call,) = interaction.followup.send.await_args_list
    assert "I can't read 'not a game' as a game" in call.args[0] and call.kwargs == {"ephemeral": True}
    nobody = make_interaction(BOB)
    slash(nobody)
    assert "haven't added an account" in nobody.followup.send.await_args.args[0] and nobody.followup.send.await_args.kwargs == {"ephemeral": True}


def test_slash_obit_says_privately_when_dms_are_closed(monkeypatch, sites):
    async def refuse(user_id, messages):
        raise forbidden()
    monkeypatch.setattr(botmod, "_dm", refuse)
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example"))
    interaction = make_interaction()
    slash(interaction, "00000001")
    assert "Allow direct messages" in interaction.followup.send.await_args.args[0] and interaction.followup.send.await_args.kwargs == {"ephemeral": True}


def test_slash_obit_uses_the_person_who_asked(dms, sites):
    register("alice_example")
    store.add_player("lichess", "bob_example", BOB, MONTH, 1500)
    analysed(spec(1, "alice_example", "x_example"), spec(2, "bob_example", "y_example"))
    interaction = make_interaction(BOB)
    slash(interaction, "00000001")                                                         # Alice's game
    assert dms == [] and "can't find that among your games" in interaction.followup.send.await_args.args[0]
    slash(interaction, "00000002")
    assert [u for u, _ in dms] == [BOB]


def test_slash_obit_only_works_in_the_allowed_channel_and_says_so_privately(dms, sites):
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example"))
    interaction = make_interaction(channel_id=CHANNEL + 1)
    slash(interaction, "00000001")
    interaction.response.send_message.assert_awaited_once_with("This command only works in the blitz channel.", ephemeral=True)
    interaction.response.defer.assert_not_awaited()
    assert dms == [] and sites.calls == []


def test_slash_obit_is_registered_for_servers_only_with_an_optional_game():
    command = botmod.bot.tree.get_command("obit")
    assert command is not None and command.guild_only is True
    assert [(p.name, p.required) for p in command.parameters] == [("game", False)]
    assert "DM" in command.description


def test_a_failing_slash_command_says_so_privately(caplog):
    fresh = make_interaction()
    asyncio.run(botmod.on_app_command_error(fresh, RuntimeError("boom")))
    fresh.response.send_message.assert_awaited_once_with("something went wrong, check the logs", ephemeral=True)
    started = make_interaction(done=True)
    asyncio.run(botmod.on_app_command_error(started, RuntimeError("boom")))
    started.followup.send.assert_awaited_once_with("something went wrong, check the logs", ephemeral=True)
    started.response.send_message.assert_not_awaited()
    hopeless = make_interaction()
    hopeless.response.send_message = AsyncMock(side_effect=discord.HTTPException(SimpleNamespace(status=500, reason="x"), "x"))
    asyncio.run(botmod.on_app_command_error(hopeless, RuntimeError("boom")))              # nothing escapes


# registering the slash commands

def fake_guild(guild_id):
    return SimpleNamespace(id=guild_id)


def registrations(monkeypatch, *, channels, sync=None):
    calls = SimpleNamespace(copied=[], synced=[])
    monkeypatch.setattr(botmod, "ALLOWED_CHANNEL_IDS", set(channels))
    monkeypatch.setattr(botmod.bot, "get_channel", lambda channel_id: channels.get(channel_id))
    monkeypatch.setattr(botmod.bot.tree, "copy_global_to", lambda guild: calls.copied.append(guild.id))

    async def fake_sync(guild=None):
        calls.synced.append(guild.id)
        if sync:
            sync(guild)
        return [SimpleNamespace(name="obit")]
    monkeypatch.setattr(botmod.bot.tree, "sync", fake_sync)
    return calls


def test_the_slash_commands_are_registered_in_the_server_of_each_allowed_channel_once(monkeypatch):
    channels = {CHANNEL: SimpleNamespace(guild=fake_guild(1)), CHANNEL + 1: SimpleNamespace(guild=fake_guild(1)), CHANNEL + 2: SimpleNamespace(guild=fake_guild(2))}
    calls = registrations(monkeypatch, channels=channels)
    asyncio.run(botmod.sync_slash_commands())
    assert sorted(calls.copied) == [1, 2] and sorted(calls.synced) == [1, 2]              # each server once, and the commands are copied in first
    asyncio.run(botmod.sync_slash_commands())
    assert sorted(calls.synced) == [1, 2]                                                 # a reconnect doesn't repeat it


def test_a_channel_the_bot_cannot_see_is_skipped(monkeypatch):
    calls = registrations(monkeypatch, channels={CHANNEL: None})
    monkeypatch.setattr(botmod, "ALLOWED_CHANNEL_IDS", {CHANNEL, 999})
    asyncio.run(botmod.sync_slash_commands())
    assert calls.synced == []


def test_a_bot_invited_without_the_scope_logs_how_to_fix_it_and_carries_on(monkeypatch, caplog):
    def refuse(guild):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Missing Access"), "Missing Access")
    calls = registrations(monkeypatch, channels={CHANNEL: SimpleNamespace(guild=fake_guild(1)), CHANNEL + 1: SimpleNamespace(guild=fake_guild(2))}, sync=refuse)
    with caplog.at_level("WARNING", logger="playmoreblitz"):
        asyncio.run(botmod.sync_slash_commands())
    assert calls.synced == [1, 2] and caplog.text.count("applications.commands") == 2      # each server tried, each told about


def test_another_failure_is_logged_and_does_not_stop_the_bot(monkeypatch, caplog):
    def broken(guild):
        raise discord.HTTPException(SimpleNamespace(status=500, reason="oops"), "server error")
    registrations(monkeypatch, channels={CHANNEL: SimpleNamespace(guild=fake_guild(1))}, sync=broken)
    asyncio.run(botmod.sync_slash_commands())
    assert "couldn't register slash commands in 1" in caplog.text


def test_the_bot_registers_its_slash_commands_when_it_starts(monkeypatch):
    registered = AsyncMock()
    monkeypatch.setattr(botmod, "sync_slash_commands", registered)
    for name in ("obit_loop", "refresh_loop", "daily_posts", "health_loop"):
        monkeypatch.setattr(getattr(botmod, name), "is_running", lambda: True)
    monkeypatch.setattr(botmod.bot, "add_view", lambda view: None)
    asyncio.run(botmod.on_ready())
    registered.assert_awaited_once()


def test_the_readme_explains_the_extra_invite_scope():
    text = open(botmod.__file__.replace("bot.py", "README.md"), encoding="utf-8").read()
    assert "applications.commands" in text and "`/obit [game]`" in text


# --- gaps found by mutation checks -------------------------------------------------------------------------------------------

def test_a_named_game_not_seen_yet_is_found_after_one_look_at_the_sites(dms, sites):
    register("alice_example")
    sites.effect = lambda: q.queue_games([spec(7, "alice_example", "x_example")], NOW) if "00000007" not in statuses() else None
    ctx = make_ctx(ALICE)
    ask(ctx, "https://lichess.org/00000007")
    assert sites.calls == [("lichess", "alice_example")] and reactions(ctx) == ["⏳"] and obit.outstanding()[0]["game_id"] == "00000007"


def test_a_failure_to_post_the_error_text_is_survived():
    ctx = make_ctx(ALICE)
    ctx.send = AsyncMock(side_effect=discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions"))
    ask(ctx)                                                                                # no account: an error to post
    assert reactions(ctx) == [NO]


def test_the_typing_indicator_shows_while_the_sites_are_looked_at(dms, sites):
    register("alice_example")
    entered = []

    class Counting:
        async def __aenter__(self):
            entered.append(1)

        async def __aexit__(self, *exc):
            return False
    ctx = make_ctx(ALICE)
    ctx.typing = lambda: Counting()
    ask(ctx)
    assert entered == [1] and len(sites.calls) == 1


def test_a_queued_slash_request_remembers_the_channel_it_came_from(dms, sites):
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example")], NOW)
    slash(make_interaction(), "00000001")
    assert obit.outstanding()[0]["channel_id"] == CHANNEL


# --- !obit only in a direct message, only for registered members who are on the server -----------------------------------------------

def in_the_channel(ctx):
    ctx.guild = SimpleNamespace(id=1)
    ctx.channel = SimpleNamespace(id=CHANNEL)
    return ctx


def test_in_the_channel_obit_only_points_to_slash_and_dms_and_the_hint_removes_itself(dms, sites):
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example"))
    ctx = in_the_channel(make_ctx(ALICE))
    ask(ctx, "00000001")
    ctx.send.assert_awaited_once_with(botmod.OBIT_HINT, delete_after=20)
    assert "/obit" in botmod.OBIT_HINT and "direct message" in botmod.OBIT_HINT
    assert dms == [] and sites.calls == [] and obit.outstanding() == [] and reactions(ctx) == []          # nothing was looked at, sent or queued


def test_a_failure_to_post_the_hint_is_survived(dms, sites):
    ctx = in_the_channel(make_ctx(ALICE))
    ctx.send = AsyncMock(side_effect=discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions"))
    ask(ctx)
    assert dms == []


def test_the_channel_check_lets_a_direct_message_through_for_obit_only():
    async def allowed(guild, channel_id, command):
        ctx = SimpleNamespace(guild=guild, channel=SimpleNamespace(id=channel_id), author=SimpleNamespace(id=ALICE),
                              command=SimpleNamespace(name=command) if command else None)
        return await botmod._in_allowed_channel(ctx)
    assert asyncio.run(allowed(None, DM_CHANNEL, "obit")) is True
    assert asyncio.run(allowed(None, DM_CHANNEL, "results")) is False and asyncio.run(allowed(None, DM_CHANNEL, "add")) is False
    assert asyncio.run(allowed(None, DM_CHANNEL, "mystatsfull")) is False and asyncio.run(allowed(None, DM_CHANNEL, None)) is False
    assert asyncio.run(allowed(SimpleNamespace(id=1), CHANNEL, "results")) is True                       # a server channel: as before
    assert asyncio.run(allowed(SimpleNamespace(id=1), CHANNEL + 5, "obit")) is False


def test_a_dm_from_someone_who_is_not_on_the_server_is_refused_and_nothing_happens(dms, sites, on_the_server):
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example"))
    on_the_server.guild = the_server(BOB)                                                    # Alice has left
    ctx = make_ctx(ALICE)
    ask(ctx, "00000001")
    assert reactions(ctx) == [NO] and said(ctx) == ["This is only for members of the server."]
    assert dms == [] and sites.calls == [] and obit.outstanding() == []


def test_a_dm_is_refused_when_membership_cannot_be_confirmed(dms, sites, on_the_server):
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example"))
    on_the_server.guild = the_server(error=discord.HTTPException(SimpleNamespace(status=500, reason="oops"), "server error"))
    ctx = make_ctx(ALICE)
    ask(ctx, "00000001")
    assert reactions(ctx) == [NO] and said(ctx) == ["I couldn't check that you're on the server just now: try again in a moment."]
    assert dms == [] and obit.outstanding() == []


def test_a_dm_is_refused_when_the_bot_cannot_see_the_server_at_all(dms, sites, monkeypatch):
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example"))
    monkeypatch.setattr(botmod.bot, "get_channel", lambda channel_id: None)
    ctx = make_ctx(ALICE)
    ask(ctx, "00000001")
    assert reactions(ctx) == [NO] and "couldn't check" in said(ctx)[0] and dms == []


def test_membership_of_any_server_the_bot_serves_is_enough(monkeypatch):
    first, second = the_server(guild_id=1), the_server(ALICE, guild_id=2)
    channels = {CHANNEL: SimpleNamespace(guild=first), CHANNEL + 1: SimpleNamespace(guild=second)}
    monkeypatch.setattr(botmod, "ALLOWED_CHANNEL_IDS", set(channels))
    monkeypatch.setattr(botmod.bot, "get_channel", lambda channel_id: channels.get(channel_id))
    assert asyncio.run(botmod._member_status(ALICE)) is True and asyncio.run(botmod._member_status(BOB)) is False


def test_a_failed_lookup_in_one_server_does_not_hide_membership_of_another_but_does_hide_absence(monkeypatch):
    broken = the_server(error=discord.HTTPException(SimpleNamespace(status=500, reason="oops"), "x"), guild_id=1)
    member_of = the_server(ALICE, guild_id=2)
    channels = {CHANNEL: SimpleNamespace(guild=broken), CHANNEL + 1: SimpleNamespace(guild=member_of)}
    monkeypatch.setattr(botmod, "ALLOWED_CHANNEL_IDS", set(channels))
    monkeypatch.setattr(botmod.bot, "get_channel", lambda channel_id: channels.get(channel_id))
    assert asyncio.run(botmod._member_status(ALICE)) is True
    assert asyncio.run(botmod._member_status(BOB)) is None                                   # can't tell: not "absent"


def test_a_dm_needs_a_registered_account_even_for_a_member(dms, sites):
    ctx = make_ctx(ALICE)                                                                   # on the server, not registered
    ask(ctx)
    assert reactions(ctx) == [NO] and "haven't added an account" in said(ctx)[0] and "in the server's channel" in said(ctx)[0]
    assert dms == [] and sites.calls == []


def test_a_dm_gets_its_review_in_the_same_conversation_with_a_tick(dms, sites):
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example"))
    ctx = make_ctx(ALICE)
    ask(ctx, "00000001")
    assert reactions(ctx) == [OK] and said(ctx) == [] and [u for u, _ in dms] == [ALICE]


def test_a_review_waiting_for_someone_who_asked_by_dm_is_dropped_once_they_leave_the_server(dms, sites, on_the_server):
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example")], NOW)
    ask(make_ctx(ALICE), "00000001")                                                        # queued from a DM (channel id is the DM's)
    assert obit.outstanding()[0]["channel_id"] == DM_CHANNEL
    analysed_now(1)
    on_the_server.guild = the_server(BOB)                                                    # Alice leaves while it waits
    asyncio.run(botmod.obit_loop.coro())
    assert dms == [] and obit.outstanding() == []


def test_a_review_waiting_for_someone_who_asked_by_dm_is_sent_while_they_are_still_members(dms, sites):
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example")], NOW)
    ask(make_ctx(ALICE), "00000001")
    analysed_now(1)
    asyncio.run(botmod.obit_loop.coro())
    assert [u for u, _ in dms] == [ALICE] and obit.outstanding() == []


def analysed_now(n):
    """Give the queued game n its analysis result."""
    q.claim("desk", 10, NOW)
    q.submit("desk", [{"site": "lichess", "game_id": f"{n:08d}", "method_version": analysis.METHOD_VERSION, "engine": "Stockfish 19", "nodes": 200_000,
                       "plies": 40, "middle_ply": 14, "end_ply": None, "eval_ply20": 10, "evals": curve(0), "white": side(), "black": side(),
                       "moments": moments_for(side(), side())}], NOW + 5)


# --- the channel restriction is really registered, and works as Discord runs it ---------------------------------------------------

def passes_the_global_checks(guild, channel_id, command):
    """Whether a message would get past every check registered on the bot, run the way discord.py runs them."""
    ctx = SimpleNamespace(guild=guild, channel=SimpleNamespace(id=channel_id), author=SimpleNamespace(id=ALICE),
                          command=SimpleNamespace(name=command) if command else None)

    async def run_all():
        return all([await discord.utils.maybe_coroutine(check, ctx) for check in botmod.bot._checks])
    return asyncio.run(run_all())


def test_the_channel_restriction_is_the_bots_registered_check():
    assert botmod._in_allowed_channel in botmod.bot._checks and botmod._in_dm not in botmod.bot._checks


def test_commands_in_the_allowed_channel_pass_the_checks_and_other_channels_do_not():
    server = SimpleNamespace(id=1)
    assert passes_the_global_checks(server, CHANNEL, "results") is True
    assert passes_the_global_checks(server, CHANNEL, "obit") is True
    assert passes_the_global_checks(server, CHANNEL + 1, "results") is False
    assert passes_the_global_checks(server, CHANNEL + 1, "obit") is False


def test_a_direct_message_passes_the_checks_for_obit_only():
    assert passes_the_global_checks(None, DM_CHANNEL, "obit") is True
    for command in ("results", "add", "mystats", "mystatsfull", "helpblitzbot", "remove", None):
        assert passes_the_global_checks(None, DM_CHANNEL, command) is False


# --- the turning point, and a chance given back -------------------------------------------------------------------------------------------------

def notes_of(row, side_="white", username="alice_example"):
    text = "\n".join(render_obit.render_obit(username, "lichess", row, side_))
    return [line[2:] for line in section(text, "I").split("\n") if line.startswith("• ")]


def test_the_biggest_swing_of_the_game_is_named_when_it_was_your_own_move():
    evals = curve(*([30] * 10), -120, *([-120] * 10))                                            # ply 11 (yours, White's): +0.3 falls to -1.2
    row = with_moments(row_of(1, evals=evals), [[11, "b", 22.0], [14, "m", 12.0]])
    notes = notes_of(row)
    assert "The biggest swing of the game was your own 6.: it cost you 22% (+0.3 → -1.2)." in notes


def test_when_the_biggest_swing_was_the_opponents_it_is_told_as_their_chance_with_the_score_and_not_as_your_own():
    evals = curve(*([0] * 13), 150, *([150] * 10))                                               # ply 14 is Black's: 0 becomes +1.5 for White
    row = with_moments(row_of(1, evals=evals), [[14, "b", 25.0], [11, "m", 12.0]])
    notes = notes_of(row)
    assert "Your opponent made 1 mistake or blunder; the biggest, 7..., cost them 25% (+0.0 → +1.5). Did you see it, and use it?" in notes
    assert not any("biggest swing of the game" in n for n in notes)


def test_the_swing_is_from_the_players_side_for_black():
    evals = curve(*([0] * 13), 150, *([150] * 10))                                               # White's ply 13.. no: ply 14 is Black's
    row = with_moments(row_of(1, evals=evals), [[14, "b", 25.0]])
    notes = notes_of(row, "black", "rival_example")
    assert "The biggest swing of the game was your own 7...: it cost you 25% (+0.0 → -1.5)." in notes


def test_a_swing_needs_ten_points_and_the_earliest_of_two_equal_ones_counts():
    small = with_moments(row_of(1, evals=curve(0)), [[11, "i", 9.9]])
    assert not any("biggest swing" in n for n in notes_of(small))
    exactly = with_moments(row_of(2, evals=curve(0)), [[11, "m", 10.0]])
    assert any("biggest swing of the game was your own 6." in n for n in notes_of(exactly))
    tied = with_moments(row_of(3, evals=curve(0)), [[15, "b", 20.0], [11, "b", 20.0]])
    assert any("your own 6.: it cost you 20%" in n for n in notes_of(tied))
    tied_with_theirs = with_moments(row_of(4, evals=curve(0)), [[12, "b", 20.0], [15, "b", 20.0]])                 # theirs (ply 12) came first
    assert not any("biggest swing" in n for n in notes_of(tied_with_theirs))


def test_a_swing_without_scores_is_still_named_just_without_them():
    row = with_moments(row_of(1), [[11, "b", 22.0]])
    row["evals"] = None
    assert "The biggest swing of the game was your own 6.: it cost you 22%." in notes_of(row)


def test_a_chance_given_back_names_the_pair():
    row = with_moments(row_of(1, evals=curve(0)), [[14, "b", 25.0], [15, "m", 12.0]])
    assert "Your opponent's blunder on 7... (−25%) was answered by your own mistake on 8. (−12%): the chance was given back straight away." in notes_of(row)


def test_a_chance_given_back_counts_and_shows_the_costliest_reply():
    row = with_moments(row_of(1, evals=curve(0)), [[8, "m", 11.0], [9, "i", 6.0], [14, "b", 25.0], [15, "b", 20.0], [22, "m", 12.0], [23, "i", 5.0]])
    (note,) = [n for n in notes_of(row) if "answered by your own" in n]
    assert note == ("Your opponent's blunder on 7... (−25%) was answered by your own blunder on 8. (−20%): the chance was given back straight away."
                    " It happened 3 times.")


def test_a_chance_is_not_given_back_when_the_reply_was_fine_or_late_or_the_gift_only_an_inaccuracy():
    fine = with_moments(row_of(1, evals=curve(0)), [[14, "b", 25.0]])
    late = with_moments(row_of(2, evals=curve(0)), [[14, "b", 25.0], [17, "m", 12.0]])
    small = with_moments(row_of(3, evals=curve(0)), [[14, "i", 8.0], [15, "m", 12.0]])
    for row in (fine, late, small):
        assert not any("answered by your own" in n for n in notes_of(row))


def test_a_chance_given_back_is_told_from_blacks_side_too():
    row = with_moments(row_of(1, evals=curve(0)), [[13, "b", 25.0], [14, "m", 12.0]])                     # White's blunder, Black's reply
    assert "Your opponent's blunder on 7. (−25%) was answered by your own mistake on 7... (−12%): the chance was given back straight away." in notes_of(row, "black", "rival_example")


def test_none_of_the_moves_of_a_quiet_game_makes_a_note():
    quiet = side(inaccuracies=0, mistakes=0, blunders=0)
    text = review(row_of(1, white=quiet, black=quiet, evals=curve(0)))
    assert "Nothing unusual: a steady game." in text


def test_a_swing_on_the_very_last_move_still_shows_the_scores():
    none = side(inaccuracies=0, mistakes=0, blunders=0)
    row = with_moments(row_of(1, plies=21, evals=curve(0, plies=21), white=none, black=none), [[21, "b", 22.0]])          # ply 21 is the last stored score
    assert "The biggest swing of the game was your own 11.: it cost you 22% (+0.0 → +0.0)." in notes_of(row)


def test_of_two_chances_given_back_equally_the_earlier_one_is_named():
    row = with_moments(row_of(1, evals=curve(0)), [[8, "b", 20.0], [9, "i", 8.0], [14, "b", 25.0], [15, "i", 8.0]])
    (note,) = [n for n in notes_of(row) if "answered by your own" in n]
    assert note.startswith("Your opponent's blunder on 4... (−20%) was answered by your own inaccuracy on 5. (−8%)") and note.endswith("It happened 2 times.")


# --- the clocks: the Time section and the two notes -----------------------------------------------------------------------------------------------

def clocks_blob(white, black, base=300, inc=0, plies=40):
    """The packed clocks (analysis.pack_clocks) of a game where White's moves took `white` seconds and Black's `black`, padded to `plies` plies."""
    clocks, w, b = [], float(base), float(base)
    for i in range(plies // 2):
        w = w - (white[i] if i < len(white) else 1) + inc
        b = b - (black[i] if i < len(black) else 1) + inc
        clocks += [w, b]
    return analysis.pack_clocks(clocks)


def clock_row(white=(3,) * 20, black=(3,) * 20, time_control="300+0", figures=None, **over):
    """An analysed game with clocks. `white` and `black` are how long each side's moves took; `figures` is (white side, black side) of
    analysis figures if the test wants its own."""
    blob = clocks_blob(list(white), list(black), *[int(x) for x in time_control.split("+")]) if "/" not in time_control else clocks_blob(list(white), list(black))
    if figures:
        over["white"], over["black"] = figures
    return row_of(1, time_control=time_control, clocks=blob, **over)


def test_a_game_with_clocks_gets_a_time_section_between_blunders_and_interesting():
    text = review(clock_row())
    assert text.index("**B ·") < text.index("**Time**") < text.index("**I ·")
    assert "**Time** — 5+0, from the clocks after every move" in text
    lines = section_between(text, "**Time**", "**I ·")
    assert "✓ Opening (your first 10 moves): 30s = 10% of base time — nicely quick" in lines
    assert any(l.startswith("✓ You never hit serious time trouble") for l in lines)


def section_between(text, start, end):
    return text[text.index(start): text.index(end)].split("\n")


def test_the_time_section_lists_every_check_with_its_icon():
    lines = section_between(review(clock_row()), "**Time**", "**I ·")
    icons = [l[0] for l in lines[1:] if l]
    assert set(icons) <= {"✓", "⚠", "•"} and len(icons) >= 6


def test_a_game_without_clocks_has_no_time_section_and_no_clock_notes():
    text = review(row_of(1))                                                      # analysed with no clocks, as before the clocks were kept
    assert "**Time**" not in text and "longest think" not in text


def test_a_daily_game_has_no_time_section_even_if_it_has_a_blob():
    text = review(clock_row(time_control="1/86400"))
    assert "**Time**" not in text


def test_a_damaged_clock_blob_is_left_out_not_fatal():
    row = clock_row()
    row["clocks"] = b"\x01"
    text = review(row)
    assert "**Time**" not in text and "**I ·" in text


def test_the_time_section_is_from_the_players_side():
    white = review(clock_row(white=(3,) * 20, black=(20,) * 20))
    black = review(clock_row(white=(3,) * 20, black=(20,) * 20), "black", username="rival_example")
    assert "Opening (your first 10 moves): 30s" in white and "Opening (your first 10 moves): 3m 20s" in black


def test_a_long_think_is_named_under_interesting_with_the_engines_verdict():
    row = clock_row(white=[3] * 5 + [45] + [3] * 14)                              # move 6 (ply 11) took 45 s
    with_slip = with_moments(row, [[11, "b", 22.0]])
    notes = notes_of(with_slip)
    assert "Your longest think was 6. (45s, 15% of your base time): it ended in a blunder (−22%). What were you stuck on?" in notes
    held = with_moments(row, [])
    assert "Your longest think was 6. (45s, 15% of your base time): the move held up, so the time was well spent. What made the position hard?" in notes_of(held)


def test_fast_slips_are_named_under_interesting():
    row = clock_row(white=[3] * 20)
    slipped = with_moments(row, [[11, "m", 12.0], [13, "b", 20.0]])
    assert any(n.startswith("You played 2 of your 2 mistakes and blunders in under 5 seconds") for n in notes_of(slipped))


def test_the_clock_notes_come_after_the_engine_ones():
    row = with_moments(clock_row(white=[3] * 5 + [45] + [3] * 14), [[12, "b", 25.0], [11, "b", 22.0]])          # their blunder (ply 12), then yours (ply 11)
    notes = notes_of(row)
    engine = next(i for i, n in enumerate(notes) if n.startswith("Your opponent made"))
    clock = next(i for i, n in enumerate(notes) if n.startswith("Your longest think"))
    assert engine < clock


def test_a_timed_game_with_nothing_to_note_is_still_a_steady_game():
    quiet = side(inaccuracies=0, mistakes=0, blunders=0)
    row = clock_row(figures=(quiet, quiet), evals=curve(0))
    assert "Nothing unusual: a steady game." in review(row) and "**Time**" in review(row)


def test_the_review_still_fits_in_discords_messages_with_the_time_section():
    messages = render_obit.render_obit("alice_example", "lichess", clock_row(), "white")
    assert all(len(m) <= 2000 for m in messages)


def test_a_game_won_on_time_says_so_and_never_says_you_finished_behind():
    text = review(clock_row(white=(3,) * 20, black=(1,) * 20, ending="timeout", result="white"))                 # White (the player) slower on the clock, then wins on time
    assert "You won on time: your opponent's clock ran out, although at the last readings you were" in text and "finished behind" not in text


def test_a_game_lost_on_time_says_your_clock_ran_out():
    text = review(clock_row(ending="timeout", result="black"))
    assert "You lost on time: your clock ran out" in text and "You won on time" not in text


def test_the_time_result_is_told_from_blacks_side():
    text = review(clock_row(white=(1,) * 20, black=(3,) * 20, ending="timeout", result="black"), "black", username="rival_example")
    assert "You won on time: your opponent's clock ran out, although at the last readings you were" in text


def test_a_draw_that_ended_on_time_is_compared_as_usual():
    text = review(clock_row(ending="timeout", result="draw"))
    time_section = text[text.index("**Time**"):text.index("**I ·")]
    assert "You won on time" not in time_section and "You lost on time" not in time_section and "The clocks finished level" in time_section


def test_the_time_section_judges_the_final_lead_of_a_game_that_did_not_end_on_time():
    resigned = review(clock_row(ending="resigned", result="white"))
    assert "The game ended on time" not in resigned and "The clocks finished level" in resigned

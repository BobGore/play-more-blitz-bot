"""render_analysis.py: the game panel, the analysis part of !mystats and the admin's queue picture."""

import pytest

import analysis_reports as reports
import render
import render_analysis as ra

WHEN = 1_789_470_600  # 15 Sep 2026, 11:50 UTC


def row(**over):
    base = {
        "side": "white", "site": "lichess", "game_id": "abcd1234", "ended_at": WHEN, "time_control": "300+5", "result": "white",
        "opening_site": "Sicilian-Defense-Najdorf-Variation", "eco_site": "B90", "engine": "Stockfish 19", "nodes": 200_000,
        "white_username": "alice_example", "black_username": "rival_example",
        "white_rating_change": 8, "black_rating_change": None,
        "white_inaccuracies": 5, "black_inaccuracies": 7, "white_mistakes": 2, "black_mistakes": 3, "white_blunders": 1, "black_blunders": 0,
        "white_acpl": 47, "black_acpl": 90,
        "white_accuracy": 88.4, "black_accuracy": 61.5, "white_acc_opening": 95.2, "black_acc_opening": 70.0,
        "white_acc_middle": 80.5, "black_acc_middle": 55.4, "white_acc_end": None, "black_acc_end": 49.6,
        "site_white_accuracy": None, "site_black_accuracy": None,
    }
    base.update(over)
    return base


def panel(text):
    return text.split("```")[1].strip("\n").split("\n")


# --- small pieces -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, label", [("300+5", "5+5"), ("180+0", "3+0"), ("90+1", "1.5+1"), ("600+0", "10+0"), ("60+2", "1+2"), ("weird", "weird"), ("a+b", "a+b"), (None, ""), ("", "")])
def test_a_time_control_reads_in_minutes(raw, label):
    assert ra.time_control_label(raw) == label


@pytest.mark.parametrize("seconds, words", [(0, "under a minute"), (59, "under a minute"), (60, "1 min"), (840, "14 min"), (7800, "2 h 10 min"),
                                            (7200, "2 h"), (200_000, "2 days")])
def test_a_duration_is_in_words(seconds, words):
    assert ra.duration(seconds) == words


# --- the panel for one game -----------------------------------------------------------------------------------------------

def test_the_panel_shows_both_sides_with_aligned_columns_and_the_figures_rounded():
    text = ra.render_lastgame("alice_example", "lichess", row())
    lines = panel(text)
    assert lines[0].split() == ["alice_example", "(W)", "rival_example", "(B)"]
    body = {line[:18].strip(): line[18:].split() for line in lines[1:]}
    assert body["Result"] == ["won", "lost"] and body["Rating change"] == ["+8", "-"]
    assert body["Inaccuracies"] == ["5", "7"] and body["Mistakes"] == ["2", "3"] and body["Blunders"] == ["1", "0"]
    assert body["Avg centipawn loss"] == ["47", "90"]
    assert body["Accuracy"] == ["88%", "62%"] and body["Opening"] == ["95%", "70%"] and body["Middlegame"] == ["81%", "55%"] and body["Endgame"] == ["-", "50%"]
    first, second = lines[0].index("alice_example"), lines[0].index("rival_example")
    assert all(line[first - 1] == " " and line[first] != " " for line in lines[1:])      # the first value column lines up under its heading
    assert all(line[second - 1] == " " and line[second] != " " for line in lines[1:])    # and so does the second


def test_the_headline_names_the_player_site_date_time_control_and_opening_and_the_link_is_not_embedded():
    lines = ra.render_lastgame("alice_example", "lichess", row()).split("\n")
    assert lines[0] == "`alice_example` · Lichess · 15 Sep 2026 · 5+5 · Sicilian Defense (B90)"
    assert lines[1] == "<https://lichess.org/abcd1234>"


def test_a_chesscom_link_and_an_opening_with_no_eco():
    text = ra.render_lastgame("carol_example", "chess.com", row(site="chess.com", game_id="live/987654321", eco_site=None, opening_site="Caro-Kann-Defense"))
    assert "<https://www.chess.com/game/live/987654321>" in text and "Caro-Kann Defense" in text and "()" not in text


def test_a_game_with_no_opening_has_a_shorter_headline():
    assert ra.render_lastgame("alice_example", "lichess", row(opening_site=None, eco_site=None)).split("\n")[0] == "`alice_example` · Lichess · 15 Sep 2026 · 5+5"


def test_seen_from_black_the_player_is_first_and_the_result_is_theirs():
    text = ra.render_lastgame("rival_example", "lichess", row(side="black", result="white"))
    lines = panel(text)
    assert lines[0].split() == ["rival_example", "(B)", "alice_example", "(W)"]
    assert lines[1].split()[1:] == ["lost", "won"]
    assert lines[6].split()[-2:] == ["90", "47"]           # Avg centipawn loss: theirs first now
    assert lines[2].split()[2:] == ["-", "+8"]              # the rating change belongs to White here


@pytest.mark.parametrize("result, side, mine, theirs", [("draw", "white", "drew", "drew"), ("black", "black", "won", "lost"), ("black", "white", "lost", "won")])
def test_result_words(result, side, mine, theirs):
    assert panel(ra.render_lastgame("x", "lichess", row(result=result, side=side)))[1].split()[1:] == [mine, theirs]


def test_a_negative_rating_change_keeps_its_sign():
    assert "-12" in panel(ra.render_lastgame("alice_example", "lichess", row(white_rating_change=-12)))[2]


def test_the_footer_says_the_figures_are_the_bots_own_estimate():
    assert ra.render_lastgame("alice_example", "lichess", row()).splitlines()[-1] == \
        "Analysed by Stockfish 19 at 200,000 nodes a position: the bot's own estimate, not the site's."


def test_the_sites_own_accuracy_is_shown_beside_ours_and_chesscom_says_it_differs():
    lichess = ra.render_lastgame("alice_example", "lichess", row(site_white_accuracy=90.0, site_black_accuracy=None))
    assert "Lichess's own accuracy: 90% / -" in lichess and "different method" not in lichess
    chesscom = ra.render_lastgame("alice_example", "chess.com", row(site_white_accuracy=76.4, site_black_accuracy=84.6))
    assert "Chess.com's own accuracy: 76% / 85% (a different method, so the two won't match)" in chesscom
    assert "own accuracy" not in ra.render_lastgame("alice_example", "lichess", row())


def test_games_still_waiting_are_mentioned_with_the_right_grammar():
    assert ra.render_lastgame("alice_example", "lichess", row(), waiting=1).splitlines()[-1] == "1 more of alice_example's games is waiting to be analysed."
    assert ra.render_lastgame("alice_example", "lichess", row(), waiting=4).splitlines()[-1] == "4 more of alice_example's games are waiting to be analysed."
    assert "waiting" not in ra.render_lastgame("alice_example", "lichess", row(), waiting=0)


def test_a_very_long_name_is_cut_and_the_whole_message_stays_within_discords_limit():
    long = "a" * 60
    text = ra.render_lastgame(long, "lichess", row(white_username=long, black_username="b" * 60), waiting=3)
    assert len(text) < render.MAX_MESSAGE and "…" in panel(text)[0]


def test_missing_figures_show_a_dash_not_a_crash():
    empty = row(white_accuracy=None, black_accuracy=None, white_acpl=None, black_acpl=None, white_acc_opening=None, black_acc_opening=None,
                white_acc_middle=None, black_acc_middle=None, white_acc_end=None, black_acc_end=None)
    lines = panel(ra.render_lastgame("alice_example", "lichess", empty))
    assert lines[6].split()[-2:] == ["-", "-"] and lines[7].split()[-2:] == ["-", "-"]


# --- the analysis part of !mystats ------------------------------------------------------------------------------------------------

def summary(**over):
    base = dict(total=61, analysed=42, waiting=12, over_limit=2, skipped=5, failed=0, accuracy=71.4, opening=88.2, middlegame=67.9, endgame=74.4,
                acpl=63.2, inaccuracies=4.13, mistakes=1.3, blunders=0.62)
    base.update(over)
    return reports.MonthSummary(**base)


def test_the_block_says_how_many_were_analysed_and_what_became_of_the_rest():
    lines = ra.analysis_block(summary()).split("\n")
    assert lines[0] == "Analysed 42 of 61 games (12 waiting, 5 skipped, 2 over the monthly limit)"
    assert lines[1] == "Accuracy 71%   Opening 88%   Middlegame 68%   Endgame 74%   Avg centipawn loss 63"
    assert lines[2] == "Per game: 4.1 inaccuracies   1.3 mistakes   0.6 blunders"


def test_a_month_where_everything_was_analysed_has_no_brackets():
    assert ra.analysis_block(summary(total=42, waiting=0, over_limit=0, skipped=0)).split("\n")[0] == "Analysed 42 of 42 games"


def test_failed_games_are_mentioned():
    assert "1 failed" in ra.analysis_block(summary(failed=1)).split("\n")[0]


def test_nothing_analysed_yet_gives_just_the_headline():
    lines = ra.analysis_block(summary(analysed=0, accuracy=None, opening=None, middlegame=None, endgame=None, acpl=None,
                                      inaccuracies=None, mistakes=None, blunders=None)).split("\n")
    assert lines == ["Analysed 0 of 61 games (12 waiting, 5 skipped, 2 over the monthly limit)"]


def test_a_missing_phase_or_loss_is_a_dash():
    assert "Endgame -" in ra.analysis_block(summary(endgame=None)) and "Avg centipawn loss -" in ra.analysis_block(summary(acpl=None))


def test_the_mystats_part_is_absent_without_games_and_titled_as_the_bots_own_estimate_with_them():
    assert ra.mystats_part(None) is None and ra.mystats_part(summary(total=0, analysed=0)) is None
    part = ra.mystats_part(summary())
    assert part.startswith("**Analysis** (the bot's own, by Stockfish)\n```\n") and part.endswith("\n```")


# --- the queue picture -------------------------------------------------------------------------------------------------------------

def status(**over):
    base = {"counts": {"pending": 24, "claimed": 3, "done": 1532, "skipped": 41, "failed": 2}, "low_priority_pending": 3, "over_limit": 37,
            "oldest_pending_seconds": 840, "workers": [("desk", 130)]}
    base.update(over)
    return base


def test_the_queue_picture_lists_every_count_the_oldest_wait_and_the_worker():
    text = ra.render_queue_status(status(), True)
    lines = text.split("```")[1].strip("\n").split("\n")
    assert lines[0].split() == ["Waiting", "24", "(3", "low", "priority)"]
    assert lines[1].split() == ["Being", "analysed", "3"]
    assert lines[2].split() == ["Done", "1,532"]
    assert lines[3].split() == ["Skipped", "41", "(37", "over", "the", "monthly", "limit)"]
    assert lines[4].split() == ["Failed", "2"]
    ends = {line.index(number) + len(number) for line, number in zip(lines[:5], ("24", "3", "1,532", "41", "2"))}
    assert len(ends) == 1                                   # the numbers line up on their right-hand edge
    assert lines[5] == "Oldest waiting game: 14 min" and lines[6] == "Worker desk: last asked for work 2 min ago"
    assert text.startswith("**Analysis queue**") and "⚠" not in text and "switched off" not in text


def test_a_quiet_queue_has_no_oldest_line_and_no_brackets():
    text = ra.render_queue_status(status(counts={"pending": 0, "claimed": 0, "done": 5, "skipped": 0, "failed": 0}, low_priority_pending=0, over_limit=0,
                                         oldest_pending_seconds=None), True)
    assert "Oldest" not in text and "low priority" not in text and "over the monthly" not in text


def test_a_switched_off_analysis_says_so():
    assert "Analysis is switched off (`ANALYSIS_ENABLED`), so no new games are being queued." in ra.render_queue_status(status(), False)


def test_games_waiting_with_no_worker_ever_is_a_warning():
    assert "⚠ Games are waiting but no worker has ever asked for work." in ra.render_queue_status(status(workers=[]), True)


def test_a_worker_that_has_gone_quiet_is_a_warning_only_when_there_is_work_and_after_fifteen_minutes():
    assert "⚠ The worker last asked for work 2 h ago." in ra.render_queue_status(status(workers=[("desk", 7200)]), True)
    assert "⚠" not in ra.render_queue_status(status(workers=[("desk", 900)]), True)
    assert "⚠" in ra.render_queue_status(status(workers=[("desk", 901)]), True)
    idle = status(counts={"pending": 0, "claimed": 0, "done": 5, "skipped": 0, "failed": 0}, workers=[("desk", 7200)])
    assert "⚠" not in ra.render_queue_status(idle, True)


def test_work_with_the_worker_counts_as_work_for_the_warning():
    busy = status(counts={"pending": 0, "claimed": 4, "done": 5, "skipped": 0, "failed": 0}, workers=[])
    assert "no worker has ever asked" in ra.render_queue_status(busy, True)


def test_a_day_before_the_tenth_has_no_leading_zero():
    assert ra.render_lastgame("alice_example", "lichess", row(ended_at=1_788_600_000)).split("\n")[0].split(" · ")[2] == "5 Sep 2026"


def test_no_panel_line_ends_in_spaces():
    lines = panel(ra.render_lastgame("alice_example", "lichess", row()))
    assert all(line == line.rstrip() for line in lines)

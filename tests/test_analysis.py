"""analysis.py: the accuracy, judgement and summary maths, with small games worked out by hand."""

import pytest

import analysis


def cp(*values):
    return [("cp", v) for v in values]


# --- win percent and winning chances ------------------------------------------------------------------------

def test_an_even_position_is_fifty_percent_and_the_scale_is_symmetric():
    assert analysis.win_percent(0) == 50
    assert analysis.win_percent(300) + analysis.win_percent(-300) == pytest.approx(100)


def test_accuracy_caps_the_evaluation_but_judgement_does_not():
    assert analysis.win_percent(5000) == analysis.win_percent(1000) == analysis.win_percent(1500)
    assert analysis.winning_chances(5000) > analysis.winning_chances(1000)


def test_a_forced_mate_counts_as_the_cap_for_accuracy():
    assert analysis.as_cp(("mate", 3)) == 1000 and analysis.as_cp(("mate", -1)) == -1000
    assert analysis.as_cp(("cp", 4321)) == 1000 and analysis.as_cp(("cp", -250)) == -250


# --- one move's accuracy -------------------------------------------------------------------------------------

def test_a_move_that_loses_nothing_or_gains_is_perfect():
    assert analysis.move_accuracy(60, 60) == 100
    assert analysis.move_accuracy(60, 75) == 100


@pytest.mark.parametrize("drop, expected", [(5, 80.8), (10, 64.6), (20, 41.0), (50, 9.5)])
def test_move_accuracy_for_a_drop_in_win_percent(drop, expected):
    """Worked by hand from Lichess's curve: 103.1668 * exp(-0.043544 * drop) - 3.1669, plus 1."""
    assert analysis.move_accuracy(80, 80 - drop) == pytest.approx(expected, abs=0.1)


def test_accuracy_never_leaves_zero_to_one_hundred_and_falls_as_the_drop_grows():
    values = [analysis.move_accuracy(100, 100 - d) for d in range(0, 101, 5)]
    assert all(0 <= v <= 100 for v in values) and values == sorted(values, reverse=True)
    assert values[-1] == 0


# --- calling a move an inaccuracy, mistake or blunder ---------------------------------------------------------

def edge(threshold):
    """The smallest whole number of centipawns lost from an equal position that reaches `threshold` winning chances."""
    return next(x for x in range(1, 2000) if analysis.winning_chances(0) - analysis.winning_chances(-x) >= threshold)


def test_the_thresholds_sit_where_the_arithmetic_says():
    assert (edge(0.1), edge(0.2), edge(0.3)) == (55, 111, 169)  # tanh(x * 0.00368208 / 2) reaches 0.1, 0.2, 0.3


@pytest.mark.parametrize("lost, verdict", [(54, None), (55, "inaccuracy"), (110, "inaccuracy"), (111, "mistake"),
                                           (168, "mistake"), (169, "blunder"), (900, "blunder")])
def test_a_white_move_from_an_equal_position_is_judged_by_what_it_loses(lost, verdict):
    assert analysis.judgement(("cp", 0), ("cp", -lost), True) == verdict


@pytest.mark.parametrize("lost, verdict", [(54, None), (55, "inaccuracy"), (111, "mistake"), (169, "blunder")])
def test_a_black_move_is_judged_the_same_way_from_the_other_side(lost, verdict):
    assert analysis.judgement(("cp", 0), ("cp", lost), False) == verdict


def test_a_move_that_improves_the_movers_position_is_never_an_error():
    assert analysis.judgement(("cp", 0), ("cp", 500), True) is None
    assert analysis.judgement(("cp", 0), ("cp", -500), False) is None


def test_judgement_uses_the_real_evaluation_not_the_capped_one():
    """-13.64 to -6.90 is about 6.7 win percent points, an inaccuracy; capping both at 10 pawns would hide it."""
    assert analysis.judgement(("cp", -1364), ("cp", -690), False) == "inaccuracy"
    assert analysis.judgement(("cp", -2300), ("cp", -700), False) == "inaccuracy"


@pytest.mark.parametrize("before, verdict", [(50, "blunder"), (-650, "blunder"), (-701, "mistake"), (-800, "mistake"),
                                             (-1000, "inaccuracy"), (-1500, "inaccuracy")])
def test_walking_into_a_forced_mate_is_judged_by_how_you_were_doing(before, verdict):
    assert analysis.judgement(("cp", before), ("mate", -3), True) == verdict
    assert analysis.judgement(("cp", -before), ("mate", 3), False) == verdict  # the same, seen from Black's side


@pytest.mark.parametrize("after, verdict", [(0, "blunder"), (500, "blunder"), (701, "mistake"), (999, "mistake"),
                                            (1000, "inaccuracy"), (3000, "inaccuracy")])
def test_letting_a_forced_mate_go_is_judged_by_what_you_are_left_with(after, verdict):
    assert analysis.judgement(("mate", 4), ("cp", after), True) == verdict
    assert analysis.judgement(("mate", -4), ("cp", -after), False) == verdict


def test_throwing_a_mate_straight_into_the_opponents_is_a_blunder():
    assert analysis.judgement(("mate", 2), ("mate", -3), True) == "blunder"
    assert analysis.judgement(("mate", -2), ("mate", 3), False) == "blunder"


@pytest.mark.parametrize("prev, cur, white", [
    (("mate", 4), ("mate", 3), True),     # closer to mating
    (("mate", 3), ("mate", 6), True),     # a slower mate is still a mate
    (("mate", -4), ("mate", -3), True),   # the opponent's mate, nothing the mover could do about it
    (("mate", -2), ("cp", -300), True),   # escaped a mate
    (("cp", 100), ("mate", 3), True),     # found a mate
    (("mate", -4), ("mate", -6), False),  # Black keeps the mate
    (("mate", 3), ("cp", 200), False),    # White's mate went: nothing for Black to have lost
])
def test_mate_scores_that_are_not_a_mistake_give_no_verdict(prev, cur, white):
    assert analysis.judgement(prev, cur, white) is None


# --- deciding whether a moment is worth a deeper look -----------------------------------------------------------

@pytest.mark.parametrize("lost, expected", [
    (0, False), (2.9, False), (3, True), (5, True), (7, True), (7.1, False),
    (7.9, False), (8, True), (10, True), (12, True), (12.1, False),
    (12.9, False), (13, True), (15, True), (17, True), (17.1, False), (100, False),
])
def test_borderline_is_within_the_margin_of_any_threshold(lost, expected):
    assert analysis.is_borderline(lost) is expected


def test_the_margin_leaves_clear_water_between_the_three_bands():
    # comfortably between two bands, or outside all three: never worth the deeper look
    assert not any(analysis.is_borderline(x) for x in (0, 1, 7.5, 12.5, 20, 30))


# --- accuracy of a whole run of moves -------------------------------------------------------------------------

def test_a_game_where_nothing_ever_changes_is_perfect_for_both_sides():
    result = analysis.game_accuracy(True, [15] * 40)
    assert result == {"white": pytest.approx(100), "black": pytest.approx(100)}


def test_one_bad_move_lowers_only_the_side_that_made_it():
    cps = [15, 15, -300, -300, -300, -300, -300, -300]  # White's second move throws away three pawns
    result = analysis.game_accuracy(True, cps)
    assert result["white"] < 90 and result["black"] == pytest.approx(100)


def test_an_empty_run_has_no_accuracy_and_a_side_with_no_moves_is_left_out():
    assert analysis.game_accuracy(True, []) == {}
    assert set(analysis.game_accuracy(True, [20])) == {"white"}
    assert set(analysis.game_accuracy(False, [20])) == {"black"}


def test_a_game_seen_from_the_other_side_swaps_the_colours():
    cps = [15, 40, 10, 60, -120, -100, -300, -280, -350, -600, -580, -900]
    forward = analysis.game_accuracy(True, cps)
    mirrored = analysis.game_accuracy(False, [-c for c in cps], initial_cp=-analysis.INITIAL_CP)
    assert mirrored["black"] == pytest.approx(forward["white"]) and mirrored["white"] == pytest.approx(forward["black"])


@pytest.mark.parametrize("length", range(1, 31))
def test_runs_of_any_length_are_handled(length):
    result = analysis.game_accuracy(True, [10 * (i % 7) - 30 for i in range(length)])
    assert result and all(0 <= v <= 100 for v in result.values())


def test_long_games_with_a_late_collapse_stay_sane():
    for quiet in (5, 30, 100):
        result = analysis.game_accuracy(True, [15] * quiet + [-400] * quiet)
        assert 0 <= result["white"] <= 100 and result["black"] == pytest.approx(100)


# --- the summary of a game ------------------------------------------------------------------------------------

# Ten plies worked by hand. White loses 200 centipawns at ply 5 (a blunder: 0.36 of winning chances) and Black
# gives back 70 at ply 8 (an inaccuracy: 0.12).
GAME = cp(30, 30, 30, 30, -170, -170, -170, -100, -100, -100)


def test_the_counts_and_average_loss_of_a_worked_game():
    s = analysis.summarise(GAME, middle=4, end=8)
    assert (s.white.inaccuracies, s.white.mistakes, s.white.blunders) == (0, 0, 1)
    assert (s.black.inaccuracies, s.black.mistakes, s.black.blunders) == (1, 0, 0)
    assert s.white.acpl == 40 and s.black.acpl == 14  # 200 lost over 5 moves, 70 lost over 5 moves
    assert s.eval_ply20 is None  # only ten plies


def test_every_phase_has_a_figure_when_the_game_reaches_it():
    s = analysis.summarise(GAME, middle=4, end=8)
    for side in (s.white, s.black):
        assert all(v is not None and 0 <= v <= 100 for v in (side.accuracy, side.acc_opening, side.acc_middle, side.acc_end))
    assert s.white.acc_middle < s.white.acc_opening  # the blunder is in the middlegame
    assert s.black.acc_opening == pytest.approx(100)


def test_a_game_that_never_leaves_the_opening_has_only_that_figure():
    s = analysis.summarise(GAME, middle=None, end=None)
    assert s.white.acc_opening == pytest.approx(s.white.accuracy) and s.white.acc_middle is None and s.white.acc_end is None


def test_a_game_without_an_endgame_has_a_middlegame_to_the_end():
    s = analysis.summarise(GAME, middle=4, end=None)
    assert s.white.acc_middle is not None and s.white.acc_end is None


def test_the_engines_own_best_move_is_never_called_an_error():
    played = ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6", "d2d3", "f8c5", "c2c3", "d7d6"]
    bests = list(played)
    assert analysis.summarise(GAME, 4, 8, bests=bests, played=played).white.blunders == 0
    bests[4] = "d2d4"  # the engine wanted something else at ply 5
    bests[7] = "c5f2"  # and something else at ply 8
    s = analysis.summarise(GAME, 4, 8, bests=bests, played=played)
    assert s.white.blunders == 1 and s.black.inaccuracies == 1


# --- replacing a summary's moments (the deeper re-check plugs its corrected moments back in through this) ------

def test_with_moments_recomputes_the_counts_on_both_sides():
    s = analysis.summarise(GAME, middle=4, end=8)  # white: 1 blunder; black: 1 inaccuracy
    corrected = analysis.with_moments(s, (analysis.Moment(5, "mistake", 12.0),))  # white's ply 5 downgraded, black's ply 8 dropped
    assert (corrected.white.inaccuracies, corrected.white.mistakes, corrected.white.blunders) == (0, 1, 0)
    assert (corrected.black.inaccuracies, corrected.black.mistakes, corrected.black.blunders) == (0, 0, 0)
    assert corrected.moments == (analysis.Moment(5, "mistake", 12.0),)


def test_with_moments_leaves_accuracy_and_acpl_alone():
    s = analysis.summarise(GAME, middle=4, end=8)
    corrected = analysis.with_moments(s, ())
    assert (corrected.white.accuracy, corrected.white.acpl) == (s.white.accuracy, s.white.acpl)
    assert (corrected.black.accuracy, corrected.black.acpl) == (s.black.accuracy, s.black.acpl)
    assert corrected.eval_ply20 == s.eval_ply20


def test_with_moments_on_an_empty_tuple_clears_every_count():
    s = analysis.summarise(GAME, middle=4, end=8)
    corrected = analysis.with_moments(s, ())
    assert (corrected.white.inaccuracies, corrected.white.mistakes, corrected.white.blunders) == (0, 0, 0)
    assert (corrected.black.inaccuracies, corrected.black.mistakes, corrected.black.blunders) == (0, 0, 0)
    assert corrected.moments == ()


def test_the_evaluation_after_ten_moves_each_is_kept():
    scores = cp(*range(0, 240, 10))
    assert analysis.summarise(scores, None, None).eval_ply20 == 190  # after ply 20 = the 20th value
    assert analysis.summarise(scores[:19], None, None).eval_ply20 is None
    big = [("cp", 5000)] * 25
    assert analysis.summarise(big, None, None).eval_ply20 == 1000  # capped like the accuracy


def test_a_game_of_one_move_or_none_does_not_break_the_summary():
    one = analysis.summarise(cp(20), None, None)
    assert one.white.acpl == 0 and one.black.acpl is None and one.black.accuracy is None
    none = analysis.summarise([], None, None)
    assert none.white.accuracy is None and none.white.acpl is None and none.eval_ply20 is None


def test_a_game_that_ends_in_mate_counts_the_mate_as_the_cap():
    scores = cp(10, 10, 10, 10) + [("mate", -3), ("mate", -2), ("mate", -1)]
    s = analysis.summarise(scores, None, None)
    assert s.white.blunders == 1  # ply 5 walked into a forced mate from an equal position


# --- packing the evaluation curve ----------------------------------------------------------------------------

def test_a_curve_survives_packing():
    scores = [("cp", 15), ("cp", -1364), ("cp", 0), ("mate", 3), ("mate", -12), ("cp", 12345)]
    blob = analysis.pack_evals(scores)
    assert len(blob) == 2 * len(scores) and analysis.unpack_evals(blob) == scores


def test_huge_values_are_cut_but_keep_their_sign_and_meaning():
    unpacked = analysis.unpack_evals(analysis.pack_evals([("cp", 99999), ("cp", -99999), ("mate", 5000), ("mate", -5000)]))
    assert unpacked == [("cp", 30000), ("cp", -30000), ("mate", 1500), ("mate", -1500)]


def test_a_mate_with_no_direction_is_refused_and_so_is_a_broken_blob():
    with pytest.raises(ValueError):
        analysis.pack_evals([("mate", 0)])
    with pytest.raises(ValueError):
        analysis.unpack_evals(b"\x01\x02\x03")


def test_a_ninety_move_game_packs_into_a_few_hundred_bytes():
    assert len(analysis.pack_evals(cp(*range(180)))) == 360


# --- pinned figures --------------------------------------------------------------------------------------------

def test_a_synthetic_game_gives_the_same_figures_as_when_this_was_checked_against_lichess():
    """Regression values for an invented 64-ply curve. They pin the window size, the weights and the way the two means
    are combined. The proof that the method matches Lichess is the known-answer test on real analysed games
    (test_known_answers_analysis_private.py)."""
    import math
    scores = cp(*[int(80 * math.sin(i / 3)) - 4 * i + (250 if i == 31 else 0) - (300 if i == 45 else 0) for i in range(64)])
    s = analysis.summarise(scores, 14, 44)
    assert s.white.accuracy == pytest.approx(92.191, abs=0.001) and s.black.accuracy == pytest.approx(94.263, abs=0.001)
    assert s.white.acc_opening == pytest.approx(94.201, abs=0.001) and s.black.acc_opening == pytest.approx(97.981, abs=0.001)
    assert s.white.acc_middle == pytest.approx(79.846, abs=0.001) and s.black.acc_middle == pytest.approx(86.023, abs=0.001)
    assert s.white.acc_end == pytest.approx(98.192, abs=0.001) and s.black.acc_end == pytest.approx(98.963, abs=0.001)
    assert (s.white.blunders, s.black.blunders, s.white.acpl, s.black.acpl, s.eval_ply20) == (1, 1, 17, 14, -72)


def test_the_two_means_are_both_used():
    """A single terrible move drags the harmonic mean down far more than the weighted mean: the result sits between."""
    cps = [15] * 30 + [-600] + [-600] * 30
    result = analysis.game_accuracy(True, cps)
    assert 50 < result["white"] < 95


def test_the_ply_a_phase_starts_on_belongs_to_that_phase():
    """A White blunder at ply 5 is an opening move if the middlegame starts at ply 6, a middlegame move if at ply 5."""
    scores = cp(20, 20, 20, 20, -300, -300, -300, -300, -300, -300)
    in_opening = analysis.summarise(scores, middle=6, end=None)
    in_middle = analysis.summarise(scores, middle=5, end=None)
    assert in_opening.white.acc_opening < 100 and in_opening.white.acc_middle == pytest.approx(100)
    assert in_middle.white.acc_middle < 100 and in_middle.white.acc_opening == pytest.approx(100)
    at_end = analysis.summarise(scores, middle=2, end=5)
    assert at_end.white.acc_end < 100 and at_end.white.acc_middle == pytest.approx(100)


def test_exactly_twenty_plies_is_enough_for_the_evaluation_after_ten_moves():
    assert analysis.summarise(cp(*range(20)), None, None).eval_ply20 == 19


def test_a_long_game_uses_the_widest_window():
    """From 80 plies the window stops growing at 8; pinned on an invented 110-ply curve."""
    import math
    cps = [int(120 * math.sin(i / 4)) - 3 * i + (400 if i == 60 else 0) - (350 if i == 85 else 0) for i in range(110)]
    result = analysis.game_accuracy(True, cps)
    assert result["white"] == pytest.approx(96.449, abs=0.001) and result["black"] == pytest.approx(97.784, abs=0.001)


# --- the clocks ----------------------------------------------------------------------------------------------------------------------

def test_the_method_is_version_five_because_the_recheck_budget_was_corrected():
    assert analysis.METHOD_VERSION == 5


def test_clocks_round_trip_in_tenths_of_a_second_two_bytes_a_ply():
    seconds = [300.0, 299.5, 12.3, 0.0, 6553.5]
    blob = analysis.pack_clocks(seconds)
    assert len(blob) == 10 and analysis.unpack_clocks(blob) == seconds


def test_clocks_are_rounded_to_a_tenth_and_kept_in_range():
    assert analysis.unpack_clocks(analysis.pack_clocks([1.26, 1.24, -5, 99999999])) == [1.3, 1.2, 0.0, 6553.5]


def test_no_clocks_pack_to_nothing_and_a_damaged_blob_is_refused():
    assert analysis.pack_clocks([]) == b"" and analysis.unpack_clocks(b"") == []
    with pytest.raises(ValueError):
        analysis.unpack_clocks(b"\x01")

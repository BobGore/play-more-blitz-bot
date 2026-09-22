"""clock_review.py: how a player spent their time, the wiki's six checks plus pace against the opponent, and the two notes that set the
clock against the engine's verdict. Games are built from move times, so every expected figure can be checked by hand."""

import pytest

import analysis
import clock_review as cr


def build(white, black, base=300, inc=0):
    """The clocks after each ply for a game where White's moves took `white` seconds and Black's took `black` (one list each),
    interleaved as the sites give them: the clock after a move is the previous clock minus the time taken plus the increment."""
    clocks, w, b = [], float(base), float(base)
    for i in range(max(len(white), len(black))):
        if i < len(white):
            w = w - white[i] + inc
            clocks.append(w)
        if i < len(black):
            b = b - black[i] + inc
            clocks.append(b)
    return clocks


def texts(clocks, side="white", base=300, inc=0):
    return [(c.icon, c.text) for c in cr.checks(clocks, side, base, inc)]


WHITE = [2, 3, 5, 2, 18, 5, 5, 5, 5, 5]                    # 55 s in all
BLACK = [3, 3, 5, 4, 5, 5, 5, 5, 5, 5]                     # 45 s in all


# --- reading a time control and the reference -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [("300+5", (300, 5)), ("180", (180, 0)), ("180+0", (180, 0)), (" 600+2 ", (600, 2)), ("1/86400", None),
                                            ("", None), (None, None), ("abc", None), ("0+5", None), ("300+", None), ("-1", None), ("300+5+1", None)])
def test_a_time_control_is_base_and_increment_in_seconds_or_nothing(text, expected):
    assert cr.parse_time_control(text) == expected


def test_the_reference_pace_is_the_wikis_and_falls_away_past_move_sixty():
    assert len(cr.REFERENCE_PACE) == 60 and cr.reference_fraction(1) == 0.9987 and cr.reference_fraction(10) == 0.9297
    assert cr.reference_fraction(30) == 0.5067 and cr.reference_fraction(60) == 0.1761
    assert cr.reference_fraction(0) == 1.0 and cr.reference_fraction(-3) == 1.0
    assert cr.reference_fraction(61) == pytest.approx(0.1761 - 0.0066) and cr.reference_fraction(1000) == 0.03


def test_the_reference_only_applies_to_the_time_controls_it_was_measured_on():
    assert cr.REFERENCE_TIME_CONTROLS == ((180, 0), (300, 0))


@pytest.mark.parametrize("seconds, text", [(0, "0s"), (45, "45s"), (59.4, "59s"), (59.6, "1m 00s"), (72, "1m 12s"), (600, "10m 00s"), (3661, "61m 01s")])
def test_seconds_are_written_as_minutes_and_seconds(seconds, text):
    assert cr.fmt_secs(seconds) == text


def test_a_lead_is_signed_with_a_proper_minus():
    assert cr._signed(65) == "+1m 05s" and cr._signed(-8) == "−8s" and cr._signed(0) == "+0s"


@pytest.mark.parametrize("ply, text", [(1, "1."), (2, "1..."), (15, "8."), (16, "8..."), (45, "23.")])
def test_a_ply_is_a_move_number_with_dots_for_black(ply, text):
    assert cr.label(ply) == text


# --- how long each move took ------------------------------------------------------------------------------------------------------------------

def test_each_sides_moves_are_numbered_and_timed_from_the_clocks():
    moves = cr.moves_of(build(WHITE, BLACK), "white", 300, 0)
    assert [(m.number, m.ply) for m in moves] == [(n, 2 * n - 1) for n in range(1, 11)] and [m.spent for m in moves] == WHITE
    black = cr.moves_of(build(WHITE, BLACK), "black", 300, 0)
    assert [(m.number, m.ply) for m in black] == [(n, 2 * n) for n in range(1, 11)] and [m.spent for m in black] == BLACK
    assert moves[4].clock == 270 and black[9].clock == 255


def test_the_increment_is_added_back_and_a_first_move_faster_than_the_lag_allowance_is_zero():
    moves = cr.moves_of([300.03, 300.03, 302.0, 298.0], "white", 300, 0)               # Lichess starts 0.03 s over the base
    assert moves[0].spent == 0.0 and moves[1].spent == pytest.approx(0.0)                # 300.03 -> 302.0 with no increment: negative, held at 0
    moves = cr.moves_of(build([7, 4], [2, 9], base=300, inc=5), "white", 300, 5)
    assert [m.spent for m in moves] == pytest.approx([7, 4]) and [m.clock for m in moves] == pytest.approx([298, 299])


def test_a_game_with_no_clocks_has_no_moves():
    assert cr.moves_of([], "white", 300, 0) == [] and cr.checks([], "white", 300, 0) == []
    assert cr.moves_of([250.0], "black", 300, 0) == []


# --- the seven checks on one worked game -------------------------------------------------------------------------------------------------------

def test_the_worked_game_reads_as_the_hand_calculation_says():
    assert texts(build(WHITE, BLACK)) == [
        ("•", "Opening (your first 10 moves): 55s = 18% of base time"),
        ("•", "Longest thinks: 5. (18s), 3. (5s), 6. (5s)"),
        ("✓", "You never hit serious time trouble (under 10% of base)"),
        ("✓", "You tracked a strong player's pace closely (worst gap 34s at move 10)"),
        ("•", "Total: you used 55s (opponent 45s); 4m 05s left at the end — you used under half your budget; slower, deeper thought was available for free"),
        ("•", "Clock lead (you minus your opponent): −10s at the end (move 10)"),
        ("⚠", "You finished behind on the clock by 10s"),
    ]


def test_from_blacks_side_the_same_game_is_read_from_blacks_moves():
    lines = texts(build(WHITE, BLACK), "black")
    assert lines[0] == ("✓", "Opening (your first 10 moves): 45s = 15% of base time — nicely quick") and lines[1][1] == "Longest thinks: 3... (5s), 5... (5s), 6... (5s)"
    assert lines[-2] == ("•", "Clock lead (you minus your opponent): +10s at the end (move 10)") and lines[-1] == ("✓", "You finished ahead on the clock by 10s")


# --- 1 opening speed --------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("seconds, icon, ending", [(45, "✓", " — nicely quick"), (46, "•", ""), (75, "•", ""), (76, "⚠", " — too slow; know your openings or play them faster")])
def test_the_opening_is_quick_up_to_15_percent_and_slow_over_25(seconds, icon, ending):
    moves = [seconds / 10] * 10
    first = texts(build(moves + [1] * 5, [1] * 15))[0]
    assert first == (icon, f"Opening (your first 10 moves): {cr.fmt_secs(seconds)} = {seconds / 3:.0f}% of base time{ending}")


def test_a_short_game_reports_the_moves_it_had():
    assert texts(build([2, 2, 2], [2, 2, 2]))[0] == ("✓", "Opening (your first 3 moves): 6s = 2% of base time — nicely quick")


# --- 2 longest thinks --------------------------------------------------------------------------------------------------------------------------------

def test_one_think_over_ten_percent_of_the_base_time_is_flagged():
    moves = [3, 3, 3, 45, 3, 3]
    line = texts(build(moves, [3] * 6))[1]
    assert line == ("⚠", "Longest thinks: 4. (45s), 1. (3s), 2. (3s) — 4. alone ate 15% of your base time")


def test_exactly_ten_percent_is_not_flagged():
    assert texts(build([3, 3, 3, 30, 3, 3], [3] * 6))[1][0] == "•"


def test_no_time_used_gives_no_thinks_line():
    assert not any(t.startswith("Longest") for _, t in texts(build([0, 0, 0], [0, 0, 0])))


def test_the_placement_of_the_deepest_thinks_is_judged_only_for_a_game_that_reached_move_fifteen():
    long = [3] * 20
    long[15], long[19], long[16] = 20, 16, 12                                    # moves 16, 20 and 17: two of the top three inside 15-25
    lines = texts(build(long, [3] * 20))
    assert ("✓", "Your deepest thinks fell in the middlegame (moves 15–25), where games are decided: good placement") in lines
    early = [3] * 20
    early[1], early[2], early[19] = 20, 18, 16                                   # moves 2, 3 and 20
    assert ("•", "Best practice: the longest thinks belong in the middlegame (roughly moves 15–25), not the opening or a lost ending") in texts(build(early, [3] * 20))
    assert not any("middlegame" in t for _, t in texts(build([3, 3, 40] + [3] * 6, [3] * 9)))              # nine moves: no such window


# --- 3 blitzed moves ----------------------------------------------------------------------------------------------------------------------------------

def test_blitzed_moves_after_move_twelve_are_counted_and_flagged_over_forty_percent():
    moves = [4] * 12 + [3, 3, 3, 9, 9, 9, 9, 9, 9, 9]                             # 10 moves after move 12: 3 fast (30%)
    assert ("✓", "3 of your 10 post-opening moves took under 5s") in texts(build(moves, [4] * 22))
    slow_then_fast = [4] * 12 + [3, 3, 3, 3, 3, 9, 9, 9, 9, 9]                    # 5 of 10: 50%
    assert ("⚠", "5 of your 10 post-opening moves took under 5s — moving too fast in positions that deserve thought is the classic cause of avoidable blunders") \
        in texts(build(slow_then_fast, [4] * 22))


def test_forty_percent_exactly_is_not_flagged_and_five_seconds_exactly_is_not_fast():
    moves = [4] * 12 + [3, 3, 3, 3, 9, 9, 9, 9, 9, 9]                             # 4 of 10 = 40%
    assert ("✓", "4 of your 10 post-opening moves took under 5s") in texts(build(moves, [4] * 22))
    assert ("✓", "0 of your 10 post-opening moves took under 5s") in texts(build([4] * 12 + [5] * 10, [4] * 22))


def test_fewer_than_five_moves_after_the_opening_is_too_few_to_judge():
    assert not any("post-opening" in t for _, t in texts(build([4] * 16, [4] * 16)))
    assert any("post-opening" in t for _, t in texts(build([4] * 17, [4] * 17)))


# --- 4 time trouble -----------------------------------------------------------------------------------------------------------------------------------

def test_time_trouble_is_the_first_move_below_ten_percent_of_the_base():
    moves = [10] * 9 + [180, 1, 1]                                               # the clock: 210 after move 9, 30 after move 10, 29 after move 11
    lines = texts(build(moves, [1] * 12))
    assert ("⚠", "Time trouble: your clock dropped below 10% of base at move 11") in lines
    assert not any(t.startswith("You never hit") for _, t in lines)


def test_exactly_ten_percent_is_not_time_trouble():
    lines = texts(build([135, 135], [1, 1]))                                     # 30 s left after move 2: exactly 10%
    assert ("✓", "You never hit serious time trouble (under 10% of base)") in lines


# --- 5 pace against the reference -------------------------------------------------------------------------------------------------------------------

def test_being_far_behind_the_reference_pace_is_flagged_with_where():
    moves = [2, 2, 2, 2, 2, 2, 2, 2, 60, 2]                                      # a huge think at move 9: 60 s behind from there
    line = [t for t in texts(build(moves, [2] * 10)) if "pace" in t[1]][0]
    assert line == ("⚠", "Furthest behind a strong player's pace at move 9: 58s (19% of base) more used than their blitz profile at that point")


def test_staying_ahead_of_the_reference_all_game_is_said():
    moves = [0.1] * 10
    assert ("✓", "You stayed ahead of a strong player's blitz pace for the whole game: in a slower time control, consider whether you are using enough of your time") \
        in texts(build(moves, [1] * 10))


def test_the_reference_is_used_for_three_and_five_minutes_without_increment_only():
    for base, inc in ((180, 0), (300, 0)):
        assert any("pace" in t for _, t in texts(build([3] * 10, [3] * 10, base, inc), base=base, inc=inc))
    for base, inc in ((180, 2), (300, 5), (600, 0), (60, 0)):
        assert not any("pace" in t for _, t in texts(build([3] * 10, [3] * 10, base, inc), base=base, inc=inc))


def test_behind_the_reference_by_exactly_fifteen_percent_is_not_flagged():
    gap_needed = 0.15 * 300                                                      # 45 s behind
    pace = [cr.reference_fraction(k) * 300 for k in range(1, 4)]
    clocks_white = [pace[0] - gap_needed, pace[1] - gap_needed, pace[2] - gap_needed]
    spent, before = [], 300.0
    for clock in clocks_white:
        spent.append(before - clock)
        before = clock
    lines = texts(build(spent, [1, 1, 1]))
    assert not any(t.startswith("Furthest behind") for _, t in lines)


# --- 6 total use ---------------------------------------------------------------------------------------------------------------------------------------

def test_using_over_forty_five_percent_of_the_budget_is_fine_and_under_is_noted():
    heavy = [15] * 10                                                            # 150 s of 300: 50%
    assert [t for t in texts(build(heavy, [10] * 10)) if t[1].startswith("Total")] == [("✓", "Total: you used 2m 30s (opponent 1m 40s); 2m 30s left at the end")]


def test_the_budget_counts_the_increment():
    line = [t for t in texts(build([10] * 10, [10] * 10, 300, 5), inc=5) if t[1].startswith("Total")][0]
    assert line[0] == "•" and "you used 1m 40s" in line[1]                        # 100 s of a 350 s budget: 29%


# --- 7 pace against the opponent ---------------------------------------------------------------------------------------------------------------------

def lead_lines(white, black, side="white"):
    return [t for t in texts(build(white, black), side) if t[1].startswith(("Clock lead", "You led", "You were", "You finished", "The clocks"))]


def test_the_lead_is_shown_at_moves_ten_twenty_thirty_and_the_end():
    (listing, verdict) = lead_lines([5] * 32, [5] * 32)
    assert listing == ("•", "Clock lead (you minus your opponent): +0s at move 10, +0s at move 20, +0s at move 30, +0s at the end (move 32)")
    assert verdict == ("•", "The clocks finished level")


def test_a_game_shorter_than_ten_moves_shows_only_the_end():
    (listing, verdict) = lead_lines([3] * 6, [5] * 6)
    assert listing == ("•", "Clock lead (you minus your opponent): +12s at the end (move 6)") and verdict == ("✓", "You finished ahead on the clock by 12s")


def test_a_lead_that_slipped_is_flagged_with_where_it_peaked():
    white = [2] * 10 + [40] * 3                                                   # ahead by 30 s at move 10 (they used 5 a move), then behind
    black = [5] * 13
    assert lead_lines(white, black)[-1] == ("⚠", "You led on the clock by 30s at move 10 but finished 1m 15s behind: the lead slipped")


def test_a_deficit_won_back_is_noted_as_a_good_thing():
    white = [8] * 10 + [1] * 12
    black = [3] * 10 + [8] * 12
    verdict = lead_lines(white, black)[-1]
    assert verdict[0] == "✓" and verdict[1].startswith("You were 50s behind on the clock at move 10 and finished")


def test_a_lead_smaller_than_three_percent_of_the_base_time_does_not_count():
    assert lead_lines([5] * 10, [5.5] * 10)[-1] == ("•", "The clocks finished level")                # 5 s ahead: under 9 s
    assert lead_lines([5] * 10, [6] * 10)[-1][0] == "✓"                                              # 10 s ahead: over 9 s


def test_only_moves_both_sides_have_made_are_compared():
    (listing, _) = lead_lines([5] * 11, [4] * 10)
    assert listing[1].endswith("at the end (move 10)")


# --- the two notes for Interesting -----------------------------------------------------------------------------------------------------------------

def moment(ply, verdict="mistake", lost=12.0):
    return analysis.Moment(ply, verdict, lost)


def notes(white, black, moments, side="white", base=300, inc=0):
    return cr.interesting_notes(build(white, black, base, inc), side, base, inc, moments)


def test_a_long_think_that_ended_in_a_slip_is_named_with_the_slip():
    result = notes([3, 3, 3, 45, 3, 3], [3] * 6, [moment(7, "blunder", 22.0)])                   # move 4 is ply 7
    assert result == ["Your longest think was 4. (45s, 15% of your base time): it ended in a blunder (−22%). What were you stuck on?"]


def test_a_long_think_that_held_up_is_said_to_have_been_worth_it():
    result = notes([3, 3, 3, 45, 3, 3], [3] * 6, [moment(9, "inaccuracy", 6.0)])
    assert result == ["Your longest think was 4. (45s, 15% of your base time): the move held up, so the time was well spent. What made the position hard?"]


def test_no_think_note_when_the_longest_is_ten_percent_or_less():
    assert notes([3, 3, 3, 30, 3, 3], [3] * 6, []) == []


def test_the_longest_think_of_two_equal_ones_is_the_earlier():
    result = notes([3, 40, 3, 40, 3, 3], [3] * 6, [])
    assert result[0].startswith("Your longest think was 2.")


def test_mistakes_and_blunders_played_in_under_five_seconds_are_counted_with_the_quickest():
    # moves: 1. 2s, 2. 3s, 3. 9s, 4. 1s, 5. 8s; the slips are at plies 3, 5 (moves 2, 3), 7 (move 4) and an inaccuracy at ply 1
    white = [2, 3, 9, 1, 8, 8]
    result = notes(white, [6] * 6, [moment(1, "inaccuracy", 6.0), moment(3, "mistake", 11.0), moment(5, "blunder", 20.0), moment(7, "mistake", 12.0)])
    assert result == ["You played 2 of your 3 mistakes and blunders in under 5 seconds (the quickest, 4., 1s): slowing down at those moments is the cheapest fix."]


def test_a_slow_slip_or_a_fast_inaccuracy_is_not_a_fast_slip():
    assert notes([9, 9, 9, 9], [6] * 4, [moment(3, "blunder", 20.0)]) == []
    assert notes([2, 9, 9, 9], [6] * 4, [moment(1, "inaccuracy", 6.0)]) == []


def test_the_notes_are_from_blacks_side_too():
    result = notes([3] * 6, [3, 3, 3, 45, 3, 3], [moment(8, "mistake", 15.0)], side="black")   # Black's move 4 is ply 8
    assert result == ["Your longest think was 4... (45s, 15% of your base time): it ended in a mistake (−15%). What were you stuck on?"]


def test_a_game_with_no_moves_by_the_player_gives_no_notes():
    assert cr.interesting_notes([], "white", 300, 0, []) == []


# --- the edges of every threshold (found by changing each number and seeing which test noticed) ---------------------------------------------------

def test_a_think_just_over_ten_percent_of_the_base_is_flagged_and_noted():
    line = texts(build([3, 3, 3, 31, 3, 3], [3] * 6))[1]
    assert line == ("⚠", "Longest thinks: 4. (31s), 1. (3s), 2. (3s) — 4. alone ate 10% of your base time")
    assert notes([3, 3, 3, 31, 3, 3], [3] * 6, [])[0].startswith("Your longest think was 4. (31s, 10% of your base time)")


def test_forty_point_eight_percent_blitzed_is_flagged_where_forty_exactly_is_not():
    moves = [4] * 12 + [3] * 20 + [9] * 29                                      # 20 of the 49 moves after move 12 under 5 s
    (line,) = [t for _, t in texts(build(moves, [4] * 61, base=1800), base=1800) if "post-opening" in t]
    assert line.startswith("20 of your 49 post-opening moves took under 5s — moving too fast")


def _placement(thinks, moves=30):
    """The placement line for a game where the given {move number: seconds} were the long thinks."""
    white = [3] * moves
    for number, seconds in thinks.items():
        white[number - 1] = seconds
    return [icon for icon, t in texts(build(white, [3] * moves)) if "deepest thinks" in t or "Best practice" in t]


def test_the_middlegame_window_is_moves_fifteen_to_twenty_five_inclusive():
    assert _placement({15: 20, 25: 18, 1: 16}) == ["✓"]                          # both ends count
    assert _placement({14: 20, 16: 18, 1: 16}) == ["•"]                          # 14 is outside: only one of the three inside
    assert _placement({25: 20, 26: 18, 1: 16}) == ["•"]                          # 26 is outside


def test_a_game_of_exactly_fifteen_moves_is_judged_for_placement_and_fourteen_is_not():
    assert _placement({15: 20, 1: 10}, moves=15) == ["•"] and _placement({14: 20, 1: 10}, moves=14) == []


def test_being_46_seconds_behind_the_reference_is_flagged_where_45_is_the_line():
    white = [46.39] + [0.5] * 9                                                  # 46.0 s behind the reference at move 1
    (line,) = [t for _, t in texts(build(white, [1] * 10)) if "pace" in t]
    assert line == "Furthest behind a strong player's pace at move 1: 46s (15% of base) more used than their blitz profile at that point"


def test_a_clock_exactly_on_the_reference_is_tracking_it_not_ahead_of_it():
    clocks = []
    for k in range(1, 11):
        clocks += [cr.reference_fraction(k) * 300, 299.0 - k]
    assert ("✓", "You tracked a strong player's pace closely (worst gap 0s at move 1)") in texts(clocks)


def test_the_budget_line_at_and_around_forty_five_percent():
    def total(white, base=300, inc=0):
        return [(i, t) for i, t in texts(build(white, [1] * len(white), base, inc), base=base, inc=inc) if t.startswith("Total")][0]
    assert total([13.5] * 10)[0] == "✓"                                          # exactly 135 s of 300: not under
    assert total([13.6] * 10)[0] == "✓"                                          # 136 s
    assert total([13.4] * 10)[0] == "•"                                          # 134 s
    assert total([15] * 10, base=300, inc=5)[0] == "•"                           # 150 s, but the budget is 350 with the increment: 43%


@pytest.mark.parametrize("white, black, verdict", [
    ([0, 9, 9], [9, 0, 0], ("⚠", "You led on the clock by 9s at move 1 but finished 9s behind: the lead slipped")),                     # peak +9, final -9: both exactly on the margin
    ([9, 0, 0], [0, 9, 9], ("✓", "You were 9s behind on the clock at move 1 and finished 9s ahead")),                                    # low -9, final +9
    ([0, 0, 0], [3, 3, 3], ("✓", "You finished ahead on the clock by 9s")),                                                              # only the final, +9
    ([3, 3, 3], [0, 0, 0], ("⚠", "You finished behind on the clock by 9s")),                                                             # only the final, -9
    ([1, 1, 1], [0, 0, 0], ("•", "The clocks finished level")),                                                                          # -3: under the margin
])
def test_the_clock_lead_rules_at_exactly_three_percent_of_the_base(white, black, verdict):
    assert lead_lines(white, black)[-1] == verdict


# --- a game decided on time says who won on time ------------------------------------------------------------------------------------------------

def verdicts(white, black, on_time, side="white"):
    return [(c.icon, c.text) for c in cr.checks(build(white, black), side, 300, 0, on_time=on_time)][-2:]


def test_winning_on_time_while_behind_on_the_last_readings_says_so_and_does_not_say_you_finished_behind():
    listing, verdict = verdicts([3, 3, 3], [0, 0, 0], "won")                                        # 9 s behind at the last readings
    assert listing == ("•", "Clock lead (you minus your opponent): −9s at the end (move 3)")
    assert verdict == ("✓", "You won on time: your opponent's clock ran out, although at the last readings you were 9s behind")


def test_winning_on_time_while_ahead_or_level_is_just_that():
    assert verdicts([0, 0, 0], [3, 3, 3], "won")[1] == ("✓", "You won on time: your opponent's clock ran out")
    assert verdicts([1, 1, 1], [0, 0, 0], "won")[1] == ("✓", "You won on time: your opponent's clock ran out")                # 3 s behind: under the margin


def test_losing_on_time_says_your_clock_ran_out_and_notes_a_lead_at_the_last_readings():
    assert verdicts([3, 3, 3], [0, 0, 0], "lost")[1] == ("⚠", "You lost on time: your clock ran out")
    assert verdicts([0, 0, 0], [3, 3, 3], "lost")[1] == ("⚠", "You lost on time: your clock ran out, although at the last readings you were 9s ahead")


def test_the_time_result_is_read_from_the_players_side():
    assert verdicts([3, 3, 3], [0, 0, 0], "won", "black")[1] == ("✓", "You won on time: your opponent's clock ran out")             # black was ahead: no "although"
    assert verdicts([3, 3, 3], [0, 0, 0], "lost", "black")[1] == ("⚠", "You lost on time: your clock ran out, although at the last readings you were 9s ahead")


def test_the_margin_for_the_although_is_exactly_three_percent_of_the_base():
    assert verdicts([3, 3, 3], [0, 0, 0], "won")[1][1].endswith("9s behind") and "although" not in verdicts([2.9, 3, 3], [0, 0, 0], "won")[1][1]


def test_without_a_time_result_the_ordinary_comparison_is_made():
    assert verdicts([3, 3, 3], [0, 0, 0], None)[1] == ("⚠", "You finished behind on the clock by 9s")


def test_a_small_lead_at_the_last_readings_is_not_mentioned_when_you_lost_on_time():
    assert verdicts([0, 0, 0], [1, 1, 1], "lost")[1] == ("⚠", "You lost on time: your clock ran out")                           # 3 s ahead: under the margin
    assert verdicts([0, 0, 0], [3, 3, 3], "lost")[1][1].endswith("you were 9s ahead")                                            # exactly the margin

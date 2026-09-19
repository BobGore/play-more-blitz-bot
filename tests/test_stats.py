import pytest
from helpers import at, game

import stats


def month(*specs):
    """Games from (result, day) pairs; rating_after drifts so each game is distinct."""
    return [game(result, when=at(day, hour), rating_after=1500 + i) for i, (result, day, hour) in enumerate(specs)]


# --- Tally -----------------------------------------------------------------


def test_score_is_wins_plus_half_the_draws():
    assert stats.Tally("x", 4, 2, 2, 0).score == 0.75
    assert stats.Tally("x", 28, 12, 2, 14).score == pytest.approx(13 / 28)


def test_score_of_nothing_is_none():
    assert stats.Tally("x").score is None


# --- summarise -------------------------------------------------------------


def test_no_games_is_a_valid_month():
    r = stats.summarise([], start_rating=1400)
    assert (r.games, r.wins, r.draws, r.losses, r.score) == (0, 0, 0, 0, None)
    assert (r.start_rating, r.end_rating, r.high, r.low) == (1400, 1400, 1400, 1400)
    assert (r.days_played, r.longest_play_streak, r.best_win_streak, r.most_games_in_day, r.busiest_day) == (0, 0, 0, 0, None)


def test_record_and_rating_line():
    games = [
        game("W", when=at(1), rating_after=1510),
        game("L", when=at(2), rating_after=1490),
        game("D", when=at(3), rating_after=1490),
    ]
    r = stats.summarise(games, start_rating=1500)
    assert (r.games, r.wins, r.draws, r.losses) == (3, 1, 1, 1)
    assert r.score == 0.5
    assert (r.start_rating, r.end_rating, r.high, r.low) == (1500, 1490, 1510, 1490)


def test_high_and_low_include_the_start_rating():
    only_losses = [game("L", when=at(1), rating_after=1480), game("L", when=at(2), rating_after=1460)]
    r = stats.summarise(only_losses, start_rating=1500)
    assert (r.high, r.low) == (1500, 1460)


def test_input_order_does_not_matter():
    games = [game("W", when=at(5), rating_after=1520), game("L", when=at(2), rating_after=1490)]
    r = stats.summarise(games, start_rating=1500)
    assert r.end_rating == 1520  # the later game, whichever came first in the list


def test_best_win_streak_is_broken_by_draws_and_losses():
    results = "WWDWWWLW"
    games = [game(x, when=at(1 + i)) for i, x in enumerate(results)]
    assert stats.summarise(games, 1500).best_win_streak == 3


def test_best_win_streak_with_no_wins_is_zero():
    assert stats.summarise([game("L"), game("D", when=at(2))], 1500).best_win_streak == 0


def test_play_streak_counts_consecutive_days_and_ignores_gaps():
    days = [3, 4, 5, 6, 17, 18, 25]
    games = [game("W", when=at(d)) for d in days]
    r = stats.summarise(games, 1500)
    assert r.days_played == 7
    assert r.longest_play_streak == 4


def test_several_games_on_one_day_are_one_day_played():
    games = [game("W", when=at(4, h)) for h in (9, 10, 11)] + [game("W", when=at(5))]
    r = stats.summarise(games, 1500)
    assert (r.days_played, r.longest_play_streak) == (2, 2)


def test_busiest_day_and_its_tie_break():
    games = [game("W", when=at(9, h)) for h in (1, 2)] + [game("W", when=at(4, h)) for h in (1, 2)] + [game("W", when=at(20))]
    r = stats.summarise(games, 1500)
    assert (r.most_games_in_day, r.busiest_day) == (2, at(4).date())  # a tie goes to the earlier day


def test_a_game_ending_just_after_midnight_utc_belongs_to_the_next_day():
    games = [game("W", when=at(4, 23, 59)), game("W", when=at(5, 0, 1))]
    r = stats.summarise(games, 1500)
    assert r.days_played == 2 and r.longest_play_streak == 2


# --- opening tables --------------------------------------------------------


def opening_games(colour, *pairs):
    return [game(result, colour=colour, opening=name, when=at(1 + i)) for i, (name, result) in enumerate(pairs)]


def test_openings_are_split_by_colour():
    games = opening_games("white", ("London-System", "W")) + opening_games("black", ("Scotch-Game", "L"))
    tables = stats.opening_tables(games, min_games=1)
    assert [t.label for t in tables["white"]] == ["London System"]
    assert [t.label for t in tables["black"]] == ["Scotch Game"]


def test_variations_are_folded_and_a_row_shows_its_record_and_score():
    games = opening_games(
        "black",
        ("Caro-Kann-Defense-Panov-Attack", "W"),
        ("Caro-Kann-Defense-Exchange-Variation", "D"),
        ("Caro-Kann-Defense", "L"),
        ("Caro-Kann-Defense-Breyer-Variation", "W"),
    )
    (row,) = stats.opening_tables(games)["black"]
    assert row == stats.Tally("Caro-Kann Defense", 4, 2, 1, 1)
    assert row.score == 0.625


def test_rare_openings_go_into_all_others_which_is_last():
    games = opening_games(
        "white",
        ("London-System", "W"), ("London-System", "L"),
        ("Scotch-Game", "W"),
        ("Dutch-Defense", "L"),
        ("Vienna-Game", "W"), ("Vienna-Game", "W"), ("Vienna-Game", "W"),
    )
    rows = stats.opening_tables(games)["white"]
    assert [r.label for r in rows] == ["Vienna Game", "London System", "All others"]
    assert rows[-1] == stats.Tally("All others", 2, 1, 0, 1)


def test_openings_sort_by_games_then_score_then_name():
    games = opening_games(
        "white",
        ("Scotch-Game", "L"), ("Scotch-Game", "L"),
        ("Dutch-Defense", "W"), ("Dutch-Defense", "W"),
        ("Bird-Opening", "W"), ("Bird-Opening", "W"),
        ("Vienna-Game", "W"), ("Vienna-Game", "W"), ("Vienna-Game", "W"),
    )
    assert [r.label for r in stats.opening_tables(games)["white"]] == [
        "Vienna Game", "Bird Opening", "Dutch Defense", "Scotch Game",
    ]


def test_no_games_gives_empty_tables():
    assert stats.opening_tables([]) == {"white": [], "black": []}


def test_a_game_with_no_opening_counts_as_unknown():
    games = opening_games("white", ("", "W"), ("", "L"))
    games[0] = game("W", opening=None)
    assert stats.opening_tables(games)["white"][0].label == "Unknown"


# --- records ---------------------------------------------------------------


def test_best_win_and_worst_loss_and_extremes():
    games = [
        game("W", opponent="a", opponent_rating=1400, when=at(1)),
        game("W", opponent="b", opponent_rating=1600, when=at(2)),
        game("L", opponent="c", opponent_rating=1300, when=at(3)),
        game("L", opponent="d", opponent_rating=1700, when=at(4)),
        game("D", opponent="e", opponent_rating=1200, when=at(5)),
    ]
    rec = stats.records(games)
    assert (rec.best_win.opponent, rec.best_win.rating) == ("b", 1600)
    assert (rec.worst_loss.opponent, rec.worst_loss.rating) == ("c", 1300)
    assert (rec.strongest_opponent.opponent, rec.strongest_opponent.result) == ("d", "L")
    assert (rec.weakest_opponent.opponent, rec.weakest_opponent.result) == ("e", "D")


def test_records_with_no_wins_or_losses_are_none_not_a_crash():
    rec = stats.records([game("D", opponent_rating=1500)])
    assert rec.best_win is None and rec.worst_loss is None
    assert rec.strongest_opponent is not None


def test_records_with_no_games():
    rec = stats.records([])
    assert rec == stats.Records(None, None, None, None, None, None)


def test_quickest_mates_won_and_lost():
    games = [
        game("W", ending="checkmated", moves=40, when=at(1)),
        game("W", ending="checkmated", moves=25, when=at(2)),
        game("W", ending="resigned", moves=10, when=at(3)),  # a resignation is not a mate
        game("L", ending="checkmated", moves=39, when=at(4)),
    ]
    rec = stats.records(games)
    assert (rec.quickest_mate_won, rec.quickest_mate_lost) == (25, 39)


def test_games_with_no_opponent_rating_are_skipped_by_the_rating_records():
    rec = stats.records([game("W", opponent_rating=None)])
    assert rec.best_win is None and rec.strongest_opponent is None


def test_a_tie_for_strongest_opponent_goes_to_the_earlier_game():
    games = [game("W", opponent="first", opponent_rating=1600, when=at(1)), game("W", opponent="second", opponent_rating=1600, when=at(2))]
    assert stats.records(games).strongest_opponent.opponent == "first"


# --- splits ----------------------------------------------------------------


def test_opponent_rating_bands_are_relative_to_the_rating_before_the_game():
    games = [
        game("W", opponent_rating=1600, rating_before=1500, when=at(1)),  # +100: higher
        game("L", opponent_rating=1550, rating_before=1500, when=at(2)),  # exactly +50: similar
        game("W", opponent_rating=1449, rating_before=1500, when=at(3)),  # -51: lower
        game("D", opponent_rating=1451, rating_before=1500, when=at(4)),  # -49: similar
    ]
    by = {t.label: t for t in stats.splits(games, 1500).by_opponent_rating}
    assert (by["Higher"].games, by["Similar"].games, by["Lower"].games) == (1, 2, 1)
    assert by["Similar"] == stats.Tally("Similar", 2, 0, 1, 1)


def test_first_chesscom_game_uses_the_start_rating_and_later_ones_chain():
    # rating_before is None throughout, as Chess.com's first game is; the second
    # game chains from the first game's rating_after (1400), not the start.
    games = [
        game("W", opponent_rating=1520, rating_before=None, rating_after=1400, when=at(1)),  # vs start 1500: +20 similar
        game("W", opponent_rating=1520, rating_before=None, rating_after=1410, when=at(2)),  # vs 1400: +120 higher
    ]
    by = {t.label: t.games for t in stats.splits(games, 1500).by_opponent_rating}
    assert by == {"Similar": 1, "Higher": 1}


def test_colour_split():
    games = [game("W", colour="white"), game("L", colour="black", when=at(2)), game("W", colour="black", when=at(3))]
    by = {t.label: t for t in stats.splits(games, 1500).by_colour}
    assert by["White"] == stats.Tally("White", 1, 1, 0, 0)
    assert by["Black"] == stats.Tally("Black", 2, 1, 0, 1)


def test_weekday_split_is_ordered_monday_first_and_only_lists_days_played():
    # 2026-09-07 is a Monday, 09-06 a Sunday.
    games = [game("W", when=at(6)), game("W", when=at(7)), game("L", when=at(9))]
    assert [t.label for t in stats.splits(games, 1500).by_weekday] == ["Mon", "Wed", "Sun"]


@pytest.mark.parametrize(
    "hour,label",
    [(0, "Night"), (5, "Night"), (6, "Morning"), (11, "Morning"), (12, "Afternoon"), (16, "Afternoon"),
     (17, "Evening"), (20, "Evening"), (21, "Night"), (23, "Night")],
)
def test_time_of_day_buckets(hour, label):
    (row,) = stats.splits([game("W", when=at(5, hour))], 1500).by_time_of_day
    assert row.label == label


def test_splits_of_no_games_are_empty():
    s = stats.splits([], 1500)
    assert (s.by_opponent_rating, s.by_colour, s.by_weekday, s.by_time_of_day) == ([], [], [], [])

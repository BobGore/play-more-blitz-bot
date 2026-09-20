"""The !mystats and !mystatsfull layouts. Invented players only."""

import pytest
from helpers import at, game

import render
import stats

FENCE = "```"


def month_of_games():
    """Six games with known answers: 3W 1D 2L, ending 1500 -> 1512."""
    return [
        game("W", when=at(2, 10), rating_after=1510, colour="white", opening="London-System", opponent="rival_a", opponent_rating=1550),
        game("L", when=at(2, 11), rating_after=1495, colour="white", opening="London-System", opponent="rival_b", opponent_rating=1400, ending="checkmated", moves=39),
        game("D", when=at(3, 22), rating_after=1495, colour="black", opening="Caro-Kann-Defense", opponent="rival_c", opponent_rating=1500),
        game("W", when=at(4, 9), rating_after=1507, colour="black", opening="Caro-Kann-Defense", opponent="rival_d", opponent_rating=1520, ending="checkmated", moves=25),
        game("W", when=at(4, 10), rating_after=1520, colour="white", opening="Scotch-Game", opponent="rival_e", opponent_rating=1480),
        game("L", when=at(9, 20), rating_after=1512, colour="black", opening="Dutch-Defense", opponent="rival_f", opponent_rating=1600),
    ]


GAMES = month_of_games()
RESULTS = stats.summarise(GAMES, 1500)


def stats_messages():
    return render.render_mystats("Example_Player", "chess.com", "2026-09", RESULTS, stats.opening_tables(GAMES))


def full_messages():
    return render.render_mystatsfull("Example_Player", "chess.com", "2026-09", RESULTS, stats.records(GAMES), stats.splits(GAMES, 1500))


# --- the results block -----------------------------------------------------


def test_the_results_block_is_exactly_the_agreed_layout():
    assert render.results_block(RESULTS) == (
        "Games 6    W 3   D 1   L 2    Score 58%\n"
        "Rating  start 1500   end 1512   net +12   high 1520   low 1495\n"
        "Days played 4   Longest play streak 3 days   Best win streak 2   Most games in a day 2"
    )


def test_a_negative_net_and_a_single_day_streak_read_naturally():
    r = stats.summarise([game("L", when=at(2), rating_after=1480)], 1500)
    block = render.results_block(r)
    assert "net -20" in block and "Longest play streak 1 day " in block


def test_no_change_is_shown_as_zero_not_plus_zero():
    r = stats.summarise([game("D", when=at(2), rating_after=1500)], 1500)
    assert "net 0 " in render.results_block(r)


@pytest.mark.parametrize("score,text", [(0.4643, "46%"), (0.5, "50%"), (0.625, "63%"), (1.0, "100%"), (0.0, "0%"), (None, "-")])
def test_scores_round_half_up(score, text):
    assert render._pct(score) == text


# --- !mystats --------------------------------------------------------------


def test_mystats_is_a_title_the_results_block_and_an_opening_table_per_colour():
    (message,) = stats_messages()
    assert message == (
        "`Example_Player` · Chess.com · September 2026 so far\n"
        "```\n"
        "Games 6    W 3   D 1   L 2    Score 58%\n"
        "Rating  start 1500   end 1512   net +12   high 1520   low 1495\n"
        "Days played 4   Longest play streak 3 days   Best win streak 2   Most games in a day 2\n"
        "```\n"
        "**As White**\n"
        "```\n"
        "Opening        G  W  D  L  Score\n"
        "London System  2  1  0  1    50%\n"
        "All others     1  1  0  0   100%\n"
        "```\n"
        "**As Black**\n"
        "```\n"
        "Opening            G  W  D  L  Score\n"
        "Caro-Kann Defense  2  1  1  0    75%\n"
        "All others         1  0  0  1     0%\n"
        "```"
    )


def test_the_number_columns_widen_only_when_the_numbers_do():
    lines = render.tally_table([stats.Tally("a", 375, 177, 10, 188), stats.Tally("b", 2, 1, 0, 1)], "Opening")
    assert lines[0] == "Opening    G    W   D    L  Score"
    assert lines[1] == "a        375  177  10  188    49%"  # (177 + 5) / 375 = 48.5%, rounded half up
    assert lines[2] == "b          2    1   0    1    50%"


def test_a_player_with_no_games_gets_the_zeros_and_a_plain_message():
    empty = stats.summarise([], 1500)
    (message,) = render.render_mystats("Example_Player", "lichess", "2026-09", empty, stats.opening_tables([]))
    assert "Games 0    W 0   D 0   L 0    Score -" in message
    assert message.endswith("No rated blitz games yet this month.")
    assert "As White" not in message and "Lichess" in message


def test_a_colour_with_no_games_says_so():
    only_white = [g for g in GAMES if g.colour == "white"]
    (message,) = render.render_mystats("Example_Player", "chess.com", "2026-09", stats.summarise(only_white, 1500), stats.opening_tables(only_white))
    assert "**As Black**\nNo games." in message


def test_the_site_is_named_properly():
    assert "· Lichess ·" in render.render_mystats("x", "lichess", "2026-09", RESULTS, stats.opening_tables(GAMES))[0]


def test_a_huge_opening_list_splits_into_messages_that_fit_and_repeats_the_header():
    # 600 different opening families, each played twice, so every one gets its own row.
    families = [f"Zork{i}-Defense" for i in range(600)]
    doubled = [game(r, when=at(1 + i % 28, i % 24, i % 60), colour="white", opening=name, rating_after=1500)
               for i, name in enumerate(families) for r in "WL"]
    messages = render.render_mystats("Example_Player", "chess.com", "2026-09", stats.summarise(doubled, 1500), stats.opening_tables(doubled))
    assert len(messages) > 1 and all(len(m) <= 2000 for m in messages)
    body = "\n".join(messages)
    assert body.count("Opening") >= len(messages) - 1  # each table part repeats its header row
    assert all(f"Zork{i} Defense" in body for i in (0, 299, 599))  # and nothing was dropped


# --- !mystatsfull ----------------------------------------------------------


def test_the_records_block():
    (message,) = full_messages()[:1]
    assert "Best win           1550  rival_a" in message
    assert "Worst loss         1400  rival_b" in message
    assert "Strongest opponent 1600  rival_f (L)" in message
    assert "Weakest opponent   1400  rival_b (L)" in message
    assert "Quickest mate won  25 moves" in message
    assert "Quickest mate lost 39 moves" in message


def test_records_with_nothing_to_show_are_dashes():
    rec = stats.records([game("D", opponent_rating=1500)])
    block = render.records_block(rec)
    assert "Best win           -" in block and "Quickest mate won  -" in block and "Quickest mate lost -" in block


def test_the_splits_are_labelled_with_what_the_buckets_mean():
    text = "\n".join(full_messages())
    assert "Higher (>50 above)" in text and "Similar (within 50)" in text
    assert "Night (21-06)" in text or "Afternoon (12-17)" in text
    for title in ("By opponent rating", "By colour", "By weekday", "By time of day (UTC)"):
        assert f"**{title}**" in text


def test_the_weekday_table_is_in_calendar_order_with_only_days_played():
    text = "\n".join(full_messages())
    weekday = text.split("**By weekday**")[1].split(FENCE)[1]
    days = [line.split()[0] for line in weekday.strip().splitlines()[1:]]
    # Games fall on 2, 3, 4 and 9 September 2026: Wednesday, Thursday, Friday and Wednesday again.
    assert days == ["Wed", "Thu", "Fri"]


def test_a_full_summary_of_a_player_with_no_games_is_short_and_kind():
    empty = stats.summarise([], 1500)
    (message,) = render.render_mystatsfull("Example_Player", "chess.com", "2026-09", empty, stats.records([]), stats.splits([], 1500))
    assert message.endswith("No rated blitz games yet this month.") and "**By colour**" not in message


def test_the_full_title_says_full():
    assert "· full" in full_messages()[0]


def test_everything_fits_one_message_for_an_ordinary_month():
    assert len(stats_messages()) == 1 and len(full_messages()) == 1

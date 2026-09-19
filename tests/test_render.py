from datetime import datetime, timedelta, timezone

import pytest

import render
from store import ResultRow

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
FENCE = "```"


def ago(minutes):
    return (NOW - timedelta(minutes=minutes)).isoformat()


def row(name="alice", site="chess.com", games=10, wins=5, draws=1, losses=4, start=1500, end=1500, gob=False,
        refreshed=12, error=None, has_row=True):
    return ResultRow(site, name, has_row, start if has_row else None, end if has_row else None, games, wins, draws, losses,
                     gob, ago(refreshed) if refreshed is not None else None, error)


def render_rows(rows, target=100):
    return render.render_results(rows, "2026-09", NOW, target)


def table_lines(message):
    """The data lines of a message's code block (without header, rule or fences)."""
    inside = message.split(FENCE)[1].strip("\n").splitlines()
    return inside[2:]


# --- age -------------------------------------------------------------------


@pytest.mark.parametrize(
    "delta,text",
    [
        (timedelta(seconds=0), "just now"),
        (timedelta(seconds=59), "just now"),
        (timedelta(minutes=1), "1 min ago"),
        (timedelta(minutes=59, seconds=59), "59 min ago"),
        (timedelta(hours=1), "1 h ago"),
        (timedelta(hours=1, minutes=5), "1 h 5 min ago"),
        (timedelta(hours=23, minutes=59), "23 h 59 min ago"),
        (timedelta(hours=24), "1 day ago"),
        (timedelta(days=3, hours=5), "3 days ago"),
    ],
)
def test_age(delta, text):
    assert render.age(delta) == text


# --- the exact layout ------------------------------------------------------


def test_the_layout_is_exactly_this():
    rows = [
        row("Chess_Fan_123", "chess.com", 28, 12, 2, 14, 1466, 1448),
        row("Hot_Streak", "lichess", 104, 60, 4, 40, 1400, 1477, gob=True, refreshed=25),
        row("Getting_There", "lichess", 42, 20, 2, 20, 1350, 1350, gob=True),
        row("Quiet_Player", "chess.com", 0, 0, 0, 0, 1500, 1500),
    ]
    (message,) = render_rows(rows)
    assert message == (
        "**September 2026 so far**\n"
        "```\n"
        "  #  Player         Site   Gm    W-D-L  Gain  Challenge\n"
        "-------------------------------------------------------\n"
        "  1  Hot_Streak     LI    104  60-4-40   +77  100GOB ✅\n"
        "  2  Getting_There  LI     42  20-2-20     0  100GOB 42/100\n"
        "  3  Chess_Fan_123  CC     28  12-2-14   -18\n"
        "  4  Quiet_Player   CC      0    0-0-0     0\n"
        "```\n"
        "Updated 25 min ago · CC = Chess.com, LI = Lichess"
    )


# --- who is shown and in what order ----------------------------------------


def test_everyone_is_shown_most_games_first_and_ties_by_name():
    rows = [row("zed", games=5), row("Amy", games=5), row("bob", games=9), row("quiet", games=0)]
    names = [line[5:].split()[0] for line in table_lines(render_rows(rows)[0])]
    assert names == ["bob", "Amy", "zed", "quiet"]  # zero-game players stay in: that is the nag


def test_gain_is_end_minus_start_with_a_sign():
    rows = [row("up", games=3, start=1500, end=1536), row("down", games=2, start=1500, end=1482),
            row("flat", games=1, start=1500, end=1500)]
    gains = {line[5:].split()[0]: line.split()[-1] for line in table_lines(render_rows(rows)[0])}
    assert gains == {"up": "+36", "down": "-18", "flat": "0"}


def test_a_long_name_is_cut_with_an_ellipsis_and_does_not_widen_the_table():
    rows = [row("A_Really_Long_Username_Indeed", games=3), row("short", games=2)]
    lines = table_lines(render_rows(rows)[0])
    assert "A_Really_Long_Usern…" in lines[0]
    assert len(lines[0]) == len(lines[1])


def test_the_site_shows_as_a_short_code():
    lines = table_lines(render_rows([row("a", "chess.com", games=2), row("b", "lichess", games=1)])[0])
    assert " CC " in lines[0] and " LI " in lines[1]


# --- the 100GOB column -----------------------------------------------------


def test_challenge_column_is_blank_for_players_not_in_it():
    (line,) = table_lines(render_rows([row(games=50, gob=False)])[0])
    assert "100GOB" not in line


def test_challenge_shows_progress_then_a_tick_at_the_target():
    rows = [row("a", games=99, gob=True), row("b", games=100, gob=True), row("c", games=143, gob=True)]
    lines = table_lines(render_rows(rows)[0])
    assert lines[0].endswith("100GOB ✅") and lines[1].endswith("100GOB ✅")  # 143 and 100
    assert lines[2].endswith("100GOB 99/100")


def test_the_target_is_configurable():
    (line,) = table_lines(render_rows([row(games=12, gob=True)], target=20)[0])
    assert line.endswith("100GOB 12/20")


# --- flags and the footer --------------------------------------------------


def test_a_failed_refresh_is_flagged_with_its_reason_and_still_shows_the_old_numbers():
    rows = [row("ok", games=5), row("broken", games=9, error="couldn't reach chess.com (TimeoutError)")]
    (message,) = render_rows(rows)
    lines = table_lines(message)
    assert lines[0].startswith("! ") and "9" in lines[0]  # broken has more games, so it is first
    assert "\n! `broken`: couldn't reach chess.com (TimeoutError)" in message


def test_a_player_never_counted_is_flagged_and_shown_with_dashes():
    rows = [row("counted", games=5), row("waiting", games=0, refreshed=None)]
    (message,) = render_rows(rows)
    waiting = table_lines(message)[1]
    assert waiting.startswith("? ") and waiting.rstrip().endswith("-")
    assert "\n? `waiting`: not counted yet" in message


def test_a_player_with_no_row_for_the_month_is_treated_as_not_counted_yet():
    (message,) = render_rows([row("new", has_row=False, games=0, refreshed=None)])
    assert table_lines(message)[0].startswith("? ")
    assert "`new`: not counted yet" in message


def test_the_footer_reports_the_oldest_successful_refresh():
    rows = [row("a", refreshed=3), row("b", refreshed=47), row("c", refreshed=10)]
    assert "Updated 47 min ago" in render_rows(rows)[0]


def test_the_footer_says_so_when_nothing_has_been_refreshed():
    (message,) = render_rows([row(games=0, refreshed=None)])
    assert "Not updated yet" in message


def test_the_title_names_the_month():
    assert render_rows([row()])[0].startswith("**September 2026 so far**\n")


def test_nobody_registered_says_how_to_start():
    (message,) = render_rows([])
    assert "Nobody is registered yet" in message and "!add" in message


# --- next month's sign-ups -------------------------------------------------


def render_with_signups(names, month="2026-09"):
    return render.render_results([row()], month, NOW, 100, names)


def test_signups_are_listed_under_the_table_for_the_month_after():
    (message,) = render_with_signups(["Alice", "bob"])
    assert message.endswith("100GOB sign-ups for October 2026: `Alice`, `bob`")


def test_no_signups_means_no_line():
    assert "sign-ups" not in render_with_signups([])[0]


def test_december_signups_are_for_january_of_the_next_year():
    assert "100GOB sign-ups for January 2027: `a`" in render_with_signups(["a"], month="2026-12")[0]


def test_a_long_signup_list_wraps_onto_short_lines_and_names_every_player_once():
    names = [f"player_{i:03d}" for i in range(150)]
    messages = render_with_signups(names)
    assert all(len(m) <= 2000 for m in messages)
    text = "\n".join(messages)
    assert all(text.count(f"`{n}`") == 1 for n in names)
    signup_lines = [ln for ln in text.splitlines() if "`player_" in ln]
    assert all(len(ln) <= 110 for ln in signup_lines)  # wrapped, not one enormous line


# --- long tables -----------------------------------------------------------


def many(n, **kw):
    return [row(f"player_{i:03d}", games=200 - i, **kw) for i in range(n)]


def test_a_long_table_splits_and_every_message_fits():
    messages = render_rows(many(120))
    assert len(messages) > 1
    assert all(len(m) <= 2000 for m in messages)
    assert all(len(m) <= render.MAX_MESSAGE for m in messages)


def test_every_player_appears_exactly_once_in_the_right_order_across_messages():
    messages = render_rows(many(120))
    names = [line[5:].split()[0] for m in messages for line in table_lines(m)]
    assert names == [f"player_{i:03d}" for i in range(120)]


def test_ranks_keep_counting_across_messages():
    messages = render_rows(many(120))
    ranks = [int(line[1:].split()[0]) for m in messages for line in table_lines(m)]
    assert ranks == list(range(1, 121))


def test_the_header_repeats_on_every_message_but_the_title_and_footer_appear_once():
    messages = render_rows(many(120))
    assert all("Player" in m.split(FENCE)[1] for m in messages)
    assert sum("so far" in m for m in messages) == 1 and messages[0].startswith("**")
    assert sum("Updated" in m for m in messages) == 1 and "Updated" in messages[-1]


def test_a_huge_footer_is_split_rather_than_overflowing_and_names_every_failed_player():
    messages = render_rows(many(60, error="x" * 200))  # 60 failures: the footer alone is over one message
    assert all(len(m) <= 2000 for m in messages)
    text = "\n".join(messages)
    assert all(f"`player_{i:03d}`" in text for i in range(60))


def test_an_enormous_list_of_waiting_players_is_split_too():
    messages = render_rows([row(f"a_rather_long_name_{i:04d}", games=0, refreshed=None) for i in range(400)])
    assert all(len(m) <= 2000 for m in messages)
    assert all(f"a_rather_long_name_{i:04d}" in "\n".join(messages) for i in (0, 399))


def test_fit_cuts_on_line_breaks_and_never_over_the_limit():
    text = "\n".join(["x" * 700] * 5)
    pieces = render._fit(text)
    assert all(len(p) <= render.MAX_MESSAGE for p in pieces)
    assert "\n".join(pieces) == text  # nothing lost when it can break on lines


def test_fit_hard_cuts_a_single_line_longer_than_a_message():
    pieces = render._fit("y" * (render.MAX_MESSAGE * 2 + 10))
    assert [len(p) for p in pieces] == [render.MAX_MESSAGE, render.MAX_MESSAGE, 10]


def test_a_single_small_table_is_one_message():
    assert len(render_rows(many(10))) == 1

"""Looking back: reading a month from what people type, the stored history, and !results, !history and !mystats for
a month that isn't the current one."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from analysis_helpers import analysed, spec
from helpers import at, game

import analysis_queue as q
import analysis_reports
import bot as botmod
import monthargs
import render
import sources
import store

OK, NO = "✅", "❌"
ALICE, BOB = 1001, 1002
NOW_MONTH = "2026-09"
CHANNEL, DM_CHANNEL = 555, 777


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(sources, "current_month", lambda now=None: NOW_MONTH)
    monkeypatch.setattr(botmod, "ALLOWED_CHANNEL_IDS", {CHANNEL})


def the_server(*member_ids, error=None, guild_id=1):
    async def fetch_member(user_id):
        if user_id in member_ids:
            return SimpleNamespace(id=user_id)
        raise error or discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Member")
    return SimpleNamespace(id=guild_id, fetch_member=AsyncMock(side_effect=fetch_member))


@pytest.fixture(autouse=True)
def on_the_server(monkeypatch):
    """!history is a DM command and checks membership; the others in this file don't touch this, so it's harmless there."""
    channel = SimpleNamespace(id=CHANNEL, guild=the_server(ALICE, BOB), send=AsyncMock())
    monkeypatch.setattr(botmod.bot, "get_channel", lambda channel_id: channel if channel_id == CHANNEL else None)
    return channel


# --- reading a month --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("2026-08", "2026-08"), ("2026/8", "2026-08"), ("2025-12", "2025-12"), (" 2026-01 ", "2026-01"),
    ("august", "2026-08"), ("Aug", "2026-08"), ("AUG.", "2026-08"), ("september", "2026-09"), ("sept", "2026-09"), ("sep", "2026-09"),
    ("january", "2026-01"), ("october", "2025-10"), ("dec", "2025-12"),
    ("last", "2026-08"), ("prev", "2026-08"), ("Previous", "2026-08"), ("this", "2026-09"), ("current", "2026-09"), ("now", "2026-09"),
])
def test_what_people_type_is_read_as_a_month(text, expected):
    assert monthargs.parse_month(text, NOW_MONTH) == expected


@pytest.mark.parametrize("text", ["2026-13", "2026-00", "1999-05", "26-08", "2026", "monday", "sepember", "", "  ", "!", "0", None, 5])
def test_anything_else_is_not_a_month(text):
    assert monthargs.parse_month(text, NOW_MONTH) is None


def test_last_month_from_january_is_the_december_before():
    assert monthargs.parse_month("last", "2026-01") == "2025-12" and monthargs.previous("2026-01") == "2025-12"
    assert monthargs.parse_month("december", "2026-01") == "2025-12" and monthargs.parse_month("january", "2026-01") == "2026-01"


def test_a_month_in_the_future_is_recognised_as_such():
    assert monthargs.is_future("2026-10", NOW_MONTH) and monthargs.is_future("2027-01", NOW_MONTH)
    assert not monthargs.is_future("2026-09", NOW_MONTH) and not monthargs.is_future("2025-12", NOW_MONTH)


# --- what is stored ----------------------------------------------------------------------------------------------------------

def add_month(site, name, month, start, end, games=10, wins=5, draws=1, losses=4, gob=0, owner=ALICE):
    if store.get_player(site, name) is None:
        store.add_player(site, name, owner, month, start)
    with store.transaction() as conn:
        store._open_month(conn, site, name, month, start)
        conn.execute("UPDATE monthly_results SET end_rating = ?, games = ?, wins = ?, draws = ?, losses = ?, in_100gob = ?, refreshed_at = '2026-09-20T12:00:00+00:00' "
                     "WHERE site = ? AND username = ? AND month = ?", (end, games, wins, draws, losses, gob, site, name, month))


def test_the_earliest_month_is_none_until_something_is_recorded():
    assert store.earliest_month() is None
    add_month("lichess", "alice_example", "2026-08", 1400, 1420)
    add_month("lichess", "bob_example", "2026-09", 1500, 1500)
    assert store.earliest_month() == "2026-08"


def test_a_players_history_is_newest_first_with_the_fields_a_table_needs():
    add_month("lichess", "alice_example", "2026-07", 1400, 1390, games=40, wins=18, draws=2, losses=20)
    add_month("lichess", "alice_example", "2026-09", 1420, 1497, games=112, wins=61, draws=5, losses=46, gob=1)
    add_month("lichess", "alice_example", "2026-08", 1390, 1420, games=98)
    add_month("lichess", "bob_example", "2026-09", 1500, 1500)
    rows = store.player_history("lichess", "ALICE_example")
    assert [r["month"] for r in rows] == ["2026-09", "2026-08", "2026-07"]
    assert rows[0] == {"month": "2026-09", "start_rating": 1420, "end_rating": 1497, "games": 112, "wins": 61, "draws": 5, "losses": 46,
                       "in_100gob": 1, "closed_at": None}
    assert store.player_history("chess.com", "alice_example") == [] and store.player_history("lichess", "nobody") == []


def test_the_average_accuracy_of_each_month_is_of_the_players_own_side_and_leaves_out_unanalysed_games():
    store.add_player("lichess", "alice_example", ALICE, "2026-08", 1500)
    from analysis_helpers import side
    analysed(spec(1, month="2026-08"), spec(2, "rival_example", "alice_example", month="2026-08"), spec(3, month="2026-09"),
             white=side(accuracy=80.0), black=side(accuracy=60.0))
    got = analysis_reports.monthly_accuracy("lichess", "alice_example")
    assert got["2026-08"] == (2, pytest.approx(70.0)) and got["2026-09"] == (1, pytest.approx(80.0))
    assert analysis_reports.monthly_accuracy("lichess", "someone_else") == {}


# --- the month-by-month table -----------------------------------------------------------------------------------------------

def hist(**over):
    base = {"month": "2026-08", "start_rating": 1390, "end_rating": 1412, "games": 98, "wins": 50, "draws": 4, "losses": 44, "in_100gob": 0, "closed_at": "x"}
    base.update(over)
    return base


def test_the_table_has_a_line_a_month_and_marks_the_open_one():
    rows = [hist(month="2026-09", start_rating=1412, end_rating=1497, games=112, wins=61, draws=5, losses=46, in_100gob=1, closed_at=None), hist(),
            hist(month="2026-07", start_rating=1401, end_rating=1390, games=41, wins=19, draws=2, losses=20)]
    (text,) = render.render_history("alice_example", "lichess", rows, 100, {"2026-09": (22, 71.4), "2026-08": (30, 66.0)}, current="2026-09")
    lines = text.split("```")[1].strip("\n").split("\n")
    assert text.startswith("**`alice_example` · Lichess · month by month**")
    assert lines[0].split() == ["Month", "Gm", "W-D-L", "Rating", "Net", "Acc", "100GOB"]
    assert lines[1].split() == ["Sep", "2026*", "112", "61-5-46", "1412→1497", "+85", "71%", "✅"]
    assert lines[2].split() == ["Aug", "2026", "98", "50-4-44", "1390→1412", "+22", "66%"]
    assert lines[3].split() == ["Jul", "2026", "41", "19-2-20", "1401→1390", "-11", "-"]
    assert text.rstrip().endswith("* still open: the month so far")


def test_the_challenge_column_shows_a_tick_progress_or_nothing():
    rows = [hist(month="2026-09", games=120, in_100gob=1), hist(month="2026-08", games=98, in_100gob=1), hist(month="2026-07", games=98, in_100gob=0)]
    (text,) = render.render_history("a", "lichess", rows, 100)
    lines = text.split("```")[1].strip("\n").split("\n")
    assert lines[1].split()[-1] == "✅" and lines[2].split()[-1] == "98/100"
    assert lines[3].split()[-1] == "-" and "/100" not in lines[3] and "✅" not in lines[3]        # the last "-" is the accuracy column


def test_the_challenge_counts_as_done_at_exactly_the_target():
    (text,) = render.render_history("a", "lichess", [hist(games=100, in_100gob=1), hist(month="2026-07", games=99, in_100gob=1)], 100)
    lines = text.split("```")[1].strip("\n").split("\n")
    assert lines[1].split()[-1] == "✅" and lines[2].split()[-1] == "99/100"


def test_accuracy_is_rounded_to_the_nearest_whole_percent():
    rows = [hist(month="2026-09"), hist(month="2026-08"), hist(month="2026-07")]
    (text,) = render.render_history("a", "lichess", rows, 100, {"2026-09": (5, 71.6), "2026-08": (5, 71.4), "2026-07": (5, 71.5)})
    lines = text.split("```")[1].strip("\n").split("\n")
    assert [line.split()[-1] for line in lines[1:]] == ["72%", "71%", "72%"]


def test_a_table_without_analysis_has_dashes_and_no_open_note_for_finished_months():
    (text,) = render.render_history("a", "chess.com", [hist()], 100, None, current="2026-09")
    assert "still open" not in text and text.split("```")[1].strip("\n").split("\n")[1].split()[-1] == "-"
    assert "Chess.com" in text


def test_a_player_with_no_months_gets_a_plain_line():
    assert render.render_history("a", "lichess", [], 100) == ["`a` · Lichess · month by month\nNo months held yet."]


def test_a_long_history_is_split_into_messages_that_fit_and_keeps_every_month():
    months = [f"{2020 + n // 12}-{n % 12 + 1:02d}" for n in range(60)][::-1]
    messages = render.render_history("a", "lichess", [hist(month=m) for m in months], 100)
    assert len(messages) > 1 and all(len(m) <= render.MAX_MESSAGE + 200 for m in messages)
    assert sum(m.count("→") for m in messages) == 60


def test_the_columns_line_up_whatever_the_widths():
    rows = [hist(month="2026-09", games=1234, start_rating=999, end_rating=2801), hist(games=5)]
    (text,) = render.render_history("a", "lichess", rows, 100)
    lines = text.split("```")[1].strip("\n").split("\n")
    assert len({line.index("→") for line in lines[1:]}) == 1                      # the arrows sit in one column
    assert len({len(line.split("  ")[0]) for line in lines[1:]}) == 1             # and so does the game count that follows the month


# --- render_myhistory: the analysis-side table, for !myhistory --------------------------------------------------------------------------

def myrow(**over):
    base = {"month": "2026-08", "games": 98, "analysed": 90, "wins": 50, "draws": 4, "losses": 44, "rating_start": 1390, "rating_end": 1412, "avg_accuracy": 71.4}
    base.update(over)
    return base


def test_myhistory_has_an_analysed_column_and_no_100gob():
    rows = [myrow(month="2026-09", games=112, analysed=100, wins=61, draws=5, losses=46, rating_start=1412, rating_end=1497, avg_accuracy=71.6),
            myrow()]
    (text,) = render.render_myhistory("alice_example", "lichess", rows, current="2026-09")
    lines = text.split("```")[1].strip("\n").split("\n")
    assert text.startswith("**`alice_example` · Lichess · analysis history**")
    assert lines[0].split() == ["Month", "Gm", "An", "W-D-L", "Rating", "Net", "Acc"]
    assert lines[1].split() == ["Sep", "2026*", "112", "100", "61-5-46", "1412→1497", "+85", "72%"]
    assert lines[2].split() == ["Aug", "2026", "98", "90", "50-4-44", "1390→1412", "+22", "71%"]
    assert "100GOB" not in text and text.rstrip().endswith("* still open: the month so far")


def test_myhistory_shows_a_dash_where_theres_nothing_to_compute():
    (text,) = render.render_myhistory("a", "lichess", [myrow(rating_start=None, rating_end=None, avg_accuracy=None)])
    line = text.split("```")[1].strip("\n").split("\n")[1]
    assert line.split()[-3:] == ["-", "-", "-"]                                    # Rating, Net, Acc


def test_myhistory_for_no_months_gets_a_plain_line():
    assert render.render_myhistory("a", "lichess", []) == ["`a` · Lichess · analysis history\nNo months held yet."]


def test_a_long_myhistory_is_split_into_messages_that_fit_and_keeps_every_month():
    months = [f"{2020 + n // 12}-{n % 12 + 1:02d}" for n in range(60)][::-1]
    messages = render.render_myhistory("a", "lichess", [myrow(month=m) for m in months])
    assert len(messages) > 1 and all(len(m) <= render.MAX_MESSAGE + 200 for m in messages)
    assert sum(m.count("→") for m in messages) == 60


# --- the commands ----------------------------------------------------------------------------------------------------------------

class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def make_ctx(author_id, dm=False):
    return SimpleNamespace(author=SimpleNamespace(id=author_id), channel=SimpleNamespace(id=DM_CHANNEL if dm else CHANNEL),
                           guild=None if dm else SimpleNamespace(id=1), message=SimpleNamespace(add_reaction=AsyncMock()),
                           send=AsyncMock(), typing=lambda: Typing(), command=MagicMock())


def run(command, ctx, *args):
    asyncio.run(command.callback(ctx, *args))


def said(ctx):
    return [c.args[0] for c in ctx.send.await_args_list]


def reactions(ctx):
    return [c.args[0] for c in ctx.message.add_reaction.await_args_list]


def two_months():
    add_month("chess.com", "Alice", "2026-08", 1400, 1430, games=30, wins=16, draws=2, losses=12)
    add_month("chess.com", "Alice", "2026-09", 1430, 1450, games=12, wins=7, draws=1, losses=4)
    add_month("chess.com", "Bobby", "2026-09", 1500, 1510, games=8, owner=BOB)
    with store.transaction() as conn:
        conn.execute("UPDATE monthly_results SET closed_at = '2026-09-01T00:00:00+00:00' WHERE month = '2026-08'")


# !results

def test_results_for_a_past_month_is_its_final_table_with_only_the_players_who_were_in_it():
    two_months()
    ctx = make_ctx(ALICE)
    run(botmod.results, ctx, "august")
    text = "\n".join(said(ctx))
    assert text.startswith("**August 2026 final**") and "Alice" in text and "Bobby" not in text and "Final results" in text
    assert "sign-ups" not in text and "1430" in text


@pytest.mark.parametrize("word", ["last", "2026-08", "Aug", "prev"])
def test_results_understands_the_usual_ways_of_naming_a_month(word):
    two_months()
    ctx = make_ctx(ALICE)
    run(botmod.results, ctx, word)
    assert "August 2026 final" in said(ctx)[0]


def test_results_with_no_month_is_still_the_current_month_so_far():
    two_months()
    ctx = make_ctx(ALICE)
    run(botmod.results, ctx)
    text = said(ctx)[0]
    assert text.startswith("**September 2026 so far**") and "Alice" in text and "Bobby" in text


def test_results_for_a_month_before_the_bot_started_says_when_it_did():
    two_months()
    ctx = make_ctx(ALICE)
    run(botmod.results, ctx, "2026-06")
    assert said(ctx) == ["There are no results held for June 2026. The bot started counting in August 2026: a player's history begins in "
                         "the month they register, and earlier months aren't filled in."]
    assert reactions(ctx) == []


def test_results_before_anything_is_recorded():
    ctx = make_ctx(ALICE)
    run(botmod.results, ctx, "last")
    assert said(ctx) == ["Nothing has been recorded yet."]


def test_results_for_a_month_that_is_not_a_month_or_has_not_happened_is_refused_with_a_hint():
    ctx = make_ctx(ALICE)
    run(botmod.results, ctx, "fortnight")
    assert reactions(ctx) == [NO] and "I don't know the month 'fortnight'" in said(ctx)[0] and "`august`" in said(ctx)[0]
    ctx = make_ctx(ALICE)
    run(botmod.results, ctx, "2026-11")
    assert reactions(ctx) == [NO] and said(ctx) == ["November 2026 hasn't happened yet"]


def test_a_long_month_word_is_cut_short_in_the_reply():
    ctx = make_ctx(ALICE)
    run(botmod.results, ctx, "x" * 500)
    assert len(said(ctx)[0]) < 200


# !history - a DM command: registered members who are on the server, for any registered player (not just their own account)

def dm(author_id=ALICE):
    return make_ctx(author_id, dm=True)


def test_history_shows_the_callers_own_months_newest_first():
    two_months()
    ctx = dm()
    run(botmod.history, ctx)
    (text,) = said(ctx)
    lines = text.split("```")[1].strip("\n").split("\n")
    assert "`Alice` · Chess.com · month by month" in text
    assert lines[1].split()[:2] == ["Sep", "2026*"] and lines[2].split()[:2] == ["Aug", "2026"]
    assert "* still open" in text


def test_history_for_another_player_by_name_and_with_accuracy_from_analysed_games():
    two_months()
    store.add_player("lichess", "carol_example", BOB, "2026-09", 1500)
    from analysis_helpers import side
    analysed(spec(1, "carol_example", "x_example", month="2026-09"), white=side(accuracy=82.0))
    ctx = dm()
    run(botmod.history, ctx, "carol_example")
    assert "82%" in said(ctx)[0] and "carol_example" in said(ctx)[0]


def test_history_uses_the_same_account_picking_as_the_other_commands():
    ctx = dm()
    run(botmod.history, ctx)
    assert reactions(ctx) == [NO] and "haven't added an account" in said(ctx)[0]
    two_months()
    store.add_player("lichess", "alice_li", ALICE, "2026-09", 1500)
    ctx = dm()
    run(botmod.history, ctx)
    assert "you have 2 accounts: Alice (chess.com), alice_li (lichess)" in said(ctx)[0] and "!history" in said(ctx)[0]


def test_history_still_shows_the_months_if_the_accuracy_column_cannot_be_read(monkeypatch):
    def broken(site, username):
        raise RuntimeError("analysis table unreadable")
    monkeypatch.setattr(botmod.analysis_reports, "monthly_accuracy", broken)
    two_months()
    ctx = dm()
    run(botmod.history, ctx)
    text = said(ctx)[0]
    assert "Aug" in text and "Sep" in text and text.split("```")[1].strip("\n").split("\n")[1].split()[-1] == "-"


def test_history_for_a_registered_player_with_no_month_rows_says_nothing_is_recorded():
    store.add_player("chess.com", "Alice", ALICE, "2026-09", 1500)
    with store.transaction() as conn:
        conn.execute("DELETE FROM monthly_results")                          # not something the bot does, but the command must cope
    ctx = dm()
    run(botmod.history, ctx)
    assert said(ctx) == ["Nothing has been recorded yet."]


def test_history_makes_no_calls_to_the_chess_sites(monkeypatch):
    async def forbidden(*a, **k):
        raise AssertionError("a site was called")
    monkeypatch.setattr(sources, "month_games", forbidden)
    two_months()
    run(botmod.history, dm())


def test_history_in_the_channel_only_points_to_a_dm():
    ctx = make_ctx(ALICE, dm=False)
    run(botmod.history, ctx)
    ctx.send.assert_awaited_once_with(botmod.HISTORY_HINT, delete_after=botmod.TEXT_STAYS_SECONDS)
    assert "direct message" in botmod.HISTORY_HINT and reactions(ctx) == []


def test_history_refuses_someone_who_is_not_on_the_server(on_the_server):
    two_months()
    on_the_server.guild = the_server(BOB)                                   # alice is not on this server
    ctx = dm()
    run(botmod.history, ctx)
    assert reactions(ctx) == [NO] and said(ctx) == ["This is only for members of the server."]


def test_history_when_membership_cannot_be_checked_is_refused_too(monkeypatch):
    monkeypatch.setattr(botmod, "ALLOWED_CHANNEL_IDS", set())               # no home guild to check against
    ctx = dm()
    run(botmod.history, ctx)
    assert reactions(ctx) == [NO] and "couldn't check" in said(ctx)[0]


def test_history_is_in_dm_commands_and_the_checks_let_a_dm_run_it():
    assert "history" in botmod.DM_COMMANDS

    async def passes():
        ctx = SimpleNamespace(guild=None, channel=SimpleNamespace(id=DM_CHANNEL), command=SimpleNamespace(name="history"))
        return all([await discord.utils.maybe_coroutine(check, ctx) for check in botmod.bot._checks])
    assert asyncio.run(passes()) is True


# !myhistory - a DM command: the caller's own accounts only, read from game_analysis, not monthly_results

def myhistory(ctx, *args):
    asyncio.run(botmod.myhistory.callback(ctx, *args))


def test_myhistory_shows_the_callers_own_analysis_history():
    store.add_player("lichess", "alice_example", ALICE, "2026-09", 1500)
    analysed(spec(1, "alice_example", "x_example", result="white"), spec(2, "y_example", "alice_example", result="black"))
    ctx = dm()
    myhistory(ctx)
    (text,) = said(ctx)
    lines = text.split("```")[1].strip("\n").split("\n")
    assert "`alice_example` · Lichess · analysis history" in text
    assert lines[0].split() == ["Month", "Gm", "An", "W-D-L", "Rating", "Net", "Acc"]
    assert lines[1].split()[:3] == ["Sep", "2026*", "2"]


def test_myhistory_can_show_a_month_history_cannot_see():
    store.add_player("lichess", "alice_example", ALICE, "2026-09", 1500)                # registered this month...
    analysed(spec(1, "alice_example", "x_example", month="2025-11", ended_at=1_700_000_000))  # ...but backfilled November
    assert "2025-11" not in [r["month"] for r in store.player_history("lichess", "alice_example")]  # !history can't see it
    ctx = dm()
    myhistory(ctx)
    assert "Nov 2025" in said(ctx)[0]                                                   # !myhistory: sees it, from game_analysis


def test_myhistory_refuses_someone_with_no_account():
    ctx = dm()
    myhistory(ctx)
    assert reactions(ctx) == [NO] and "haven't added an account" in said(ctx)[0]


def test_myhistory_shows_every_account_when_no_site_is_given():
    store.add_player("lichess", "alice_li", ALICE, "2026-09", 1500)
    store.add_player("chess.com", "alice_cc", ALICE, "2026-09", 1500)
    analysed(spec(1, "alice_li", "x_example"))
    analysed(spec(2, "alice_cc", "y_example", site="chess.com"))
    ctx = dm()
    myhistory(ctx)
    texts = said(ctx)
    assert len(texts) == 2 and "alice_cc" in texts[0] and "Chess.com" in texts[0]        # alphabetical, like store.accounts_of
    assert "alice_li" in texts[1] and "Lichess" in texts[1]


def test_myhistory_takes_a_site_and_shows_only_that_account():
    store.add_player("lichess", "alice_li", ALICE, "2026-09", 1500)
    store.add_player("chess.com", "alice_cc", ALICE, "2026-09", 1500)
    analysed(spec(1, "alice_li", "x_example"))
    analysed(spec(2, "alice_cc", "y_example", site="chess.com"))
    ctx = dm()
    myhistory(ctx, "lichess")
    (text,) = said(ctx)
    assert "alice_li" in text and "alice_cc" not in text


def test_myhistory_refuses_an_unknown_site():
    store.add_player("lichess", "alice_example", ALICE, "2026-09", 1500)
    ctx = dm()
    myhistory(ctx, "fics")
    assert reactions(ctx) == [NO] and "the site must be" in said(ctx)[0]


def test_myhistory_refuses_a_site_the_caller_does_not_have():
    store.add_player("lichess", "alice_example", ALICE, "2026-09", 1500)
    ctx = dm()
    myhistory(ctx, "chess.com")
    assert reactions(ctx) == [NO] and "you don't have a chess.com account registered - you have alice_example (lichess)" in said(ctx)[0]


def test_myhistory_in_the_channel_only_points_to_a_dm():
    ctx = make_ctx(ALICE, dm=False)
    myhistory(ctx)
    ctx.send.assert_awaited_once_with(botmod.MYHISTORY_HINT, delete_after=botmod.TEXT_STAYS_SECONDS)
    assert "direct message" in botmod.MYHISTORY_HINT and reactions(ctx) == []


def test_myhistory_refuses_someone_who_is_not_on_the_server(on_the_server):
    store.add_player("lichess", "alice_example", ALICE, "2026-09", 1500)
    on_the_server.guild = the_server(BOB)                                   # alice is not on this server
    ctx = dm()
    myhistory(ctx)
    assert reactions(ctx) == [NO] and said(ctx) == ["This is only for members of the server."]


def test_myhistory_is_in_dm_commands_and_the_checks_let_a_dm_run_it():
    assert "myhistory" in botmod.DM_COMMANDS

    async def passes():
        ctx = SimpleNamespace(guild=None, channel=SimpleNamespace(id=DM_CHANNEL), command=SimpleNamespace(name="myhistory"))
        return all([await discord.utils.maybe_coroutine(check, ctx) for check in botmod.bot._checks])
    assert asyncio.run(passes()) is True


# !mystats and !mystatsfull for a month

@pytest.fixture
def played(monkeypatch):
    state = {"calls": [], "games": [game("W", when=at(2, 10, month=8), rating_after=1410, opening="London-System", opponent="rival_a"),
                                    game("L", when=at(3, 11, month=8), rating_after=1400, opening="London-System", opponent="rival_b")]}

    async def fake(session, site_name, username, month):
        state["calls"].append((site_name, username, month))
        return list(state["games"])
    monkeypatch.setattr(botmod.gamecache, "month_games", fake)
    return state


def test_mystats_last_shows_the_previous_month_for_the_callers_account_and_is_not_so_far(played):
    two_months()
    ctx = make_ctx(ALICE)
    run(botmod.mystats, ctx, "last")
    text = "\n".join(said(ctx))
    assert "`Alice` · Chess.com · August 2026" in text and "so far" not in text
    assert played["calls"] == [("chess.com", "Alice", "2026-08")]


def test_the_current_month_is_still_so_far(played):
    two_months()
    ctx = make_ctx(ALICE)
    run(botmod.mystats, ctx)
    assert "September 2026 so far" in "\n".join(said(ctx)) and played["calls"] == [("chess.com", "Alice", "2026-09")]


@pytest.mark.parametrize("args", [("Alice", "chess.com", "august"), ("Alice", "august"), ("august",), ("Alice", "2026-08"), ("Alice", "chess.com", "last")])
def test_the_month_can_come_last_in_place_of_the_site_or_in_place_of_the_name(played, args):
    two_months()
    run(botmod.mystats, make_ctx(ALICE), *args)
    assert played["calls"] == [("chess.com", "Alice", "2026-08")]


def test_a_registered_player_whose_name_is_a_month_is_still_found_by_name(played):
    store.add_player("chess.com", "august", ALICE, "2026-09", 1500)
    ctx = make_ctx(BOB)
    run(botmod.mystats, ctx, "august")
    assert played["calls"] == [("chess.com", "august", "2026-09")]                # the name, current month


def test_a_month_with_no_row_for_that_player_says_when_their_history_starts_and_makes_no_site_call(played):
    two_months()
    ctx = make_ctx(ALICE)
    run(botmod.mystats, ctx, "Alice", "chess.com", "2026-06")
    assert reactions(ctx) == [NO]
    assert said(ctx) == ["Alice has no results held for June 2026. Their history starts in August 2026, the month they registered: "
                         "earlier months aren't filled in."]
    assert played["calls"] == []
    ctx.command.reset_cooldown.assert_called_once_with(ctx)


def test_a_month_that_is_not_a_month_or_is_in_the_future_is_refused(played):
    two_months()
    ctx = make_ctx(ALICE)
    run(botmod.mystats, ctx, "Alice", "chess.com", "someday")
    assert reactions(ctx) == [NO] and "I don't know the month 'someday'" in said(ctx)[0]
    ctx.command.reset_cooldown.assert_called_once_with(ctx)                      # a refused command doesn't use up the cooldown
    ctx = make_ctx(ALICE)
    run(botmod.mystats, ctx, "Alice", "chess.com", "2027-01")
    assert said(ctx) == ["January 2027 hasn't happened yet"] and played["calls"] == []
    ctx.command.reset_cooldown.assert_called_once_with(ctx)


def test_mystatsfull_takes_a_month_too_and_is_not_so_far_for_a_past_one(played):
    two_months()
    ctx = make_ctx(ALICE)
    run(botmod.mystatsfull, ctx, "Alice", "chess.com", "august")
    text = "\n".join(said(ctx))
    assert "August 2026 · full" in text and "so far" not in text and played["calls"] == [("chess.com", "Alice", "2026-08")]


def test_mystatsfull_for_the_current_month_is_still_so_far(played):
    two_months()
    ctx = make_ctx(ALICE)
    run(botmod.mystatsfull, ctx, "Alice")
    assert "September 2026 so far" in "\n".join(said(ctx)) and played["calls"] == [("chess.com", "Alice", "2026-09")]


def test_a_past_month_with_no_games_says_that_month_not_this_month(played):
    two_months()
    played["games"] = []
    ctx = make_ctx(ALICE)
    run(botmod.mystats, ctx, "last")
    assert "No rated blitz games that month." in "\n".join(said(ctx)) and "yet this month" not in "\n".join(said(ctx))
    ctx = make_ctx(ALICE)
    run(botmod.mystats, ctx)
    assert "No rated blitz games yet this month." in "\n".join(said(ctx))


def test_the_analysis_part_of_a_past_month_is_that_months(played):
    two_months()
    from analysis_helpers import side
    analysed(spec(1, "Alice", "x_example", site="chess.com", month="2026-08"), spec(2, "Alice", "y_example", site="chess.com", month="2026-09"),
             white=side(accuracy=90.0))
    ctx = make_ctx(ALICE)
    run(botmod.mystats, ctx, "last")
    assert "Analysed 1 of 1 games" in "\n".join(said(ctx)) and "Accuracy 90%" in "\n".join(said(ctx))


# the help

def test_the_help_explains_that_registration_counts_only_this_month_and_lists_the_history_commands():
    ctx = make_ctx(ALICE)
    run(botmod.help_blitz_bot, ctx)
    text = said(ctx)[0]
    assert "It counts this month's games so far; earlier months aren't counted" in text
    assert "`!results [month]`" in text and "`!history [username]`" in text and "`!mystats [username] [month]`" in text
    assert len(text) < 2000

""""!export and /export: a member's own games as CSV files, one file per account, in a direct message or by slash command."""

import asyncio
import csv
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from analysis_helpers import MONTH, NOW, OWNER, analysed, register, side, spec

import analysis_queue as q
import bot as botmod
import export_data
import store
from openings import opening_family

OK, NO = "✅", "❌"
ALICE, BOB = OWNER, 1002
CHANNEL, DM_CHANNEL = 555, 777
EXTRA = {"white_rating": 1500, "black_rating": 1520, "white_rating_change": 8, "black_rating_change": -8}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(botmod, "ALLOWED_CHANNEL_IDS", {CHANNEL})
    monkeypatch.setattr(botmod.sources, "current_month", lambda now=None: MONTH)
    monkeypatch.setattr(botmod.time, "time", lambda: NOW)
    botmod._exports.clear()


def rows_of(data):
    """The lines of a CSV file's bytes as lists of cells."""
    return list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))


def period(kind="month", label=MONTH, **kw):
    return export_data.Period(kind, label, month=kw.get("month", MONTH if kind == "month" else None), since=kw.get("since"))


# --- reading the period ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, kind, label", [
    (None, "month", "2026-09"), ("", "month", "2026-09"), ("  ", "month", "2026-09"), ("this", "month", "2026-09"), ("Current", "month", "2026-09"),
    ("last", "month", "2026-08"), ("august", "month", "2026-08"), ("2026-07", "month", "2026-07"), ("2025/12", "month", "2025-12"),
    ("week", "week", "last-7-days"), ("WEEK", "week", "last-7-days"), ("7days", "week", "last-7-days"), ("7d", "week", "last-7-days"),
    ("all", "all", "all"), ("Everything", "all", "all"),
])
def test_what_people_type_is_read_as_a_period(text, kind, label):
    got = export_data.parse_period(text, "2026-09", NOW)
    assert (got.kind, got.label) == (kind, label)


def test_a_week_is_the_seven_days_before_now_and_a_month_carries_its_month():
    assert export_data.parse_period("week", "2026-09", NOW).since == NOW - 7 * 24 * 3600
    assert export_data.parse_period("last", "2026-09", NOW).month == "2026-08" and export_data.parse_period("all", "2026-09", NOW).month is None


@pytest.mark.parametrize("text", ["fortnight", "2026-13", "yesterday", "monday", "!", 5])
def test_anything_else_is_not_a_period(text):
    assert export_data.parse_period(text, "2026-09", NOW) is None


def test_periods_are_described_in_words():
    assert export_data.describe(period()) == "September 2026"
    assert export_data.describe(period("week", "last-7-days", since=1)) == "the last 7 days"
    assert export_data.describe(period("all", "all")) == "all the games held"


def test_file_names_are_safe_and_say_what_they_hold():
    assert export_data.file_name("lichess", "Pawn_Storm", period()) == "lichess_Pawn_Storm_2026-09.csv"
    assert export_data.file_name("chess.com", "a b/c?", period("all", "all"), "summary") == "chess.com_a_b_c__summary_all.csv"


# --- finding the games -----------------------------------------------------------------------------------------------------------------

def test_the_games_of_an_account_in_a_period_come_oldest_first_analysed_or_not():
    register("alice_example")
    register("bob_example")
    analysed(spec(1, "alice_example", "x_example", ended_at=NOW - 500), spec(2, "y_example", "bob_example", ended_at=NOW - 400))
    q.queue_games([spec(3, "y_example", "ALICE_example", ended_at=NOW - 300), spec(4, "alice_example", "z_example", month="2026-08", ended_at=NOW - 10 ** 7)], NOW)
    got = export_data.games_for("lichess", "alice_example", period())
    assert [g["game_id"] for g in got] == ["00000001", "00000003"] and [g["status"] for g in got] == [q.DONE, q.PENDING]     # 2 is Bob's, 4 is last month's
    assert [g["game_id"] for g in export_data.games_for("lichess", "alice_example", period("all", "all"))] == ["00000004", "00000001", "00000003"]
    assert [g["game_id"] for g in export_data.games_for("lichess", "alice_example", period("week", "w", since=NOW - 450))] == ["00000003"]


def test_a_week_holds_only_the_games_ended_within_it():
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example", ended_at=NOW - 8 * 24 * 3600), spec(2, "alice_example", "y_example", ended_at=NOW - 6 * 24 * 3600),
                   spec(3, "alice_example", "z_example", ended_at=NOW - 10)], NOW)
    got = export_data.games_for("lichess", "alice_example", export_data.parse_period("week", MONTH, NOW))
    assert [g["game_id"] for g in got] == ["00000002", "00000003"]


def test_a_game_ended_exactly_at_the_start_of_the_week_is_in_it():
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example", ended_at=NOW - 7 * 24 * 3600), spec(2, "alice_example", "y_example", ended_at=NOW - 7 * 24 * 3600 - 1)], NOW)
    assert [g["game_id"] for g in export_data.games_for("lichess", "alice_example", export_data.parse_period("week", MONTH, NOW))] == ["00000001"]


def test_the_same_name_on_the_other_site_is_a_different_account():
    register("alice_example")
    register("alice_example", site="chess.com")
    analysed(spec(1, "alice_example", "x_example"), spec(2, "alice_example", "y_example", site="chess.com"))
    assert [g["game_id"] for g in export_data.games_for("chess.com", "alice_example", period())] == ["live/2"]
    assert export_data.games_for("chess.com", "nobody", period()) == []


# --- one game's line ---------------------------------------------------------------------------------------------------------------------

def one_game(**extra):
    register("alice_example")
    analysed(spec(1, "alice_example", "rival_example", opening_site="London System", eco_site="D02", ending="timeout", ended_at=NOW - 60, **EXTRA, **extra))
    return export_data.games_for("lichess", "alice_example", period())[0]


def cells(row, username="alice_example", site="lichess"):
    return dict(zip(export_data.GAME_HEADERS, export_data.game_cells(site, username, row)))


def test_an_analysed_game_from_whites_side_fills_every_column():
    c = cells(one_game())
    assert c == {
        "ended_utc": "2026-09-21 14:12:20", "site": "lichess", "account": "alice_example", "game_link": "https://lichess.org/00000001", "colour": "white",
        "result": "win", "ending": "timeout", "time_control": "5+5", "opening": "London System", "opening_family": opening_family("London System"), "eco": "D02",
        "my_rating": "1500", "rating_change": "8", "opponent": "rival_example", "opponent_rating": "1520", "analysis": "analysed",
        "my_accuracy": "88.4", "my_opening_accuracy": "95.2", "my_middlegame_accuracy": "80.5", "my_endgame_accuracy": "",
        "my_inaccuracies": "5", "my_mistakes": "2", "my_blunders": "1", "my_acpl": "47",
        "opponent_accuracy": "61.2", "opponent_inaccuracies": "7", "opponent_mistakes": "3", "opponent_blunders": "2", "site_accuracy": "",
        "engine_score_after_10_moves": "0.10",
        "worst_moments": "1. inaccuracy -6%; 2. inaccuracy -6%; 3. inaccuracy -6%; 4. inaccuracy -6%; 5. inaccuracy -6%; 6. mistake -12%; 7. mistake -12%; 8. blunder -25%",
    }
    assert list(c) == export_data.GAME_HEADERS


def test_from_blacks_side_the_figures_are_blacks_the_score_is_flipped_and_only_blacks_moments_are_listed():
    register("alice_example")
    analysed(spec(1, "rival_example", "alice_example", result="white", **EXTRA))
    c = cells(export_data.games_for("lichess", "alice_example", period())[0])
    assert (c["colour"], c["result"], c["my_rating"], c["rating_change"], c["opponent"], c["opponent_rating"]) == ("black", "loss", "1520", "-8", "rival_example", "1500")
    assert (c["my_accuracy"], c["my_inaccuracies"], c["my_mistakes"], c["my_blunders"], c["my_acpl"]) == ("61.2", "7", "3", "2", "90")
    assert (c["opponent_accuracy"], c["opponent_blunders"]) == ("88.4", "1") and c["engine_score_after_10_moves"] == "-0.10"
    assert c["worst_moments"].startswith("1... inaccuracy -6%; 2... inaccuracy -6%") and c["worst_moments"].endswith("11... blunder -25%; 12... blunder -25%")


def test_the_account_name_is_matched_ignoring_case_when_working_out_the_side():
    row = one_game()
    assert cells(row, username="ALICE_Example")["colour"] == "white" and cells(row, username="RIVAL_example")["colour"] == "black"


def test_a_game_being_analysed_again_still_shows_its_figures():
    row = one_game()
    with store.transaction() as conn:
        conn.execute("UPDATE game_analysis SET status = 'claimed', claimed_by = 'desk'")           # the worker has it to redo it: old figures stay
    again = export_data.games_for("lichess", "alice_example", period())[0]
    c = cells(again)
    assert c["analysis"] == "analysed" and c["my_accuracy"] == "88.4" and c["worst_moments"].startswith("1. inaccuracy")
    line = dict(zip(export_data.SUMMARY_HEADERS, export_data.summary_cells("lichess", "alice_example", period(), [again])))
    assert line["analysed"] == 1 and line["avg_accuracy"] == "88.4"


def test_a_game_with_no_opening_has_blank_opening_columns():
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "rival_example")], NOW)
    c = cells(export_data.games_for("lichess", "alice_example", period())[0])
    assert (c["opening"], c["opening_family"], c["eco"]) == ("", "", "")


def test_a_draw_and_the_sites_own_accuracy():
    register("alice_example")
    analysed(spec(1, "alice_example", "rival_example", result="draw"))
    with store.transaction() as conn:
        conn.execute("UPDATE game_analysis SET site_white_accuracy = 91.26")
    c = cells(export_data.games_for("lichess", "alice_example", period())[0])
    assert c["result"] == "draw" and c["site_accuracy"] == "91.3" and c["my_rating"] == "" and c["rating_change"] == ""


def test_a_game_not_yet_analysed_has_its_facts_but_blank_analysis_columns():
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "rival_example", opening_site="London System", **EXTRA)], NOW)
    c = cells(export_data.games_for("lichess", "alice_example", period())[0])
    assert c["analysis"] == "waiting" and c["opponent"] == "rival_example" and c["my_rating"] == "1500" and c["opening"] == "London System"
    blanks = ["my_accuracy", "my_opening_accuracy", "my_middlegame_accuracy", "my_endgame_accuracy", "my_inaccuracies", "my_mistakes", "my_blunders", "my_acpl",
              "opponent_accuracy", "opponent_inaccuracies", "opponent_mistakes", "opponent_blunders", "engine_score_after_10_moves", "worst_moments"]
    assert [c[k] for k in blanks] == [""] * len(blanks)


@pytest.mark.parametrize("status, reason, error, expected", [
    (q.CLAIMED, None, None, "waiting"), (q.SKIPPED, q.OVER_MONTHLY_LIMIT, None, "skipped: over_monthly_limit"),
    (q.SKIPPED, None, None, "skipped: no reason given"), (q.FAILED, None, "boom", "failed")])
def test_the_analysis_column_says_where_a_game_stands(status, reason, error, expected):
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "rival_example")], NOW)
    with store.transaction() as conn:
        conn.execute("UPDATE game_analysis SET status = ?, skip_reason = ?, last_error = ?", (status, reason, error))
    assert cells(export_data.games_for("lichess", "alice_example", period())[0])["analysis"] == expected


def test_text_that_a_spreadsheet_could_run_as_a_formula_is_defused():
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "=HYPERLINK(1)", ending="+cmd", opening_site="@x", eco_site="-1"), spec(2, "alice_example", "\tTab")], NOW)
    first, second = (cells(g) for g in export_data.games_for("lichess", "alice_example", period()))
    assert (first["opponent"], first["ending"], first["opening"], first["eco"]) == ("'=HYPERLINK(1)", "'+cmd", "'@x", "'-1")
    assert second["opponent"] == "'\tTab"
    assert export_data._text(None) == "" and export_data._text("plain") == "plain" and export_data._text("a=b") == "a=b" and export_data._text(12) == "12"


# --- the files ---------------------------------------------------------------------------------------------------------------------------

def test_a_games_file_is_a_header_and_a_line_per_game_that_a_spreadsheet_can_read():
    register("alice_example")
    analysed(spec(1, "alice_example", "rival, the \"great\"", opening_site="Sicilian, Najdorf", **EXTRA), spec(2, "alice_example", "Zoë_example"))
    data = export_data.games_csv("lichess", "alice_example", export_data.games_for("lichess", "alice_example", period()))
    assert data.startswith(b"\xef\xbb\xbf") and data.count(b"\r\n") == 3                       # Excel's mark, and a line ending after each of 3 lines
    table = rows_of(data)
    assert table[0] == export_data.GAME_HEADERS and len(table) == 3 and all(len(line) == len(table[0]) for line in table)
    assert table[1][table[0].index("opponent")] == 'rival, the "great"' and table[1][table[0].index("opening")] == "Sicilian, Najdorf"
    assert table[2][table[0].index("opponent")] == "Zoë_example"


def test_a_file_for_no_games_is_just_the_header():
    assert rows_of(export_data.games_csv("lichess", "alice_example", [])) == [export_data.GAME_HEADERS]


# --- the summary -------------------------------------------------------------------------------------------------------------------------------

def summary_data():
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example", result="white", ending="timeout", ended_at=NOW - 300, white_rating=1500, white_rating_change=8),
             spec(2, "y_example", "alice_example", result="white", ending="timeout", ended_at=NOW - 200, black_rating=1508, black_rating_change=-9),
             white=side(), black=side(accuracy=61.2, acc_opening=70.0, acc_middle=55.5, inaccuracies=7, mistakes=3, blunders=2, acpl=91))
    q.queue_games([spec(3, "alice_example", "z_example", result="draw", ended_at=NOW - 100, white_rating=1499)], NOW)
    return export_data.games_for("lichess", "alice_example", period())


def test_the_summary_line_counts_the_record_the_rating_and_the_averages():
    rows = summary_data()
    line = dict(zip(export_data.SUMMARY_HEADERS, export_data.summary_cells("lichess", "alice_example", period(), rows)))
    assert line == {
        "period": "2026-09", "site": "lichess", "account": "alice_example", "games": 3, "analysed": 2, "wins": 1, "draws": 1, "losses": 1, "win_percent": "33.3",
        "rating_start": "1500", "rating_end": "1499", "rating_net": "-1",
        "avg_accuracy": "74.8", "avg_opening_accuracy": "82.6", "avg_middlegame_accuracy": "68.0", "avg_endgame_accuracy": "",
        "inaccuracies_per_game": "6.00", "mistakes_per_game": "2.50", "blunders_per_game": "1.50", "avg_acpl": "69", "lost_on_time": 1}


def test_a_summary_file_is_one_header_and_one_line():
    rows = summary_data()
    table = rows_of(export_data.summary_csv([export_data.summary_cells("lichess", "alice_example", period(), rows)]))
    assert table[0] == export_data.SUMMARY_HEADERS and len(table) == 2 and len(table[1]) == len(table[0])


def test_a_summary_of_games_none_of_which_are_analysed_leaves_the_averages_blank():
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example", result="black")], NOW)
    line = dict(zip(export_data.SUMMARY_HEADERS, export_data.summary_cells("lichess", "alice_example", period(), export_data.games_for("lichess", "alice_example", period()))))
    assert (line["games"], line["analysed"], line["wins"], line["losses"], line["win_percent"]) == (1, 0, 0, 1, "0.0")
    assert line["avg_accuracy"] == line["blunders_per_game"] == line["avg_acpl"] == line["rating_start"] == line["rating_end"] == line["rating_net"] == ""


def test_a_summary_of_no_games_has_no_percentage():
    line = dict(zip(export_data.SUMMARY_HEADERS, export_data.summary_cells("lichess", "alice_example", period(), [])))
    assert (line["games"], line["analysed"], line["win_percent"]) == (0, 0, "")


# --- monthly_summaries: the analysis side of !history, for !myhistory --------------------------------------------------------------------------

def test_monthly_summaries_is_empty_for_an_account_with_nothing_held():
    assert export_data.monthly_summaries("lichess", "nobody_example") == []


def test_monthly_summaries_groups_by_month_newest_first():
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example", month="2025-11", ended_at=NOW - 2_000_000, result="white"),
             spec(2, "alice_example", "y_example", month="2025-12", ended_at=NOW - 1_000_000, result="black"),
             spec(3, "alice_example", "z_example", month="2025-12", ended_at=NOW - 900_000, result="draw"))
    rows = export_data.monthly_summaries("lichess", "alice_example")
    assert [r["month"] for r in rows] == ["2025-12", "2025-11"]
    assert (rows[0]["games"], rows[0]["wins"], rows[0]["draws"], rows[0]["losses"]) == (2, 0, 1, 1)
    assert (rows[1]["games"], rows[1]["wins"], rows[1]["draws"], rows[1]["losses"]) == (1, 1, 0, 0)


def test_monthly_summaries_matches_the_export_summarys_own_arithmetic():
    rows = summary_data()                                                                  # 3 games in September, built above
    exported = dict(zip(export_data.SUMMARY_HEADERS, export_data.summary_cells("lichess", "alice_example", period(), rows)))
    (line,) = export_data.monthly_summaries("lichess", "alice_example")
    assert line["month"] == "2026-09"
    assert (line["games"], line["analysed"], line["wins"], line["draws"], line["losses"]) == (
        exported["games"], exported["analysed"], exported["wins"], exported["draws"], exported["losses"])
    assert (str(line["rating_start"]), str(line["rating_end"])) == (exported["rating_start"], exported["rating_end"])
    assert line["avg_accuracy"] == pytest.approx(74.8)                        # (88.4 + 61.2) / 2, no rounding to fight with


def test_monthly_summaries_only_covers_the_named_account():
    register("alice_example", "bob_example")
    analysed(spec(1, "alice_example", "x_example"), spec(2, "bob_example", "y_example"))
    (line,) = export_data.monthly_summaries("lichess", "alice_example")
    assert line["games"] == 1
    assert export_data.monthly_summaries("chess.com", "alice_example") == []               # not this site


def test_monthly_summaries_leaves_rating_and_accuracy_none_with_nothing_to_compute():
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example", white_rating=None)], NOW)         # queued, not analysed, and no rating
    (line,) = export_data.monthly_summaries("lichess", "alice_example")
    assert (line["games"], line["analysed"]) == (1, 0)
    assert (line["rating_start"], line["rating_end"], line["avg_accuracy"]) == (None, None, None)


# --- what the commands do -----------------------------------------------------------------------------------------------------------

def the_server(*member_ids, error=None, guild_id=1):
    async def fetch_member(user_id):
        if user_id in member_ids:
            return SimpleNamespace(id=user_id)
        raise error or discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Member")
    return SimpleNamespace(id=guild_id, fetch_member=AsyncMock(side_effect=fetch_member))


@pytest.fixture(autouse=True)
def on_the_server(monkeypatch):
    channel = SimpleNamespace(id=CHANNEL, guild=the_server(ALICE, BOB), send=AsyncMock())
    monkeypatch.setattr(botmod.bot, "get_channel", lambda channel_id: channel if channel_id == CHANNEL else None)
    return channel


@pytest.fixture
def sent(monkeypatch):
    """What was sent by DM: a list of (user id, parts)."""
    log = []

    async def fake(user_id, parts):
        log.append((user_id, parts))
    monkeypatch.setattr(botmod, "_dm_parts", fake)
    return log


def flow(what="games", text=None, user=ALICE):
    return asyncio.run(botmod._export_flow(user, what, text))


def two_accounts():
    register("alice_example")
    register("alice_cc", site="chess.com")
    analysed(spec(1, "alice_example", "x_example", ended_at=NOW - 300, **EXTRA), spec(2, "y_example", "alice_cc", site="chess.com", ended_at=NOW - 200))
    q.queue_games([spec(3, "alice_example", "w_example", ended_at=NOW - 100)], NOW)


def test_each_account_gets_its_own_file_and_message():
    two_accounts()
    parts, error = flow()
    assert error is None and [p["filename"] for p in parts] == ["chess.com_alice_cc_2026-09.csv", "lichess_alice_example_2026-09.csv"]
    assert parts[0]["text"] == "`alice_cc` · Chess.com · September 2026: 1 games (1 analysed)."
    assert parts[1]["text"] == "`alice_example` · Lichess · September 2026: 2 games (1 analysed)."
    chess_com, lichess = (rows_of(p["data"]) for p in parts)
    assert [line[chess_com[0].index("game_link")] for line in chess_com[1:]] == ["https://www.chess.com/game/live/2"]
    assert [line[lichess[0].index("game_link")] for line in lichess[1:]] == ["https://lichess.org/00000001", "https://lichess.org/00000003"]
    assert {line[lichess[0].index("account")] for line in lichess[1:]} == {"alice_example"}                     # nothing of the other account


def test_an_account_with_no_games_in_the_period_gets_a_line_and_no_file():
    two_accounts()
    parts, error = flow(text="2026-08")
    assert error is None and all("data" not in p for p in parts)
    assert parts[1]["text"] == "`alice_example` · Lichess · August 2026: no games held."
    assert botmod._exports == {}                                                                          # nothing was sent, so no wait for the next


def test_a_summary_is_one_small_file_per_account():
    two_accounts()
    parts, error = flow("summary")
    assert error is None and [p["filename"] for p in parts] == ["chess.com_alice_cc_summary_2026-09.csv", "lichess_alice_example_summary_2026-09.csv"]
    assert parts[1]["text"] == "`alice_example` · Lichess · September 2026: 2 games, summarised on one line."
    table = rows_of(parts[1]["data"])
    assert table[0] == export_data.SUMMARY_HEADERS and table[1][table[0].index("games")] == "2"


def test_the_period_can_be_a_week_last_month_or_everything():
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example", month="2026-08", ended_at=NOW - 20 * 24 * 3600), spec(2, "alice_example", "y_example", ended_at=NOW - 60)], NOW)
    def links(text):
        botmod._exports.clear()
        return [line[3] for p in flow(text=text)[0] for line in rows_of(p["data"])[1:]]
    assert links("week") == ["https://lichess.org/00000002"]
    assert links("last") == ["https://lichess.org/00000001"]
    assert links("all") == ["https://lichess.org/00000001", "https://lichess.org/00000002"]


def test_the_analysed_count_includes_a_game_being_analysed_again():
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example"))
    q.queue_games([spec(2, "alice_example", "y_example")], NOW)
    with store.transaction() as conn:
        conn.execute("UPDATE game_analysis SET status = 'claimed' WHERE game_id = '00000001'")     # being redone: old figures stay
    assert flow()[0][0]["text"] == "`alice_example` · Lichess · September 2026: 2 games (1 analysed)."


def test_a_month_count_higher_than_the_file_is_explained():
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example")], NOW)
    with store.transaction() as conn:
        conn.execute("UPDATE monthly_results SET games = 40 WHERE username = 'alice_example'")
    text = flow()[0][0]["text"]
    assert text.endswith("The bot counted 40 games that month; the file holds 1, because games played before analysis was switched on aren't in it.")
    botmod._exports.clear()
    assert "counted" not in flow("summary")[0][0]["text"]
    botmod._exports.clear()
    assert "counted" not in flow(text="all")[0][0]["text"]
    with store.transaction() as conn:
        conn.execute("UPDATE monthly_results SET games = 1 WHERE username = 'alice_example'")
    botmod._exports.clear()
    assert "counted" not in flow()[0][0]["text"]


def test_a_file_too_big_to_send_is_replaced_by_a_line_saying_so(monkeypatch):
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example")], NOW)
    monkeypatch.setattr(export_data, "MAX_BYTES", 50)
    parts, error = flow()
    assert error is None and parts == [{"text": "`alice_example` · Lichess · September 2026: too many games for one file: choose a shorter period."}]
    assert botmod._exports == {}


def test_the_flow_refuses_someone_with_no_account_an_unknown_period_and_a_month_that_has_not_happened():
    assert flow() == (None, "you haven't added an account yet - use `!add <username> <site>` in the server's channel first")
    register("alice_example")
    assert flow(text="fortnight") == (None, "I don't know the period 'fortnight': try `week`, `last`, a month like `2026-08`, or `all`")
    assert flow(text="2099-01") == (None, "January 2099 hasn't happened yet")
    assert len(flow(text="x" * 500)[1]) < 200


def test_one_person_exports_once_every_half_minute_and_someone_elses_wait_is_their_own(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(botmod, "_monotonic", lambda: clock[0])
    register("alice_example")
    store.add_player("lichess", "bob_example", BOB, MONTH, 1500)
    q.queue_games([spec(1, "alice_example", "x_example"), spec(2, "bob_example", "y_example")], NOW)
    assert flow()[1] is None
    assert flow() == (None, "one export at a time please: try again in a few seconds")
    assert flow(user=BOB)[1] is None
    clock[0] += botmod.EXPORT_GAP_SECONDS - 1
    assert flow()[1] is not None
    clock[0] += 1
    assert flow()[1] is None


def test_the_words_after_export_may_come_in_either_order():
    args = botmod._export_args
    assert args(None, None) == ("games", None) and args("last", None) == ("games", "last") and args("summary", None) == ("summary", None)
    assert args("summary", "week") == ("summary", "week") and args("week", "summary") == ("summary", "week")
    assert args("Summary", "ALL") == ("summary", "ALL") and args("games", "all") == ("games", "all") and args("games", None) == ("games", None)


# --- sending ------------------------------------------------------------------------------------------------------------------------------

def test_each_part_is_sent_with_its_file_and_a_delete_button(monkeypatch):
    calls = []

    class FakeUser:
        async def send(self, content, **kwargs):
            calls.append((content, kwargs))
    monkeypatch.setattr(botmod.bot, "get_user", lambda user_id: FakeUser() if user_id == ALICE else None)
    asyncio.run(botmod._dm_parts(ALICE, [{"text": "one", "filename": "a.csv", "data": b"x,y\r\n"}, {"text": "no games"}]))
    (text1, kw1), (text2, kw2) = calls
    assert text1 == "one" and kw1["file"].filename == "a.csv" and kw1["file"].fp.read() == b"x,y\r\n" and isinstance(kw1["view"], botmod.DeleteButton)
    assert text2 == "no games" and kw2["file"] is None and isinstance(kw2["view"], botmod.DeleteButton)


# --- !export in a direct message ---------------------------------------------------------------------------------------------------------------

class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def make_ctx(author_id=ALICE, dm=True):
    return SimpleNamespace(author=SimpleNamespace(id=author_id), channel=SimpleNamespace(id=DM_CHANNEL if dm else CHANNEL), guild=None if dm else SimpleNamespace(id=1),
                           message=SimpleNamespace(add_reaction=AsyncMock()), send=AsyncMock(), typing=lambda: Typing(), command=MagicMock())


def export(ctx, *args):
    asyncio.run(botmod.export_command.callback(ctx, *args))


def said(ctx):
    return [c.args[0] for c in ctx.send.await_args_list]


def reactions(ctx):
    return [c.args[0] for c in ctx.message.add_reaction.await_args_list]


def test_a_dm_gets_the_files_and_a_tick(sent):
    two_accounts()
    ctx = make_ctx()
    export(ctx)
    assert reactions(ctx) == [OK] and said(ctx) == []
    (user, parts), = sent
    assert user == ALICE and [p["filename"] for p in parts] == ["chess.com_alice_cc_2026-09.csv", "lichess_alice_example_2026-09.csv"]


def test_a_dm_can_ask_for_a_summary_of_a_period(sent):
    two_accounts()
    export(make_ctx(), "summary", "last")
    assert sent[0][1][1]["text"] == "`alice_example` · Lichess · August 2026: no games held."
    botmod._exports.clear()
    export(make_ctx(), "week", "summary")
    assert sent[1][1][1]["filename"] == "lichess_alice_example_summary_last-7-days.csv"


def test_a_dm_with_a_problem_gets_it_in_words(sent):
    ctx = make_ctx()
    export(ctx)                                                                             # not registered
    assert reactions(ctx) == [NO] and "haven't added an account" in said(ctx)[0] and sent == []
    register("alice_example")
    ctx = make_ctx()
    export(ctx, "fortnight")
    assert reactions(ctx) == [NO] and "I don't know the period 'fortnight'" in said(ctx)[0]


def test_someone_who_is_not_on_the_server_gets_nothing(sent, on_the_server):
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example")], NOW)
    on_the_server.guild = the_server(BOB)
    ctx = make_ctx()
    export(ctx)
    assert reactions(ctx) == [NO] and said(ctx) == ["This is only for members of the server."] and sent == [] and botmod._exports == {}
    on_the_server.guild = the_server(error=discord.HTTPException(SimpleNamespace(status=500, reason="oops"), "x"))
    ctx = make_ctx()
    export(ctx)
    assert reactions(ctx) == [NO] and "couldn't check" in said(ctx)[0] and sent == []


def test_in_the_channel_export_only_points_to_slash_and_dms(sent):
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example")], NOW)
    ctx = make_ctx(dm=False)
    export(ctx)
    ctx.send.assert_awaited_once_with(botmod.EXPORT_HINT, delete_after=20)
    assert "/export" in botmod.EXPORT_HINT and "direct message" in botmod.EXPORT_HINT and sent == [] and reactions(ctx) == [] and botmod._exports == {}


def test_a_failure_to_send_the_files_or_to_post_the_reply_is_survived(monkeypatch):
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example")], NOW)

    async def broken(user_id, parts):
        raise discord.HTTPException(SimpleNamespace(status=500, reason="oops"), "server error")
    monkeypatch.setattr(botmod, "_dm_parts", broken)
    ctx = make_ctx()
    export(ctx)
    assert reactions(ctx) == [NO] and said(ctx) == ["I couldn't send that just now: try again in a moment"]
    ctx = make_ctx()
    ctx.send = AsyncMock(side_effect=discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions"))
    export(ctx)
    assert reactions(ctx) == [NO]
    hint = make_ctx(dm=False)
    hint.send = AsyncMock(side_effect=discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions"))
    export(hint)


def test_the_command_checks_let_a_dm_run_export_and_obit_only():
    def passes(guild, channel_id, command):
        ctx = SimpleNamespace(guild=guild, channel=SimpleNamespace(id=channel_id), command=SimpleNamespace(name=command))

        async def run_all():
            return all([await discord.utils.maybe_coroutine(check, ctx) for check in botmod.bot._checks])
        return asyncio.run(run_all())
    assert passes(None, DM_CHANNEL, "export") is True and passes(None, DM_CHANNEL, "obit") is True and passes(None, DM_CHANNEL, "results") is False
    assert passes(None, DM_CHANNEL, "backfill") is True
    assert passes(SimpleNamespace(id=1), CHANNEL, "export") is True and passes(SimpleNamespace(id=1), CHANNEL + 1, "export") is False
    assert botmod.DM_COMMANDS == ("obit", "export", "clear", "backfill", "history", "myhistory")


# --- /export ---------------------------------------------------------------------------------------------------------------------------------------

def make_interaction(user_id=ALICE, channel_id=CHANNEL):
    return SimpleNamespace(user=SimpleNamespace(id=user_id), channel_id=channel_id,
                           response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))


def slash(interaction, *args):
    asyncio.run(botmod.export_slash.callback(interaction, *args))


def test_slash_export_sends_the_files_by_dm_and_tells_only_the_person_asking(sent):
    two_accounts()
    interaction = make_interaction()
    slash(interaction)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.followup.send.assert_awaited_once_with("I've sent you 2 files by DM.", ephemeral=True)
    interaction.response.send_message.assert_not_awaited()
    assert [u for u, _ in sent] == [ALICE]


def test_slash_export_takes_a_period_and_a_kind_and_says_one_file_in_the_singular(sent):
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example")], NOW)
    interaction = make_interaction()
    slash(interaction, "all", "summary")
    interaction.followup.send.assert_awaited_once_with("I've sent you 1 file by DM.", ephemeral=True)
    assert sent[0][1][0]["filename"] == "lichess_alice_example_summary_all.csv"


def test_slash_export_with_no_games_says_so_privately(sent):
    register("alice_example")
    interaction = make_interaction()
    slash(interaction)
    interaction.followup.send.assert_awaited_once_with("There were no games to send.", ephemeral=True)
    assert sent[0][1] == [{"text": "`alice_example` · Lichess · September 2026: no games held."}]


def test_slash_export_problems_are_private(sent):
    interaction = make_interaction()
    slash(interaction)
    assert "haven't added an account" in interaction.followup.send.await_args.args[0] and interaction.followup.send.await_args.kwargs == {"ephemeral": True}
    register("alice_example")
    interaction = make_interaction()
    slash(interaction, "fortnight")
    assert "I don't know the period" in interaction.followup.send.await_args.args[0] and interaction.followup.send.await_args.kwargs == {"ephemeral": True}
    assert sent == []


def test_slash_export_says_privately_when_dms_are_closed_or_sending_fails(monkeypatch):
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example")], NOW)

    async def refuse(user_id, parts):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Cannot send messages to this user")
    monkeypatch.setattr(botmod, "_dm_parts", refuse)
    interaction = make_interaction()
    slash(interaction)
    assert "Allow direct messages" in interaction.followup.send.await_args.args[0] and interaction.followup.send.await_args.kwargs == {"ephemeral": True}

    async def broken(user_id, parts):
        raise discord.HTTPException(SimpleNamespace(status=500, reason="oops"), "server error")
    monkeypatch.setattr(botmod, "_dm_parts", broken)
    botmod._exports.clear()
    interaction = make_interaction()
    slash(interaction)
    assert interaction.followup.send.await_args.args[0] == "I couldn't send that just now: try again in a moment"


def test_slash_export_only_works_in_the_allowed_channel(sent):
    register("alice_example")
    q.queue_games([spec(1, "alice_example", "x_example")], NOW)
    interaction = make_interaction(channel_id=CHANNEL + 1)
    slash(interaction)
    interaction.response.send_message.assert_awaited_once_with("This command only works in the blitz channel.", ephemeral=True)
    interaction.response.defer.assert_not_awaited()
    assert sent == []


def test_slash_export_is_registered_for_servers_with_optional_period_and_a_choice_of_kind():
    command = botmod.bot.tree.get_command("export")
    assert command is not None and command.guild_only is True
    by_name = {p.name: p for p in command.parameters}
    assert [(n, p.required) for n, p in by_name.items()] == [("period", False), ("what", False)]
    assert [c.value for c in by_name["what"].choices] == ["games", "summary"] and by_name["what"].default == "games"


def test_help_and_readme_describe_export():
    ctx = make_ctx(dm=False)
    asyncio.run(botmod.help_blitz_bot.callback(ctx))
    text = said(ctx)[0]
    assert "`/export [period] [what]`" in text and "Or `!export` by DM" in text and len(text) < 2000
    readme = open(botmod.__file__.replace("bot.py", "README.md"), encoding="utf-8").read()
    assert "`!export [summary] [period]`" in readme and "`/export [period] [what]`" in readme
    assert botmod.USAGE["export"] == "!export [summary] [period]"

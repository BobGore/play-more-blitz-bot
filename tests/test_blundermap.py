"""!blundermap and !blundermapfor: a member's monthly blunder map, built by blundermap.py in a separate process and sent by DM."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from analysis_helpers import MONTH, NOW, OWNER, analysed, register, spec

import blundermap_build as bm
import bot as botmod
import settings
import store

OK, NO, WAIT = "✅", "❌", "⏳"
ALICE, BOB, ADMIN = OWNER, 1002, 810486671174795274
CHANNEL, DM_CHANNEL = 555, 777
COUNTS_SCHEMA = """
CREATE TABLE game_counts (site TEXT, game_id TEXT, month TEXT, plies INTEGER, legal BLOB, good BLOB, pass2_version INTEGER,
                          engine TEXT, nodes_per_move INTEGER, computed_at INTEGER, computed_by TEXT, seconds REAL,
                          PRIMARY KEY (site, game_id));
CREATE TABLE blunder_moves (site TEXT, game_id TEXT, ply INTEGER, side TEXT, legal_moves INTEGER, good_moves INTEGER,
                            losing_moves INTEGER, fen TEXT, played_move TEXT, best_move TEXT, good_list TEXT, close_list TEXT,
                            PRIMARY KEY (site, game_id, ply));
"""
SUMMARY = {"games": 12, "analysed": 11, "skipped": 1, "used": 9, "waiting": 2, "noClock": 0, "mismatch": 0, "monthOpen": True,
           "cards": 7}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    counts = tmp_path / "blundermap.db"
    db = sqlite3.connect(counts)
    db.executescript(COUNTS_SCHEMA)
    db.close()
    monkeypatch.setattr(settings, "BLUNDERMAP_DB", str(counts))
    monkeypatch.setattr(settings, "BLUNDERMAP_USERS", {ALICE})
    monkeypatch.setattr(settings, "BLUNDERMAP_EVERYONE", False)
    monkeypatch.setattr(botmod, "ALLOWED_CHANNEL_IDS", {CHANNEL})
    monkeypatch.setattr(botmod.sources, "current_month", lambda now=None: MONTH)
    monkeypatch.setattr(botmod.time, "time", lambda: NOW)
    botmod._blundermaps.clear()
    botmod._blundermaps_building.clear()
    return counts


def the_server(*member_ids):
    async def fetch_member(user_id):
        if user_id in member_ids:
            return SimpleNamespace(id=user_id)
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Member")
    return SimpleNamespace(id=1, fetch_member=AsyncMock(side_effect=fetch_member))


@pytest.fixture(autouse=True)
def on_the_server(monkeypatch):
    channel = SimpleNamespace(id=CHANNEL, guild=the_server(ALICE, BOB, ADMIN), send=AsyncMock())
    monkeypatch.setattr(botmod.bot, "get_channel", lambda channel_id: channel if channel_id == CHANNEL else None)
    return channel


@pytest.fixture
def sent(monkeypatch):
    """What was sent by DM, as (user id, parts); and builds faked, recorded as (site, username, month)."""
    log = {"dm": [], "built": []}

    async def fake_dm(user_id, parts):
        log["dm"].append((user_id, parts))

    async def fake_build(site, username, month, **kw):
        log["built"].append((site, username, month))
        return b"<html>map</html>", dict(SUMMARY)
    monkeypatch.setattr(botmod, "_dm_parts", fake_dm)
    monkeypatch.setattr(bm, "build", fake_build)
    return log


def make_ctx(author_id=ALICE, dm=True):
    return SimpleNamespace(author=SimpleNamespace(id=author_id), channel=SimpleNamespace(id=DM_CHANNEL if dm else CHANNEL),
                           guild=None if dm else SimpleNamespace(id=1), message=SimpleNamespace(add_reaction=AsyncMock()),
                           send=AsyncMock(), command=MagicMock())


def run(command, ctx, *args):
    """Run the command, then wait for the background job it started, as the bot's loop would."""
    async def go():
        await command.callback(ctx, *args)
        while botmod._blundermap_tasks:
            await asyncio.gather(*list(botmod._blundermap_tasks))
    asyncio.run(go())


def said(ctx):
    return [c.args[0] for c in ctx.send.await_args_list]


def reactions(ctx):
    return [c.args[0] for c in ctx.message.add_reaction.await_args_list]


def alice_with_games():
    register("alice_example")
    analysed(spec(1, "alice_example", "x_example"), spec(2, "y_example", "alice_example"))


# --- the words and the file name ----------------------------------------------------------------------------------------------------

def test_the_message_says_how_much_of_the_month_is_on_the_map():
    assert bm.describe(SUMMARY, "September 2026") == (
        "September 2026 so far: 9 of your 11 analysed games are on the map, with 7 blunder cards. "
        "2 more are still being counted: ask again later to see them too.")
    done = dict(SUMMARY, monthOpen=False, used=10, waiting=0, cards=1)
    assert bm.describe(done, "August 2026", "alice_example") == (
        "August 2026: 10 of alice_example's 11 analysed games are on the map, with 1 blunder card. "
        "1 can't be placed on it (their clock times weren't kept).")


def test_file_names_are_safe():
    assert bm.file_name("chess.com", "a b/c", "2026-09") == "blundermap_chess.com_a_b_c_2026-09.html"


def test_games_held_counts_an_accounts_month():
    alice_with_games()
    assert tuple(bm.games_held("lichess", "ALICE_example", MONTH)) == (2, 2)
    assert tuple(bm.games_held("lichess", "alice_example", "2026-08")) == (0, 0)


# --- building in a separate process -------------------------------------------------------------------------------------------------

def test_the_real_generator_runs_and_reports_what_the_page_holds():
    alice_with_games()
    page, summary = asyncio.run(bm.build("lichess", "alice_example", MONTH))
    assert page.startswith(b"<!") and b"/*DATA*/null" not in page
    assert summary["games"] == 2 and summary["analysed"] == 2 and summary["waiting"] == 2 and summary["used"] == 0


def test_a_generator_failure_is_a_build_error():
    alice_with_games()
    with pytest.raises(bm.BuildError, match="no games"):
        asyncio.run(bm.build("lichess", "nobody_example", MONTH))


def test_a_generator_that_takes_too_long_is_stopped(tmp_path, monkeypatch):
    (tmp_path / "blundermap.py").write_text("import time\ntime.sleep(30)\n")  # stands in for the real one
    monkeypatch.setattr(bm, "HERE", tmp_path)
    with pytest.raises(bm.BuildError, match="longer than 1 s"):
        asyncio.run(bm.build("lichess", "x_example", MONTH, timeout=1))


# --- !blundermap -------------------------------------------------------------------------------------------------------------------

def test_a_tester_gets_an_hourglass_a_note_and_then_the_file(sent):
    alice_with_games()
    ctx = make_ctx()
    run(botmod.blundermap_command, ctx)
    assert reactions(ctx) == [WAIT] and "Making your blunder map for September 2026" in said(ctx)[0]
    (user, parts), = sent["dm"]
    assert user == ALICE and sent["built"] == [("lichess", "alice_example", MONTH)]
    assert parts[0]["filename"] == "blundermap_lichess_alice_example_2026-09.html" and parts[0]["data"] == b"<html>map</html>"
    assert parts[0]["text"].startswith("`alice_example` · Lichess · September 2026 so far: 9 of your 11")


def test_an_account_with_nothing_analysed_gets_a_line_not_a_file(sent):
    register("alice_example")
    run(botmod.blundermap_command, make_ctx(), "august")
    assert sent["built"] == [] and sent["dm"][0][1] == [{"text": "`alice_example` · Lichess: no analysed games held for August 2026, so there's no map."}]


def test_once_an_hour_per_month_except_for_admins(sent, monkeypatch):
    alice_with_games()
    clock = [1000.0]
    monkeypatch.setattr(botmod, "_monotonic", lambda: clock[0])
    run(botmod.blundermap_command, make_ctx())
    ctx = make_ctx()
    clock[0] += 600
    run(botmod.blundermap_command, ctx)
    assert reactions(ctx) == [NO] and "try again in 50 minutes" in said(ctx)[0] and len(sent["built"]) == 1
    run(botmod.blundermap_command, make_ctx(), "august")       # another month is fine
    clock[0] += 3600
    run(botmod.blundermap_command, make_ctx())
    assert len(sent["dm"]) == 3


def test_one_build_in_hand_per_person(sent):
    alice_with_games()
    botmod._blundermaps_building.add(ALICE)
    ctx = make_ctx()
    run(botmod.blundermap_command, ctx)
    assert reactions(ctx) == [NO] and "still making your last one" in said(ctx)[0] and sent["built"] == []


def test_only_testers_while_it_is_tried_out_then_everyone(sent, monkeypatch):
    store.add_player("lichess", "bob_example", BOB, MONTH, 1500)
    ctx = make_ctx(BOB)
    run(botmod.blundermap_command, ctx)
    assert reactions(ctx) == [NO] and "being tried out" in said(ctx)[0]
    monkeypatch.setattr(settings, "BLUNDERMAP_EVERYONE", True)
    ctx = make_ctx(BOB)
    run(botmod.blundermap_command, ctx)
    assert reactions(ctx) == [WAIT]


def test_switched_off_without_a_counts_file(sent, monkeypatch):
    monkeypatch.setattr(settings, "BLUNDERMAP_DB", "")
    ctx = make_ctx()
    run(botmod.blundermap_command, ctx)
    assert reactions(ctx) == [NO] and "isn't switched on" in said(ctx)[0]


def test_in_the_channel_it_only_points_to_dms(sent):
    ctx = make_ctx(dm=False)
    run(botmod.blundermap_command, ctx)
    ctx.send.assert_awaited_once_with(botmod.BLUNDERMAP_HINT, delete_after=20)
    assert sent["built"] == [] and reactions(ctx) == []


def test_not_on_the_server_or_no_account(sent, on_the_server):
    ctx = make_ctx()
    run(botmod.blundermap_command, ctx)
    assert reactions(ctx) == [NO] and "haven't added an account" in said(ctx)[0]
    on_the_server.guild = the_server(BOB)
    ctx = make_ctx()
    run(botmod.blundermap_command, ctx)
    assert said(ctx) == ["This is only for members of the server."]


def test_a_build_that_fails_is_said_in_words(sent, monkeypatch):
    alice_with_games()

    async def broken(*a, **kw):
        raise bm.BuildError("boom")
    monkeypatch.setattr(bm, "build", broken)
    run(botmod.blundermap_command, make_ctx())
    assert sent["dm"][0][1] == [{"text": "`alice_example` · Lichess: I couldn't make the map for September 2026 just now. Try again later."}]
    assert ALICE not in botmod._blundermaps_building


def test_a_future_month_is_refused(sent):
    alice_with_games()
    ctx = make_ctx()
    run(botmod.blundermap_command, ctx, "2027-01")
    assert reactions(ctx) == [NO] and "hasn't happened yet" in said(ctx)[0]


# --- !blundermapfor ----------------------------------------------------------------------------------------------------------------

def test_an_admin_gets_a_members_map_and_the_member_is_not_told(sent):
    alice_with_games()
    ctx = make_ctx(ADMIN)
    run(botmod.blundermapfor, ctx, "alice_example")
    assert reactions(ctx) == [WAIT] and said(ctx) == ["Making alice_example's blunder map for September 2026: I'll send it here."]
    (user, parts), = sent["dm"]
    assert user == ADMIN and "9 of alice_example's 11 analysed games" in parts[0]["text"]


def alice_on_both_sites():
    register("alice_example")
    register("alice_cc", site="chess.com")
    analysed(spec(1, "alice_example", "x_example"), spec(2, "y_example", "alice_cc", site="chess.com"))


def test_with_two_accounts_both_maps_come_unless_a_site_is_given(sent):
    alice_on_both_sites()
    run(botmod.blundermap_command, make_ctx())
    assert sorted(sent["built"]) == [("chess.com", "alice_cc", MONTH), ("lichess", "alice_example", MONTH)]
    for args in (("lichess",), ("LICHESS",), ("september", "lichess"), ("lichess", "2026-09")):
        sent["built"].clear()
        botmod._blundermaps.clear()
        run(botmod.blundermap_command, make_ctx(), *args)
        assert sent["built"] == [("lichess", "alice_example", MONTH)], args
    sent["built"].clear()
    run(botmod.blundermap_command, make_ctx(), "chess.com", "august")
    assert sent["built"] == [] and sent["dm"][-1][1] == [{"text": "`alice_cc` · Chess.com: no analysed games held for August 2026, so there's no map."}]


def test_a_site_with_no_account_is_refused(sent):
    alice_with_games()
    ctx = make_ctx()
    run(botmod.blundermap_command, ctx, "chess.com")
    assert reactions(ctx) == [NO] and said(ctx) == ["you have no Chess.com account on the list"] and sent["built"] == []


def test_blundermapfor_takes_the_site_and_month_in_either_order(sent):
    register("alice_example")
    register("alice_example", site="chess.com")  # the same name on both sites, so the site decides
    analysed(spec(1, "alice_example", "x_example"), spec(2, "y_example", "alice_example", site="chess.com"))
    for args in (("chess.com",), ("chess.com", "september"), ("september", "chess.com"), ("2026-09", "Chess.com")):
        sent["built"].clear()
        ctx = make_ctx(ADMIN)
        run(botmod.blundermapfor, ctx, "alice_example", *args)
        assert reactions(ctx) == [WAIT] and sent["built"] == [("chess.com", "alice_example", MONTH)], args
    ctx = make_ctx(ADMIN)
    run(botmod.blundermapfor, ctx, "alice_example", "august")  # a month alone, for a name on two sites: which one?
    assert reactions(ctx) == [NO]


def test_the_commands_are_dm_only_and_blundermapfor_is_for_admins():
    assert "blundermap" in botmod.DM_COMMANDS and "blundermapfor" in botmod.ADMIN_DM_COMMANDS
    assert "blundermap" in botmod.USAGE and "blundermapfor" in botmod.USAGE


# --- the counting queue in !analysisq -----------------------------------------------------------------------------------------------

QUEUE_SCHEMA = """
CREATE TABLE count_queue (site TEXT, game_id TEXT, month TEXT, analysed_at INTEGER, status TEXT, taken_by TEXT, taken_at INTEGER,
                          tries INTEGER, last_error TEXT, updated_at INTEGER, PRIMARY KEY (site, game_id));
CREATE TABLE runs (id INTEGER PRIMARY KEY, machine TEXT, verb TEXT, at INTEGER, seconds REAL, queued INTEGER, games INTEGER,
                   counted INTEGER, failed INTEGER, released INTEGER, refused INTEGER, note TEXT);
"""


def a_queue(counts_path):
    """A counting queue beside blundermap.db, as the counting gateway keeps it: 3 waiting, 3 being counted by two machines,
    2 counted, 1 failed; fish4 sent counts 10 minutes ago, fish2 an hour ago."""
    db = sqlite3.connect(counts_path.with_name("blundermap-queue.db"))
    db.executescript(QUEUE_SCHEMA)
    rows = [("w1", NOW - 7200, "waiting", None), ("w2", NOW - 600, "waiting", None), ("w3", NOW - 60, "waiting", None),
            ("t1", NOW - 900, "taken", "fish4"), ("t2", NOW - 900, "taken", "fish2"), ("t3", NOW - 900, "taken", "fish2"),
            ("c1", NOW - 9000, "counted", None), ("c2", NOW - 9000, "counted", None), ("f1", NOW - 9000, "failed", None)]
    db.executemany("INSERT INTO count_queue VALUES ('lichess', ?, '2026-09', ?, ?, ?, NULL, 0, NULL, 0)", rows)
    db.executemany("INSERT INTO runs (machine, verb, at, seconds, games, counted) VALUES (?, ?, ?, 1, ?, ?)",
                   [("fish2", "take_in", NOW - 3600, 20, 20), ("fish4", "take_in", NOW - 1200, 10, 9),
                    ("fish4", "take_in", NOW - 600, 10, 10), ("fish4", "hand_out", NOW - 590, 10, 0)])
    db.commit()
    db.close()


def test_the_counting_queue_is_read_and_drawn_for_the_admin(isolated):
    assert bm.counting_status(NOW) is None                                   # no queue file yet
    a_queue(isolated)
    status = bm.counting_status(NOW)
    assert status["counts"] == {"waiting": 3, "taken": 3, "counted": 2, "failed": 1}
    assert status["taken_by"] == [("fish2", 2), ("fish4", 1)]
    assert status["last"] == [("fish4", 600, 10), ("fish2", 3600, 20)] and status["oldest_waiting_seconds"] == 7200
    text = botmod.render_analysis.render_counting_status(status)
    assert text.startswith("**Blunder map counting**")
    assert "Being counted        3   (fish2 2, fish4 1)" in text and "Counter fish4: last sent counts 10 min ago (10 games)" in text
    assert "⚠" not in text


def test_counting_that_has_stalled_is_flagged(isolated):
    a_queue(isolated)
    later = bm.counting_status(NOW + 4 * 3600)
    assert "⚠ No counts have come in for 4 h 10 min." in botmod.render_analysis.render_counting_status(later)
    assert botmod.render_analysis.render_counting_status(None) == ""


def test_analysisq_adds_the_counting_queue_when_the_map_is_on(isolated, monkeypatch):
    a_queue(isolated)
    monkeypatch.setattr(botmod, "datetime", SimpleNamespace(now=lambda tz=None: SimpleNamespace(timestamp=lambda: NOW)))
    ctx = make_ctx(ADMIN)
    asyncio.run(botmod.analysisq.callback(ctx))
    text = said(ctx)[0]
    assert "**Analysis queue**" in text and "**Blunder map counting**" in text and len(text) < 2000
    monkeypatch.setattr(settings, "BLUNDERMAP_DB", "")
    ctx = make_ctx(ADMIN)
    asyncio.run(botmod.analysisq.callback(ctx))
    assert "Blunder map counting" not in said(ctx)[0]


def test_the_help_lists_blundermap_only_once_it_is_open_to_everyone(monkeypatch):
    assert "!blundermap" not in "".join(botmod.help_messages())  # testers only: the others would be refused
    monkeypatch.setattr(settings, "BLUNDERMAP_EVERYONE", True)
    channel, private = botmod.help_messages()
    assert "`!blundermap [month] [site]`" in private and "!blundermap" not in channel
    assert all(len(m) < 2000 for m in (channel, private))
    monkeypatch.setattr(settings, "BLUNDERMAP_DB", "")
    assert "!blundermap" not in "".join(botmod.help_messages())  # switched off altogether

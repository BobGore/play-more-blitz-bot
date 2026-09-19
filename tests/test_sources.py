"""Parsing tests for sources.py. Hand-built fixtures with invented players."""

import json
from datetime import datetime, timezone

import pytest

import sources

PGN = (
    '[Event "Live Chess"]\n[Date "2025.11.03"]\n[White "TestUser"]\n[Black "Rival"]\n'
    '[ECO "B14"]\n'
    '[ECOUrl "https://www.chess.com/openings/Caro-Kann-Defense-Panov-Attack...5.Nc3-e6-6.Nf3-Nc6"]\n'
    '[TimeControl "300+5"]\n\n'
    "1. e4 {[%clk 0:05:04.8]} 1... c6 {[%clk 0:05:02.9]} 2. d4 {[%clk 0:05:08.9]} 2... d5 "
    "{[%clk 0:05:06.3]} 3. exd5 {[%clk 0:05:13.7]} 3... cxd5 {[%clk 0:05:09.9]} 1-0"
)


def cc_game(*, me_result="win", opp_result="resigned", me_white=True, rating=1500, end_time=1762189110,
            rules="chess", time_class="blitz", rated=True, time_control="300+5", pgn=PGN, opp_rating=1480):
    me = {"rating": rating, "result": me_result, "username": "TestUser"}
    opp = {"rating": opp_rating, "result": opp_result, "username": "Rival"}
    return {
        "url": "https://www.chess.com/game/live/1", "pgn": pgn, "time_control": time_control,
        "end_time": end_time, "rated": rated, "time_class": time_class, "rules": rules,
        "white": me if me_white else opp, "black": opp if me_white else me,
    }


def parse_cc(*games):
    return sources.parse_chesscom_month({"games": list(games)}, "testuser")


# --- month_bounds ----------------------------------------------------------


def test_month_bounds_ordinary_month():
    start, end = sources.month_bounds("2026-09")
    assert start == datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_month_bounds_december_rolls_into_next_year():
    start, end = sources.month_bounds("2025-12")
    assert end == datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("bad", ["2026", "2026-13", "September", "2026-9-1", ""])
def test_month_bounds_rejects_malformed(bad):
    with pytest.raises(ValueError):
        sources.month_bounds(bad)


# --- Chess.com -------------------------------------------------------------


def test_chesscom_empty_month_is_no_games_not_an_error():
    assert parse_cc() == []


def test_chesscom_filters_out_variants_other_time_classes_and_unrated():
    games = parse_cc(
        cc_game(me_result="bughousepartnerlose", rules="bughouse"),
        cc_game(time_class="rapid"),
        cc_game(rated=False),
        cc_game(),
    )
    assert len(games) == 1


def test_chesscom_win_takes_its_ending_from_the_opponents_code():
    game = parse_cc(cc_game(me_result="win", opp_result="checkmated"))[0]
    assert (game.result, game.ending) == ("W", "checkmated")


@pytest.mark.parametrize("code", ["agreed", "repetition", "stalemate", "insufficient", "50move", "timevsinsufficient"])
def test_chesscom_draw_codes(code):
    game = parse_cc(cc_game(me_result=code, opp_result=code))[0]
    assert (game.result, game.ending) == ("D", code)


@pytest.mark.parametrize("code", ["checkmated", "resigned", "timeout", "abandoned"])
def test_chesscom_loss_codes(code):
    game = parse_cc(cc_game(me_result=code, opp_result="win"))[0]
    assert (game.result, game.ending) == ("L", code)


def test_chesscom_unknown_result_code_fails_loudly_instead_of_counting_as_a_loss():
    with pytest.raises(sources.UnknownResult, match="mystery"):
        parse_cc(cc_game(me_result="mystery"))


def test_chesscom_finds_the_players_side_whichever_colour_and_case():
    as_black = parse_cc(cc_game(me_white=False))[0]
    assert as_black.colour == "black"
    assert parse_cc(cc_game(me_white=True))[0].colour == "white"


def test_chesscom_game_without_the_player_fails_loudly():
    with pytest.raises(sources.SourceError):
        sources.parse_chesscom_month({"games": [cc_game()]}, "someone_else")


def test_chesscom_sorts_by_time_and_chains_rating_before():
    later = cc_game(rating=1520, end_time=2000)
    earlier = cc_game(rating=1490, end_time=1000)
    first, second = parse_cc(later, earlier)
    assert first.rating_after == 1490 and first.rating_before is None
    assert second.rating_after == 1520 and second.rating_before == 1490


def test_chesscom_counts_full_moves_ignoring_clock_comments_and_black_markers():
    assert parse_cc(cc_game())[0].moves == 3


def test_chesscom_opening_is_the_slug_without_the_move_list():
    game = parse_cc(cc_game())[0]
    assert game.opening == "Caro-Kann-Defense-Panov-Attack"
    assert game.eco == "B14"


def test_chesscom_missing_opening_is_none():
    game = parse_cc(cc_game(pgn='[White "TestUser"]\n\n1. e4 e5 1-0'))[0]
    assert game.opening is None and game.eco is None


@pytest.mark.parametrize("given,expected", [("300+5", "300+5"), ("180", "180+0"), ("600", "600+0")])
def test_chesscom_time_control_is_normalised(given, expected):
    assert parse_cc(cc_game(time_control=given))[0].time_control == expected


# --- Lichess ---------------------------------------------------------------


def li_game(*, id="abc12345", status="resign", winner="white", me_white=True, rating=1500, diff=8,
            variant="standard", perf="blitz", rated=True, last_move_at=1_700_000_000_000, moves="e4 e5 Nf3 Nc6 Bb5",
            clock=None, opening=None, opponent_user="rival", drop_diff=False):
    me = {"user": {"name": "TestUser", "id": "testuser"}, "rating": rating}
    if not drop_diff:
        me["ratingDiff"] = diff
    opp = {"user": {"name": "Rival", "id": "rival"}, "rating": 1480, "ratingDiff": -8}
    if opponent_user is None:
        opp = {"rating": 1480, "ratingDiff": -8}  # anonymous
    game = {
        "id": id, "rated": rated, "variant": variant, "perf": perf, "status": status,
        "lastMoveAt": last_move_at, "moves": moves,
        "clock": clock if clock is not None else {"initial": 180, "increment": 2, "totalTime": 260},
        "players": {"white": me if me_white else opp, "black": opp if me_white else me},
    }
    if winner:
        game["winner"] = winner
    if opening:
        game["opening"] = opening
    return game


def parse_li(*games, user="testuser"):
    return sources.parse_lichess_month("\n".join(json.dumps(g) for g in games), user)


def test_lichess_empty_body_is_no_games_not_an_error():
    assert sources.parse_lichess_month("", "testuser") == []
    assert sources.parse_lichess_month("\n\n", "testuser") == []


def test_lichess_win_as_white_and_rating_before_and_after():
    game = parse_li(li_game(winner="white", rating=1500, diff=8))[0]
    assert game.result == "W" and game.ending == "resigned"
    assert (game.rating_before, game.rating_after) == (1500, 1508)
    assert game.colour == "white"


def test_lichess_loss_as_black_uses_a_negative_diff():
    game = parse_li(li_game(me_white=False, winner="white", rating=1500, diff=-9))[0]
    assert game.result == "L" and game.colour == "black"
    assert game.rating_after == 1491


def test_lichess_draw_needs_a_drawn_status_and_no_winner():
    game = parse_li(li_game(status="draw", winner=None, diff=0))[0]
    assert (game.result, game.ending) == ("D", "draw")


def test_lichess_aborted_games_are_skipped():
    assert parse_li(li_game(status="aborted", winner=None), li_game(id="real0001")) [0].url.endswith("real0001")


def test_lichess_unknown_status_fails_loudly():
    with pytest.raises(sources.UnknownResult, match="unknownFinish"):
        parse_li(li_game(status="unknownFinish", winner=None))


def test_lichess_no_winner_is_not_automatically_a_draw():
    for status in ("resign", "mate", "cheat"):
        with pytest.raises(sources.UnknownResult):
            parse_li(li_game(status=status, winner=None))


@pytest.mark.parametrize("status", ["timeout", "outoftime"])
def test_lichess_flag_fall_with_no_winner_is_a_draw(status):
    # Confirmed on real games: the clock ran out against a player who couldn't
    # mate, Result 1/2-1/2, and Lichess still reports a timeout status.
    game = parse_li(li_game(status=status, winner=None, diff=0))[0]
    assert (game.result, game.ending) == ("D", "timeout")


@pytest.mark.parametrize("status", ["timeout", "outoftime"])
def test_lichess_flag_fall_with_a_winner_is_still_decided(status):
    assert parse_li(li_game(status=status, winner="white"))[0].result == "W"
    assert parse_li(li_game(status=status, winner="black"))[0].result == "L"


def test_lichess_draw_with_a_winner_is_contradictory():
    with pytest.raises(sources.UnknownResult):
        parse_li(li_game(status="draw", winner="white"))


@pytest.mark.parametrize("field,value", [("variant", "chess960"), ("perf", "rapid"), ("rated", False)])
def test_lichess_anything_but_rated_standard_blitz_fails_loudly(field, value):
    with pytest.raises(sources.UnknownResult):
        parse_li(li_game(**{field: value}))


def test_lichess_missing_rating_change_fails_loudly():
    with pytest.raises(sources.SourceError, match="rating change"):
        parse_li(li_game(drop_diff=True))


def test_lichess_time_control_moves_and_opening():
    opening = {"eco": "C60", "name": "Ruy Lopez", "ply": 5}
    game = parse_li(li_game(opening=opening, moves="e4 e5 Nf3 Nc6 Bb5"))[0]
    assert game.time_control == "180+2"
    assert game.moves == 3  # 5 plies is 3 full moves
    assert (game.opening, game.eco) == ("Ruy Lopez", "C60")
    assert game.url == "https://lichess.org/abc12345"


def test_lichess_missing_opening_is_none():
    game = parse_li(li_game())[0]
    assert game.opening is None and game.eco is None


def test_lichess_anonymous_opponent():
    assert parse_li(li_game(opponent_user=None))[0].opponent == "anonymous"


def test_lichess_username_match_ignores_case():
    assert len(parse_li(li_game(), user="TESTUSER")) == 1


def test_lichess_game_without_the_player_fails_loudly():
    with pytest.raises(sources.SourceError):
        parse_li(li_game(), user="someone_else")


def test_lichess_bad_json_fails_loudly():
    with pytest.raises(sources.SourceError, match="wasn't JSON"):
        sources.parse_lichess_month("<!DOCTYPE html>", "testuser")


@pytest.mark.parametrize("status", ["created", "started"])
def test_lichess_games_still_in_progress_are_skipped_not_an_error(status):
    done = li_game(id="done0001")
    live = li_game(id="live0001", status=status, winner=None)
    assert [g.url[-8:] for g in parse_li(done, live)] == ["done0001"]


# --- incremental fetching --------------------------------------------------


def _ended(seconds):
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def test_only_after_keeps_strictly_later_games():
    games = parse_cc(cc_game(end_time=1000), cc_game(end_time=2000), cc_game(end_time=3000))
    assert [g.ended_at for g in sources.only_after(games, _ended(2000))] == [_ended(3000)]


def test_only_after_none_keeps_everything():
    games = parse_cc(cc_game(end_time=1000), cc_game(end_time=2000))
    assert sources.only_after(games, None) == games


def test_only_after_keeps_the_rating_chain_for_the_first_new_game():
    games = parse_cc(cc_game(rating=1490, end_time=1000), cc_game(rating=1510, end_time=2000))
    (new,) = sources.only_after(games, _ended(1000))
    assert new.rating_before == 1490 and new.rating_after == 1510


def test_lichess_export_params_for_a_whole_month():
    params = sources.lichess_export_params("2026-09")
    assert params["since"] == int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp() * 1000)
    assert params["until"] == int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
    assert (params["perfType"], params["rated"], params["sort"]) == ("blitz", "true", "dateAsc")


def test_lichess_export_params_start_at_the_watermark_and_rely_on_only_after_for_the_exact_cut():
    # Lichess returns a game whose last move is within the watermark's second,
    # so `since` is only a hint. Asking from the watermark (not +1ms) can't
    # miss a game, and only_after() drops the repeat.
    watermark = datetime(2026, 9, 12, 10, 30, tzinfo=timezone.utc)
    params = sources.lichess_export_params("2026-09", after=watermark)
    assert params["since"] == int(watermark.timestamp() * 1000)


def test_lichess_export_params_never_reach_back_before_the_month():
    params = sources.lichess_export_params("2026-09", after=datetime(2026, 8, 1, tzinfo=timezone.utc))
    assert params["since"] == int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp() * 1000)


def test_lichess_games_come_back_oldest_first():
    late = li_game(id="late0001", last_move_at=2_000_000_000_000)
    early = li_game(id="early001", last_move_at=1_000_000_000_000)
    assert [g.url[-8:] for g in parse_li(late, early)] == ["early001", "late0001"]

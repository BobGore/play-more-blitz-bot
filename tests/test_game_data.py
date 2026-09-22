"""game_data.py: reading a game as the two sites send it."""

import pytest

import game_data as gd

TEN_MOVES = "e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7"


def lichess(**over):
    raw = {"id": "abcd1234", "variant": "standard", "status": "resign", "moves": TEN_MOVES,
           "players": {"white": {"user": {"name": "alice_example"}, "analysis": {"accuracy": 88}}, "black": {"user": {"name": "zed_example"}}}}
    raw.update(over)
    return raw


PGN = '''[Event "Live Chess"]
[Site "Chess.com"]
[White "alice_example"]
[Black "zed_example"]
[Result "1-0"]
[ECO "C78"]
[Link "https://www.chess.com/game/live/123456789"]

1. e4 {[%clk 0:05:02.6]} 1... e5 {[%clk 0:05:03.9]} 2. Nf3 {[%clk 0:04:54.3]} 2... Nc6 {[%clk 0:05:07.9]} 3. Bb5 {[%clk 0:04:50.0]}
3... a6 {[%clk 0:05:00.1]} 4. Ba4 {[%clk 0:04:40.0]} 4... Nf6 {[%clk 0:04:59.9]} 5. O-O {[%clk 0:04:30.0]} 5... Be7 $6 {[%clk 0:04:58.0]} 1-0
'''


def chesscom(**over):
    raw = {"url": "https://www.chess.com/game/live/123456789", "rules": "chess", "pgn": PGN, "time_class": "blitz",
           "initial_setup": gd.STANDARD_START, "accuracies": {"white": 88.31, "black": 71.5}}
    raw.update(over)
    return raw


# --- Lichess -------------------------------------------------------------------------------------------------------

def test_a_lichess_game_gives_its_moves_and_the_sites_own_accuracy():
    g = gd.from_lichess(lichess())
    assert g.moves == tuple(TEN_MOVES.split()) and (g.site_white_accuracy, g.site_black_accuracy) == (88.0, None)


@pytest.mark.parametrize("variant", ["chess960", "fromPosition", "crazyhouse", "antichess", None, ""])
def test_a_lichess_variant_is_not_analysed(variant):
    with pytest.raises(gd.NotAnalysable) as why:
        gd.from_lichess(lichess(variant=variant))
    assert why.value.reason == gd.NOT_STANDARD_START


def test_a_lichess_game_from_a_set_position_is_not_analysed_even_if_it_says_standard():
    with pytest.raises(gd.NotAnalysable) as why:
        gd.from_lichess(lichess(initialFen="8/8/8/8/8/8/8/K1k5 w - - 0 1"))
    assert why.value.reason == gd.NOT_STANDARD_START


@pytest.mark.parametrize("status, reason", [("aborted", gd.TOO_SHORT), ("noStart", gd.TOO_SHORT), ("started", gd.UNAVAILABLE), ("created", gd.UNAVAILABLE)])
def test_a_lichess_game_that_never_got_going_or_is_still_on_is_not_analysed(status, reason):
    with pytest.raises(gd.NotAnalysable) as why:
        gd.from_lichess(lichess(status=status))
    assert why.value.reason == reason


def test_a_lichess_game_with_no_moves_field_is_unavailable():
    with pytest.raises(gd.NotAnalysable) as why:
        gd.from_lichess(lichess(moves=None))
    assert why.value.reason == gd.UNAVAILABLE


def test_a_short_game_is_too_short_and_the_limit_is_adjustable():
    with pytest.raises(gd.NotAnalysable) as why:
        gd.from_lichess(lichess(moves="e4 e5 Nf3 Nc6 Bb5"))
    assert why.value.reason == gd.TOO_SHORT
    assert len(gd.from_lichess(lichess(moves="e4 e5 Nf3 Nc6 Bb5"), min_plies=5).moves) == 5
    assert len(gd.from_lichess(lichess(moves="e4 e5 Nf3 Nc6 Bb5 a6"), min_plies=gd.MIN_PLIES).moves) == 6


@pytest.mark.parametrize("figure", [None, "high", True, -1, 101, {}])
def test_a_site_accuracy_that_is_not_a_percentage_is_ignored(figure):
    raw = lichess()
    raw["players"]["white"]["analysis"]["accuracy"] = figure
    assert gd.from_lichess(raw).site_white_accuracy is None


def test_a_lichess_game_with_no_players_still_reads():
    assert gd.from_lichess(lichess(players=None)).site_white_accuracy is None


# --- Chess.com -----------------------------------------------------------------------------------------------------

def test_the_moves_of_a_chesscom_pgn_are_read_without_numbers_clocks_or_annotations():
    assert gd.moves_from_pgn(PGN) == TEN_MOVES.split()


def test_variations_comments_and_results_are_left_out_of_the_moves():
    text = '[Result "*"]\n\n1. e4 {a comment with 1. d4 in it} (1. d4 d5) e5 2. Nf3 $1 Nc6 *'
    assert gd.moves_from_pgn(text) == ["e4", "e5", "Nf3", "Nc6"]
    assert gd.moves_from_pgn("1. e4 e5 1/2-1/2") == ["e4", "e5"]
    assert gd.moves_from_pgn("") == []


def test_a_chesscom_game_gives_its_moves_and_the_sites_own_accuracy():
    g = gd.from_chesscom(chesscom())
    assert g.moves == tuple(TEN_MOVES.split()) and (g.site_white_accuracy, g.site_black_accuracy) == (88.31, 71.5)


@pytest.mark.parametrize("rules", ["chess960", "bughouse", "crazyhouse", "kingofthehill", "threecheck", None])
def test_a_chesscom_variant_is_not_analysed(rules):
    with pytest.raises(gd.NotAnalysable) as why:
        gd.from_chesscom(chesscom(rules=rules))
    assert why.value.reason == gd.NOT_STANDARD_START


def test_a_chesscom_game_from_another_start_is_not_analysed():
    with pytest.raises(gd.NotAnalysable) as why:
        gd.from_chesscom(chesscom(initial_setup="4k3/8/8/8/8/8/8/4K3 w - - 0 1"))
    assert why.value.reason == gd.NOT_STANDARD_START
    fen_in_pgn = PGN.replace("[ECO", '[SetUp "1"]\n[FEN "4k3/8/8/8/8/8/8/4K3 w - - 0 1"]\n[ECO')
    with pytest.raises(gd.NotAnalysable):
        gd.from_chesscom(chesscom(initial_setup=None, pgn=fen_in_pgn))


def test_a_chesscom_game_without_a_pgn_is_unavailable():
    for pgn in (None, "", "   "):
        with pytest.raises(gd.NotAnalysable) as why:
            gd.from_chesscom(chesscom(pgn=pgn))
        assert why.value.reason == gd.UNAVAILABLE


def test_a_short_chesscom_game_is_too_short():
    with pytest.raises(gd.NotAnalysable) as why:
        gd.from_chesscom(chesscom(pgn='[Result "1-0"]\n\n1. e4 e5 2. Qh5 Nc6 1-0'))
    assert why.value.reason == gd.TOO_SHORT


def test_chesscom_without_accuracies_gives_none():
    g = gd.from_chesscom(chesscom(accuracies=None))
    assert (g.site_white_accuracy, g.site_black_accuracy) == (None, None)


# --- finding a game in an archive --------------------------------------------------------------------------------------

ARCHIVE = {"games": [{"url": "https://www.chess.com/game/live/111"}, {"url": "https://www.chess.com/game/live/123456789", "n": 2},
                     {"url": "https://www.chess.com/game/daily/123456789", "n": 3}, {"url": "https://www.chess.com/game/live/6123456789"},
                     {"url": None}, {}]}


def test_a_game_is_found_by_its_id_and_kind():
    assert gd.find_chesscom_game(ARCHIVE, "live/123456789")["n"] == 2
    assert gd.find_chesscom_game(ARCHIVE, "daily/123456789")["n"] == 3


def test_a_game_that_is_not_there_is_not_found_and_a_longer_number_does_not_match():
    assert gd.find_chesscom_game(ARCHIVE, "live/23456789") is None
    assert gd.find_chesscom_game(ARCHIVE, "live/999") is None
    assert gd.find_chesscom_game({}, "live/1") is None
    assert gd.find_chesscom_game({"games": []}, "live/1") is None


# --- the clocks ---------------------------------------------------------------------------------------------------------------------

def test_lichess_clocks_are_read_as_seconds_one_per_ply():
    centiseconds = [30003, 30003, 29955, 29875, 29907, 29731, 29500, 29410, 29020, 29000]
    assert gd.from_lichess(lichess(clocks=centiseconds)).clocks == tuple(v / 100 for v in centiseconds)


def test_lichess_may_list_one_clock_more_than_there_are_plies_and_the_extra_is_dropped():
    centiseconds = list(range(30000, 30011))                                               # 11 for 10 plies
    assert gd.from_lichess(lichess(clocks=centiseconds)).clocks == tuple(v / 100 for v in centiseconds[:10])


@pytest.mark.parametrize("clocks", [None, "30003", [30003] * 9, [30003] * 9 + [-1], [30003] * 9 + [1.5], [30003] * 9 + [True], [30003] * 9 + ["5"], {}])
def test_lichess_clocks_that_are_missing_short_or_unsound_give_none(clocks):
    raw = lichess() if clocks is None else lichess(clocks=clocks)
    assert gd.from_lichess(raw).clocks is None


def test_a_lichess_game_with_zero_clock_values_is_still_a_game_with_clocks():
    assert gd.from_lichess(lichess(clocks=[0] * 10)).clocks == (0.0,) * 10


def test_chesscom_clocks_come_from_the_clk_comments_in_move_order():
    assert gd.from_chesscom(chesscom()).clocks == (302.6, 303.9, 294.3, 307.9, 290.0, 300.1, 280.0, 299.9, 270.0, 298.0)


def test_chesscom_clocks_in_hours_and_whole_seconds_are_read():
    pgn = PGN.replace("[%clk 0:05:02.6]", "[%clk 1:02:03.4]").replace("[%clk 0:05:03.9]", "[%clk 0:04:58]")
    assert gd.from_chesscom(chesscom(pgn=pgn)).clocks[:2] == (3723.4, 298.0)


def test_chesscom_clocks_are_none_unless_there_is_one_for_every_ply():
    assert gd.from_chesscom(chesscom(pgn=PGN.replace(" {[%clk 0:04:58.0]}", ""))).clocks is None                # 9 of 10
    assert gd.from_chesscom(chesscom(pgn=PGN.replace("Be7 $6 {[%clk 0:04:58.0]}", "Be7 {[%clk 0:04:58.0]} {[%clk 0:04:57.0]}"))).clocks is None   # 11 of 10
    no_clocks = "\n\n".join([PGN.split("\n\n")[0], "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7 1-0"])
    assert gd.from_chesscom(chesscom(pgn=no_clocks)).clocks is None                                              # a daily game: no clocks


def test_a_clock_looking_thing_in_the_headers_is_not_counted():
    header, body = PGN.split("\n\n")
    tricked = header + '\n[Note "[%clk 0:00:01]"]\n\n' + body
    assert gd.from_chesscom(chesscom(pgn=tricked)).clocks == gd.from_chesscom(chesscom()).clocks


def test_a_game_data_built_without_clocks_has_none():
    assert gd.GameData(("e4",) * 6, None, None).clocks is None

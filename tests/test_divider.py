"""divider.py: where the middlegame and the endgame start."""

import pytest

import divider


def chess():
    return pytest.importorskip("chess")


def position(fen):
    c = chess()
    b = c.Board(fen)
    return (b.occupied, b.kings, b.pawns, b.occupied_co[c.WHITE], b.occupied_co[c.BLACK])


START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
EIGHT_PIECES = "rnbqk3/8/8/8/8/8/8/RNBQK3 w - - 0 1"  # four majors and minors each, both back ranks still full enough
TWO_PIECES = "r3k3/8/8/8/8/8/8/R3K3 w - - 0 1"
FOUR_ROOKS_AND_PAWNS = "r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R3K2R w - - 0 1"


# --- the mixedness table, spot-checked against the scalachess source ---------------------------------------------

@pytest.mark.parametrize("y, white, black, expected", [
    (3, 0, 1, 4), (3, 0, 2, 5), (6, 0, 2, 0), (3, 0, 3, 7), (7, 0, 3, 0), (3, 0, 4, 7), (3, 0, 5, 0),
    (3, 1, 0, 6), (3, 1, 1, 6), (4, 1, 1, 5), (3, 1, 2, 8), (3, 1, 3, 9), (3, 1, 4, 0),
    (3, 2, 0, 3), (2, 2, 0, 0), (3, 2, 1, 6), (5, 2, 2, 7), (3, 2, 3, 0),
    (3, 3, 0, 5), (1, 3, 0, 0), (3, 3, 1, 7), (3, 3, 2, 0),
    (3, 4, 0, 5), (1, 4, 0, 0), (3, 4, 1, 0), (3, 5, 0, 0), (3, 0, 0, 0),
])
def test_the_score_of_one_block(y, white, black, expected):
    assert divider._score(y, white, black) == expected


def test_the_starting_position_is_not_mixed_at_all():
    """Each 2x2 block holds pieces of one colour only, and the rows that hold four score nothing."""
    _, _, _, white, black = position(START)
    assert divider.mixedness(white, black) == 0


def test_there_are_forty_nine_blocks_of_four_squares():
    assert len(divider._BLOCKS) == 49 and all(b.bit_count() == 4 for b in divider._BLOCKS)


# --- the tests on a position -----------------------------------------------------------------------------------

def test_counting_majors_and_minors_leaves_out_kings_and_pawns():
    occupied, kings, pawns, _, _ = position(START)
    assert divider.majors_and_minors(occupied, kings, pawns) == 14
    occupied, kings, pawns, _, _ = position(EIGHT_PIECES)
    assert divider.majors_and_minors(occupied, kings, pawns) == 8
    occupied, kings, pawns, _, _ = position(FOUR_ROOKS_AND_PAWNS)
    assert divider.majors_and_minors(occupied, kings, pawns) == 4


def test_the_back_rank_is_sparse_when_either_side_has_fewer_than_four_pieces_there():
    _, _, _, white, black = position(START)
    assert not divider.backrank_sparse(white, black)
    _, _, _, white, black = position("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/R3K3 w - - 0 1")  # White down to two on the rank
    assert divider.backrank_sparse(white, black)
    _, _, _, white, black = position("r3k3/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w - - 0 1")  # Black down to two
    assert divider.backrank_sparse(white, black)
    _, _, _, white, black = position("r2qk2r/pppppppp/8/8/8/8/PPPPPPPP/R2QK2R w - - 0 1")  # exactly four each: not sparse
    assert not divider.backrank_sparse(white, black)
    _, _, _, white, black = position("r2qk3/pppppppp/8/8/8/8/PPPPPPPP/R2QK2R w - - 0 1")  # Black down to three
    assert divider.backrank_sparse(white, black)


# --- dividing a game -------------------------------------------------------------------------------------------

def test_a_game_that_never_thins_out_has_no_phases():
    assert divider.divide([position(START)] * 5) == (None, None)


def test_the_middlegame_starts_at_the_first_position_with_ten_or_fewer_pieces():
    assert divider.divide([position(START), position(START), position(EIGHT_PIECES), position(EIGHT_PIECES)]) == (2, None)


def test_the_endgame_starts_at_the_first_position_with_six_or_fewer_pieces():
    positions = [position(START), position(EIGHT_PIECES), position(TWO_PIECES), position(TWO_PIECES)]
    assert divider.divide(positions) == (1, 2)


def test_the_middlegame_can_start_early_on_a_sparse_back_rank():
    developed = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/R3K3 w - - 0 1"
    assert divider.divide([position(START), position(developed)]) == (1, None)


def test_a_jump_straight_to_an_endgame_leaves_no_middlegame():
    assert divider.divide([position(START), position(TWO_PIECES)]) == (None, 1)


def test_the_first_position_counts_as_ply_zero():
    assert divider.divide([position(EIGHT_PIECES)]) == (0, None)


def test_the_mixedness_limit_can_start_the_middlegame_on_its_own():
    """A full checkerboard of both colours: no development, no thinning, but as mixed as a board can be."""
    white = sum(1 << sq for sq in range(64) if (sq // 8 + sq % 8) % 2 == 0)
    black = sum(1 << sq for sq in range(64) if (sq // 8 + sq % 8) % 2 == 1)
    full = (white | black, 0, 0, white, black)
    assert divider.majors_and_minors(*full[:3]) == 64 and not divider.backrank_sparse(white, black)
    assert divider.mixedness(white, black) == 49 * 7 > divider.MIXEDNESS_LIMIT
    assert divider.divide([position(START), full]) == (1, None)

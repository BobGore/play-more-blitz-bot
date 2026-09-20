"""Where a game's opening ends and its middlegame and endgame begin.

A translation of the divider in scalachess (the chess library behind Lichess, MIT licence), so that our
phases start where Lichess's do. A position is described by five bitboards (square a1 is bit 0, h8 is
bit 63, the same layout as python-chess and scalachess): every occupied square, the kings, the pawns,
the white pieces and the black pieces. Nothing here needs a chess library.

    positions[0] is the starting position and positions[n] the position after n half-moves (plies).
    divide(positions) gives (middle, end): the ply at which the middlegame starts and the one at which
    the endgame starts, either of which is None if the game never got there.

The middlegame starts at the first position with ten or fewer major and minor pieces, or where a back
rank has fewer than four pieces left (they have been developed), or where the "mixedness" of the
pieces on the board, a measure of how tangled the two armies are, is over 150. The endgame starts at
the first position with six or fewer major and minor pieces.
"""

FIRST_RANK = 0xFF
LAST_RANK = 0xFF << 56

MIDDLEGAME_PIECES = 10
ENDGAME_PIECES = 6
BACKRANK_MINIMUM = 4
MIXEDNESS_LIMIT = 150

# Mixedness looks at every 2x2 block of squares (there are 7 x 7 of them).
_BLOCKS = [0x0303 << (x + 8 * y) for y in range(7) for x in range(7)]


def majors_and_minors(occupied, kings, pawns):
    """Queens, rooks, bishops and knights of both sides."""
    return (occupied & ~(kings | pawns)).bit_count()


def backrank_sparse(white, black):
    """True once either side has developed enough that fewer than four pieces are left on its first rank."""
    return (FIRST_RANK & white).bit_count() < BACKRANK_MINIMUM or (LAST_RANK & black).bit_count() < BACKRANK_MINIMUM


def _score(y, white, black):
    """How mixed one 2x2 block is: `white` and `black` pieces in it, `y` its row counted from 1 at the bottom."""
    if white == 0:
        if black == 1:
            return 1 + y
        if black == 2:
            return 2 + (6 - y) if y < 6 else 0
        if black in (3, 4):
            return 3 + (7 - y) if y < 7 else 0
        return 0
    if white == 1:
        if black == 0:
            return 1 + (8 - y)
        if black == 1:
            return 5 + abs(4 - y)
        if black == 2:
            return 4 + (7 - y)
        if black == 3:
            return 5 + (7 - y)
        return 0
    if white == 2:
        if black == 0:
            return 2 + (y - 2) if y > 2 else 0
        if black == 1:
            return 4 + (y - 1)
        if black == 2:
            return 7
        return 0
    if white == 3:
        if black == 0:
            return 3 + (y - 1) if y > 1 else 0
        if black == 1:
            return 5 + (y - 1)
        return 0
    if white == 4:
        return 3 + (y - 1) if black == 0 and y > 1 else 0  # a group of four on the home row scores 0
    return 0


def mixedness(white, black):
    return sum(_score(i // 7 + 1, (white & block).bit_count(), (black & block).bit_count()) for i, block in enumerate(_BLOCKS))


def divide(positions):
    """(middle, end) plies for a list of positions given as (occupied, kings, pawns, white, black) bitboards."""
    middle = end = None
    for ply, (occupied, kings, pawns, white, black) in enumerate(positions):
        pieces = majors_and_minors(occupied, kings, pawns)
        if middle is None and (pieces <= MIDDLEGAME_PIECES or backrank_sparse(white, black) or mixedness(white, black) > MIXEDNESS_LIMIT):
            middle = ply
        if pieces <= ENDGAME_PIECES:  # six or fewer is also ten or fewer, so `middle` has been set by now
            end = ply
            break
    if end is not None and middle is not None and middle >= end:
        middle = None  # the endgame arrived first or together: there is no middlegame
    return middle, end

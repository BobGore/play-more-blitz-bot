"""Known-answer check of game_data.py against real Chess.com and Lichess games.

Every game's moves are replayed with python-chess: they must all be legal, and a Chess.com game must end on exactly
the position the archive reports for it. The games name real people, so the files live in tests/fixtures_private/
(gitignored) and the tests skip themselves when they are absent. Nothing real is named in this committed file.
"""

import json
from pathlib import Path

import pytest

import game_data as gd

PRIVATE = Path(__file__).parent / "fixtures_private"
CHESSCOM = sorted((PRIVATE / "chesscom").glob("*.json")) if (PRIVATE / "chesscom").exists() else []
LICHESS = PRIVATE / "lichess_analysed_games.ndjson"


@pytest.mark.skipif(not CHESSCOM, reason="private fixtures not present")
def test_chesscom_moves_replay_to_the_position_the_archive_reports():
    chess = pytest.importorskip("chess")
    checked = 0
    for path in CHESSCOM:
        for raw in json.loads(path.read_text(encoding="utf-8")).get("games", []):
            try:
                data = gd.from_chesscom(raw)
            except gd.NotAnalysable:
                continue
            board = chess.Board()
            for move in data.moves:
                board.push_san(move)
            assert board.board_fen() == raw["fen"].split()[0], raw["url"]
            checked += 1
    assert checked > 300


@pytest.mark.skipif(not LICHESS.exists(), reason="private fixture not present")
def test_lichess_moves_are_all_legal_and_variants_are_refused():
    chess = pytest.importorskip("chess")
    analysed = refused = 0
    for line in LICHESS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        try:
            data = gd.from_lichess(raw)
        except gd.NotAnalysable as why:
            refused += 1
            assert why.reason in (gd.NOT_STANDARD_START, gd.TOO_SHORT, gd.UNAVAILABLE)
            continue
        board = chess.Board()
        for move in data.moves:
            board.push_san(move)
        analysed += 1
    assert analysed > 100 and refused > 0

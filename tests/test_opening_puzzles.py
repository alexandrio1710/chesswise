"""Tests for opening_puzzles.attempt_move: an out-of-range move_index used
to raise an uncaught IndexError (raw 500) instead of the clean 404 the
route's error handling implies, and a queen-auto-promotion attempt used to
be silently graded "correct" (then secretly replaced with a different
move) whenever the puzzle's actual solution needed a different promotion
piece.
"""

import itertools
import json

import pytest

from db import get_connection
from opening_puzzles import attempt_move

_id_counter = itertools.count(1)


def _insert_puzzle(fen: str, solution_uci: list[str], side_to_move: str = "white") -> int:
    conn = get_connection()
    try:
        puzzle_id = conn.execute(
            "INSERT INTO opening_puzzles (external_id, opening_family, fen, side_to_move, "
            "solution_uci, fetched_at) VALUES (?, ?, ?, ?, ?, datetime('now'))",
            (f"test-{next(_id_counter)}", "Test Opening", fen, side_to_move, json.dumps(solution_uci)),
        ).lastrowid
        conn.commit()
        return puzzle_id
    finally:
        conn.close()


class TestMoveIndexBounds:
    def test_out_of_range_move_index_raises_value_error_not_index_error(self):
        # A simple one-move puzzle: solution has exactly one entry (index 0).
        puzzle_id = _insert_puzzle("4k3/8/4K3/8/8/8/4P3/8 w - - 0 1", ["e2e4"])
        with pytest.raises(ValueError):
            attempt_move(puzzle_id, move_index=99, from_square="e2", to_square="e4")

    def test_negative_move_index_raises_value_error(self):
        puzzle_id = _insert_puzzle("4k3/8/4K3/8/8/8/4P3/8 w - - 0 1", ["e2e4"])
        with pytest.raises(ValueError):
            attempt_move(puzzle_id, move_index=-1, from_square="e2", to_square="e4")

    def test_in_range_move_index_does_not_raise(self):
        puzzle_id = _insert_puzzle("4k3/8/4K3/8/8/8/4P3/8 w - - 0 1", ["e2e4"])
        result = attempt_move(puzzle_id, move_index=0, from_square="e2", to_square="e4")
        assert result["correct"] is True


class TestPromotionExactMatch:
    # White king e6, white pawn e7, black king a4 (legal, no checks) —
    # e7e8 needs a promotion piece to be legal at all.
    FEN = "8/4P3/4K3/8/k7/8/8/8 w - - 0 1"

    def test_queen_promotion_matches_queen_solution(self):
        puzzle_id = _insert_puzzle(self.FEN, ["e7e8q"])
        result = attempt_move(puzzle_id, move_index=0, from_square="e7", to_square="e8")
        assert result["correct"] is True

    def test_queen_auto_promotion_does_not_match_knight_solution(self):
        # The solution specifically requires underpromotion to a knight;
        # a bare e7e8 click auto-fills to queen (no promotion picker in
        # the UI) and must be graded wrong, not silently accepted as
        # "correct" while actually playing the knight move underneath.
        puzzle_id = _insert_puzzle(self.FEN, ["e7e8n"])
        result = attempt_move(puzzle_id, move_index=0, from_square="e7", to_square="e8")
        assert result["correct"] is False
        assert result["done"] is True

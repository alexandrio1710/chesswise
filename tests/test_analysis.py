"""Unit tests for analysis.py's game-phase detection — a direct port of
Lichess's own Divider algorithm (see analysis.py's module comment above
MIDGAME_MAJORS_MINORS_CEILING for the source link and full rationale).
Stockfish isn't needed for any of this: phase depends only on piece
positions, not evals.
"""

import chess

from analysis import (
    ENDGAME_MAJORS_MINORS_CEILING,
    MIDGAME_MAJORS_MINORS_CEILING,
    _backrank_sparse,
    _compute_phase_boundaries,
    _is_endgame_position,
    _is_midgame_position,
    _majors_and_minors,
    _mixedness,
    _phase_for_ply,
)


def _boards_for(pgn_moves: str) -> list[chess.Board]:
    """One board per ply, in order — analyze_game_moves's own board
    sequence, without needing Stockfish (phase doesn't use evals)."""
    board = chess.Board()
    boards = []
    for san in pgn_moves.split():
        board.push_san(san)
        boards.append(board.copy(stack=False))
    return boards


class TestMajorsAndMinors:
    def test_starting_position_has_fourteen(self):
        # 2R + 2N + 2B + 1Q per side, pawns and kings excluded.
        assert _majors_and_minors(chess.Board()) == 14

    def test_pawns_and_kings_are_excluded(self):
        # King + a lone pawn each side, no majors/minors at all.
        board = chess.Board("4k3/4p3/8/8/8/8/4P3/4K3 w - - 0 1")
        assert _majors_and_minors(board) == 0


class TestBackrankSparse:
    def test_starting_position_is_not_sparse(self):
        assert _backrank_sparse(chess.Board()) is False

    def test_developed_pieces_off_the_backrank_is_sparse(self):
        # White's rank 1 (Qc1, Rf1, Kg1) and black's rank 8 (Ra8, Ke8,
        # Rh8) both have only 3 pieces left — under the <4 threshold.
        board = chess.Board("r3k2r/8/8/8/8/8/8/2Q2RK1 w kq - 0 1")
        assert _backrank_sparse(board) is True


class TestMixedness:
    def test_starting_position_is_low(self):
        # Completely separated armies — nothing mixed yet.
        assert _mixedness(chess.Board()) == 0

    def test_a_contested_middlegame_position_is_higher_than_the_start(self):
        boards = _boards_for("e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7 Re1 b5 Bb3 d6 c3 O-O")
        assert _mixedness(boards[-1]) > _mixedness(chess.Board())


class TestIsMidgameEndgamePosition:
    def test_starting_position_is_neither(self):
        board = chess.Board()
        assert _is_midgame_position(board) is False
        assert _is_endgame_position(board) is False

    def test_thin_material_triggers_midgame_and_endgame_together(self):
        # Just 2 rooks left on the board (majors_and_minors=2) — comfortably
        # under both the midgame (<=10) and endgame (<=6) ceilings at once.
        board = chess.Board("4k3/8/8/8/8/8/8/R3K2R w KQ - 0 1")
        assert _majors_and_minors(board) <= MIDGAME_MAJORS_MINORS_CEILING
        assert _majors_and_minors(board) <= ENDGAME_MAJORS_MINORS_CEILING
        assert _is_midgame_position(board) is True
        assert _is_endgame_position(board) is True


class TestComputePhaseBoundaries:
    def test_no_flags_ever_true_is_opening_for_the_whole_game(self):
        middle, end = _compute_phase_boundaries([False] * 20, [False] * 20)
        assert middle is None
        assert end is None

    def test_midgame_flag_without_endgame_flag(self):
        flags = [False, False, True, True, True]
        middle, end = _compute_phase_boundaries(flags, [False] * 5)
        assert middle == 3  # 1-based: index 2 -> ply 3
        assert end is None

    def test_both_flags_found_in_order(self):
        midgame_flags = [False, True, True, True, True]
        endgame_flags = [False, False, False, True, True]
        middle, end = _compute_phase_boundaries(midgame_flags, endgame_flags)
        assert middle == 2
        assert end == 4

    def test_endgame_flag_before_midgame_flag_discards_the_midgame_boundary(self):
        # Only possible in an artificial position, but Divider.scala's own
        # behavior: middle gets dropped rather than reporting an endgame
        # that started before the middlegame did.
        midgame_flags = [False, False, True]
        endgame_flags = [True, True, True]
        middle, end = _compute_phase_boundaries(midgame_flags, endgame_flags)
        assert middle is None
        assert end == 1

    def test_endgame_is_never_searched_if_midgame_never_triggers(self):
        midgame_flags = [False, False, False]
        endgame_flags = [False, False, True]
        middle, end = _compute_phase_boundaries(midgame_flags, endgame_flags)
        assert middle is None
        assert end is None


class TestPhaseForPly:
    def test_whole_game_opening_when_no_boundaries(self):
        assert _phase_for_ply(1, None, None) == "opening"
        assert _phase_for_ply(50, None, None) == "opening"

    def test_before_middle_is_opening(self):
        assert _phase_for_ply(5, middle_ply=10, end_ply=None) == "opening"

    def test_at_and_after_middle_with_no_end_is_middlegame(self):
        assert _phase_for_ply(10, middle_ply=10, end_ply=None) == "middlegame"
        assert _phase_for_ply(40, middle_ply=10, end_ply=None) == "middlegame"

    def test_between_middle_and_end_is_middlegame(self):
        assert _phase_for_ply(15, middle_ply=10, end_ply=30) == "middlegame"

    def test_at_and_after_end_is_endgame(self):
        assert _phase_for_ply(30, middle_ply=10, end_ply=30) == "endgame"
        assert _phase_for_ply(50, middle_ply=10, end_ply=30) == "endgame"

    def test_end_defined_without_middle_still_reaches_endgame(self):
        # The Divider-discards-middle edge case from
        # TestComputePhaseBoundaries — opening until end, then endgame,
        # skipping middlegame entirely.
        assert _phase_for_ply(1, middle_ply=None, end_ply=5) == "opening"
        assert _phase_for_ply(5, middle_ply=None, end_ply=5) == "endgame"

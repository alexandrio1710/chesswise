"""Unit tests for mistake severity and game-phase classification."""

from mistakes import (
    BLUNDER_THRESHOLD_CP,
    ENDGAME_MOVE_CUTOFF,
    ENDGAME_PIECE_COUNT,
    INACCURACY_THRESHOLD_CP,
    MISTAKE_THRESHOLD_CP,
    OPENING_MOVE_CUTOFF,
    _classify_move,
    classify_phase,
    classify_severity,
)


class TestClassifySeverity:
    def test_below_inaccuracy_threshold_is_not_flagged(self):
        assert classify_severity(0) is None
        assert classify_severity(99) is None

    def test_inaccuracy_band(self):
        assert classify_severity(INACCURACY_THRESHOLD_CP) == "inaccuracy"
        assert classify_severity(150) == "inaccuracy"
        assert classify_severity(MISTAKE_THRESHOLD_CP - 1) == "inaccuracy"

    def test_mistake_band(self):
        assert classify_severity(MISTAKE_THRESHOLD_CP) == "mistake"
        assert classify_severity(300) == "mistake"
        assert classify_severity(BLUNDER_THRESHOLD_CP - 1) == "mistake"

    def test_blunder_band(self):
        assert classify_severity(BLUNDER_THRESHOLD_CP) == "blunder"
        assert classify_severity(1000) == "blunder"
        assert classify_severity(9999) == "blunder"

    def test_negative_drop_not_flagged(self):
        # A move that IMPROVED the position (opponent blundered) should
        # never be flagged as the mover's own mistake.
        assert classify_severity(-500) is None


class TestClassifyMoveMissedMate:
    """Regression test for the exact bug reported live: a move that finds
    a slower mate than the fastest one available — still completely
    winning either way — used to get flagged as a blunder losing
    thousands of centipawns, because analysis.py represents mate scores
    as roughly +-MATE_SCORE_CP (10000) and eval_drop was diffed raw.
    Fixed by capped_eval_drop (stats.ACCURACY_EVAL_CAP_CP).
    """

    def _move(self, **overrides):
        base = {
            "ply": 41, "move_number": 21, "move_san": "Kg1",
            "color_moved": "white", "clock_seconds_remaining": 30,
            "non_king_piece_count": 10,
            "eval_before_cp": 9998, "eval_after_cp": 9950,
        }
        base.update(overrides)
        return base

    def test_missing_the_fastest_mate_in_an_already_winning_position_is_not_flagged(self):
        # Both eval_before and eval_after are near +MATE_SCORE_CP (white
        # mates either way, just a little sooner or later) — practically
        # nothing changed, so this shouldn't be flagged as any mistake.
        assert _classify_move(self._move()) is None

    def test_a_real_winning_to_losing_blunder_is_still_flagged(self):
        move = self._move(eval_before_cp=950, eval_after_cp=-950)
        result = _classify_move(move)
        assert result is not None
        assert result["severity"] == "blunder"


class TestClassifyPhase:
    def test_early_moves_are_opening_regardless_of_material(self):
        assert classify_phase(1, 30) == "opening"
        assert classify_phase(OPENING_MOVE_CUTOFF, 30) == "opening"

    def test_low_material_is_endgame_even_mid_game(self):
        assert classify_phase(OPENING_MOVE_CUTOFF + 1, ENDGAME_PIECE_COUNT - 1) == "endgame"
        assert classify_phase(15, 2) == "endgame"

    def test_long_game_is_endgame_even_with_material_on_board(self):
        # Move 30+ counts as endgame regardless of piece count — a
        # deliberate simplification (see classify_phase's docstring).
        assert classify_phase(ENDGAME_MOVE_CUTOFF, 14) == "endgame"
        assert classify_phase(ENDGAME_MOVE_CUTOFF + 5, 14) == "endgame"

    def test_middlegame_is_the_remaining_band(self):
        mid_move = (OPENING_MOVE_CUTOFF + ENDGAME_MOVE_CUTOFF) // 2
        assert classify_phase(mid_move, ENDGAME_PIECE_COUNT + 5) == "middlegame"

    def test_boundary_at_opening_cutoff(self):
        assert classify_phase(OPENING_MOVE_CUTOFF, 14) == "opening"
        assert classify_phase(OPENING_MOVE_CUTOFF + 1, 14) == "middlegame"

    def test_boundary_at_endgame_piece_count(self):
        assert classify_phase(15, ENDGAME_PIECE_COUNT) == "middlegame"
        assert classify_phase(15, ENDGAME_PIECE_COUNT - 1) == "endgame"

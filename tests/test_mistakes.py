"""Unit tests for mistake severity and game-phase classification."""

from mistakes import (
    BLUNDER_WIN_PERCENT_LOSS,
    ENDGAME_MOVE_CUTOFF,
    ENDGAME_PIECE_COUNT,
    INACCURACY_WIN_PERCENT_LOSS,
    MATE_ADVICE_INACCURACY_CP,
    MATE_ADVICE_MISTAKE_CP,
    MISTAKE_WIN_PERCENT_LOSS,
    OPENING_MOVE_CUTOFF,
    _classify_move,
    _mate_advice_severity,
    _severity_from_win_percent_loss,
    classify_phase,
    classify_severity,
)


class TestSeverityFromWinPercentLoss:
    """The pure threshold lookup — Lichess's own Advice.scala boundaries
    (see mistakes.py's module comment), hand-verifiable in isolation.
    """

    def test_below_inaccuracy_threshold_is_not_flagged(self):
        assert _severity_from_win_percent_loss(0) is None
        assert _severity_from_win_percent_loss(INACCURACY_WIN_PERCENT_LOSS - 0.1) is None

    def test_inaccuracy_band(self):
        assert _severity_from_win_percent_loss(INACCURACY_WIN_PERCENT_LOSS) == "inaccuracy"
        assert _severity_from_win_percent_loss(MISTAKE_WIN_PERCENT_LOSS - 0.1) == "inaccuracy"

    def test_mistake_band(self):
        assert _severity_from_win_percent_loss(MISTAKE_WIN_PERCENT_LOSS) == "mistake"
        assert _severity_from_win_percent_loss(BLUNDER_WIN_PERCENT_LOSS - 0.1) == "mistake"

    def test_blunder_band(self):
        assert _severity_from_win_percent_loss(BLUNDER_WIN_PERCENT_LOSS) == "blunder"
        assert _severity_from_win_percent_loss(50) == "blunder"


class TestClassifySeverity:
    """classify_severity(eval_before_cp, eval_after_cp) — win-probability-
    based (Lichess's own CpAdvice), not raw centipawns: see mistakes.py's
    module comment above INACCURACY_WIN_PERCENT_LOSS for why a flat cp
    threshold doesn't distinguish a real swing from one that doesn't
    change who's winning.
    """

    def test_no_change_is_not_flagged(self):
        assert classify_severity(0, 0) is None
        assert classify_severity(300, 300) is None

    def test_a_small_swing_near_dead_equal_is_not_flagged(self):
        # A few centipawns of engine wobble between similar quiet moves.
        assert classify_severity(20, 10) is None

    def test_a_large_swing_from_equal_to_lost_is_a_blunder(self):
        assert classify_severity(0, -600) == "blunder"

    def test_a_move_that_improves_the_position_is_not_flagged(self):
        # The opponent's prior move was itself weak — never the mover's
        # own mistake to flag.
        assert classify_severity(0, 200) is None

    def test_a_bigger_loss_is_never_a_milder_verdict_than_a_smaller_one(self):
        small = classify_severity(0, -20)
        big = classify_severity(0, -600)
        order = {None: 0, "inaccuracy": 1, "mistake": 2, "blunder": 3}
        assert order[big] >= order[small]


class TestMateAdviceSeverity:
    """The special case Lichess's own MateAdvice adds on top of the
    ordinary win%-based judgment: a move that specifically creates a
    forced mate against the mover, or loses one they already had — graded
    by the eval right before (creating one) or right after (losing one)
    rather than the win%-loss, since win% is already saturated for any
    mate-scale eval.
    """

    def test_walking_into_mate_from_a_winning_position_is_a_blunder(self):
        assert _mate_advice_severity(500, -9998) == "blunder"

    def test_walking_into_mate_from_a_moderately_lost_position_is_a_mistake(self):
        assert _mate_advice_severity(-800, -9998) == "mistake"

    def test_walking_into_mate_from_an_already_crushed_position_is_an_inaccuracy(self):
        assert _mate_advice_severity(-1200, -9998) == "inaccuracy"

    def test_losing_your_own_forced_mate_but_still_crushing_is_an_inaccuracy(self):
        assert _mate_advice_severity(9998, 1200) == "inaccuracy"

    def test_losing_your_own_forced_mate_down_to_a_smaller_edge_is_a_mistake(self):
        assert _mate_advice_severity(9998, 800) == "mistake"

    def test_losing_your_own_forced_mate_entirely_is_a_blunder(self):
        assert _mate_advice_severity(9998, 100) == "blunder"

    def test_a_mate_found_slower_than_the_fastest_available_is_not_a_special_case(self):
        # Still winning either way (mate either way) — this falls through
        # to the ordinary win%-based path instead, which is already
        # saturated at the mate-detection ceiling (see ClassifyMoveMissedMate).
        assert _mate_advice_severity(9998, 9950) is None

    def test_boundary_values_match_lichess_own_thresholds(self):
        assert MATE_ADVICE_INACCURACY_CP == 999
        assert MATE_ADVICE_MISTAKE_CP == 700


class TestClassifyMoveMissedMate:
    """Regression test for the exact bug reported live: a move that finds
    a slower mate than the fastest one available — still completely
    winning either way — used to get flagged as a blunder losing
    thousands of centipawns, because analysis.py represents mate scores
    as roughly +-MATE_SCORE_CP (10000) and eval_drop was diffed raw.
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

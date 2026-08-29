"""Unit tests for puzzles.py's uniqueness gate — a direct port of Lichess's
own real puzzle generator's is_valid_attack threshold:
https://github.com/ornicar/lichess-puzzler/blob/master/generator/generator.py

Our own generator previously had no uniqueness check at all: any flagged
mistake/blunder became a "find the best move" puzzle regardless of
whether a second move was nearly as good, which makes for a frustrating,
arguably-wrong-to-mark-incorrect puzzle when it happens.
"""

from puzzles import _line_eval_cp, has_unique_solution


def _line(eval_cp=None, mate_in=None):
    return {"eval_cp": eval_cp, "mate_in": mate_in}


class TestLineEvalCp:
    def test_a_plain_cp_line_returns_its_own_value(self):
        assert _line_eval_cp(_line(eval_cp=150)) == 150

    def test_a_positive_mate_line_is_a_large_positive_value(self):
        assert _line_eval_cp(_line(mate_in=3)) > 1000

    def test_a_negative_mate_line_is_a_large_negative_value(self):
        assert _line_eval_cp(_line(mate_in=-3)) < -1000


class TestHasUniqueSolution:
    def test_no_second_line_is_always_unique(self):
        assert has_unique_solution([_line(eval_cp=50)]) is True

    def test_a_much_better_best_move_is_unique(self):
        # A real blunder-punishing tactic: the best move wins big, the
        # runner-up is only slightly better than doing nothing.
        lines = [_line(eval_cp=800), _line(eval_cp=20)]
        assert has_unique_solution(lines) is True

    def test_two_similarly_good_moves_is_not_unique(self):
        # Both moves keep a similar, moderate edge — a solver playing
        # either one is playing about as well as the other.
        lines = [_line(eval_cp=120), _line(eval_cp=90)]
        assert has_unique_solution(lines) is False

    def test_two_mate_in_ones_is_not_unique(self):
        # Either move mates immediately — genuinely ambiguous, not a
        # "find the one right move" puzzle.
        lines = [_line(mate_in=1), _line(mate_in=1)]
        assert has_unique_solution(lines) is False

    def test_a_forced_mate_vs_a_roughly_equal_alternative_is_unique(self):
        lines = [_line(mate_in=2), _line(eval_cp=20)]
        assert has_unique_solution(lines) is True

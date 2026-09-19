import chess
import pytest

import explain

FORK_FEN = "3q1k2/8/8/2N5/8/8/8/4K3 w - - 0 1"


def _explain(fen, uci, classification, **kw):
    board = chess.Board(fen)
    defaults = dict(eval_before_cp=0, eval_after_cp=0, best_uci=None, best_pv=None, best_mate_in=None)
    defaults.update(kw)
    return explain.explain_move(board, chess.Move.from_uci(uci), classification=classification, **defaults)


class TestDescribeMoveEffect:
    def test_fork_names_both_targets(self):
        board = chess.Board(FORK_FEN)
        effect = explain.describe_move_effect(board, chess.Move.from_uci("c5e6"))
        assert effect == "forks the queen and the king"

    def test_pin_names_the_pinned_piece_and_what_it_shields(self):
        board = chess.Board("4k3/3n4/8/8/2B5/8/8/4K3 w - - 0 1")
        assert explain.describe_move_effect(board, chess.Move.from_uci("c4b5")) == "pins the knight to the king"

    def test_skewer(self):
        board = chess.Board("8/5r2/8/3k4/8/8/B7/4K3 w - - 0 1")
        assert explain.describe_move_effect(board, chess.Move.from_uci("a2b3")) == "skewers the king and the rook"

    def test_discovered_attack(self):
        board = chess.Board("8/4q2k/8/8/8/4N3/8/4R1K1 w - - 0 1")
        assert explain.describe_move_effect(board, chess.Move.from_uci("e3c4")) == "uncovers an attack on the queen"

    def test_mate_patterns_are_named(self):
        assert explain.describe_move_effect(
            chess.Board("6k1/5ppp/8/8/8/8/8/R3K3 w - - 0 1"), chess.Move.from_uci("a1a8")) == "delivers a back-rank mate"
        assert explain.describe_move_effect(
            chess.Board("6rk/6pp/8/6N1/8/8/8/4K3 w - - 0 1"), chess.Move.from_uci("g5f7")) == "delivers a smothered mate"

    def test_forced_mate_line_is_reported_by_distance(self):
        board = chess.Board("6k1/5ppp/8/8/8/8/8/R3K3 w - - 0 1")
        assert explain.describe_move_effect(board, chess.Move.from_uci("e1d2"), mate_in=3) == "leads to a forced mate in 3"

    def test_taking_an_undefended_piece(self):
        board = chess.Board("4k3/8/8/3b4/8/8/8/3QK3 w - - 0 1")
        assert explain.describe_move_effect(board, chess.Move.from_uci("d1d5")) == "captures the undefended bishop"

    def test_a_capture_that_wins_material_in_the_line_says_so(self):
        # Bxc3 bxc3 looks like an even trade, but the line goes on to win a piece.
        board = chess.Board("4k3/8/8/8/8/2n5/1P6/4K3 w - - 0 1")
        assert explain.describe_move_effect(board, chess.Move.from_uci("b2b3")) is None

    def test_quiet_move_has_no_effect(self):
        assert explain.describe_move_effect(chess.Board(), chess.Move.from_uci("a2a3")) is None

    def test_castling(self):
        board = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        assert explain.describe_move_effect(board, chess.Move.from_uci("e1g1")) == "castles to safety"


class TestExplainMove:
    def test_headline_uses_san_and_the_class_label(self):
        result = _explain(chess.STARTING_FEN, "e2e4", "book", opening_name="King's Pawn Game")
        assert result["headline"] == "e4 is a book move"
        assert "King's Pawn Game" in result["text"]

    def test_best_move_says_what_it_does(self):
        result = _explain(FORK_FEN, "c5e6", "best", best_uci="c5e6", best_pv=["c5e6", "f8e8", "e6d8", "e8d8"])
        assert result["headline"] == "Ne6+ is the best move"
        assert "forks the queen and the king" in result["text"]
        assert result["played_is_best"] is True

    def test_excellent_move_points_at_the_better_alternative(self):
        result = _explain(FORK_FEN, "e1f2", "excellent", best_uci="c5e6", best_pv=["c5e6", "f8e8", "e6d8", "e8d8"])
        assert result["headline"] == "Kf2 is an excellent move"
        assert "The best move was Ne6+, which forks the queen and the king." in result["text"]

    def test_missing_a_forced_mate_says_so(self):
        result = _explain("6k1/5ppp/8/8/8/8/8/R3K3 w - - 0 1", "e1d2", "miss", eval_before_cp=9999, eval_after_cp=300,
                          best_uci="a1a8", best_pv=["a1a8"], best_mate_in=1)
        assert "forced mate in 1 with Ra8#" in result["text"]
        assert result["hint_kind"] == "checkmate"

    def test_allowing_mate_names_the_refutation(self):
        # Scholar's mate: after 3...d6?? White has Qxf7#.
        board = chess.Board()
        for uci in ("e2e4", "e7e5", "f1c4", "b8c6", "d1h5"):
            board.push_uci(uci)
        result = _explain(board.fen(), "d7d6", "blunder", eval_before_cp=-50, eval_after_cp=-9999,
                          best_uci="g8f6", best_pv=["g8f6"], reply_uci="h5f7", reply_pv=["h5f7"], reply_mate_in=1)
        assert result["headline"] == "d6 is a blunder"
        assert "This allows Qxf7#, and a forced mate in 1." in result["text"]

    def test_losing_a_piece_names_what_is_lost(self):
        # ...Qd5 hangs the queen to a pawn.
        result = _explain("4k3/8/2p5/8/8/8/8/3QK3 w - - 0 1", "d1d5", "blunder", eval_before_cp=900, eval_after_cp=0,
                          best_uci="d1d2", best_pv=["d1d2"], reply_uci="c6d5", reply_pv=["c6d5"])
        assert "This allows cxd5, winning your queen." in result["text"]
        assert "The best move was Qd2." in result["text"]

    def test_a_hanging_piece_is_called_out_when_no_reply_data_exists(self):
        result = _explain("4k3/8/2p5/8/8/8/8/3QK3 w - - 0 1", "d1d5", "mistake", eval_before_cp=900, eval_after_cp=0)
        assert "Your queen on d5 is left hanging." in result["text"]

    def test_falls_back_to_the_eval_change_when_it_cannot_say_why(self):
        result = _explain(chess.STARTING_FEN, "a2a3", "inaccuracy", eval_before_cp=30, eval_after_cp=-40)
        assert result["text"] == "This drops your evaluation from +0.30 to -0.40."

    def test_a_miss_adds_the_lost_win_framing(self):
        result = _explain("4k3/8/2p5/8/8/8/8/3QK3 w - - 0 1", "d1d5", "miss", eval_before_cp=900, eval_after_cp=0,
                          reply_uci="c6d5", reply_pv=["c6d5"])
        assert result["text"].startswith("You had a winning position and let it slip.")

    def test_brilliant_mentions_the_sacrifice(self):
        # Qd5 walks into a pawn capture: a real sacrifice, and the engine says it is best.
        result = _explain("4k3/8/2p5/8/8/8/8/3QK3 w - - 0 1", "d1d5", "brilliant", best_uci="d1d5")
        assert result["headline"] == "Qd5 is brilliant"
        assert "sacrificed material" in result["text"]

    def test_great_move_context_depends_on_the_position(self):
        winning = _explain(chess.STARTING_FEN, "e2e4", "great", eval_before_cp=300)
        losing = _explain(chess.STARTING_FEN, "e2e4", "great", eval_before_cp=-300)
        assert "keep it" in winning["text"]
        assert "stay in the game" in losing["text"]

    def test_evals_are_formatted_from_the_movers_side(self):
        result = _explain(chess.STARTING_FEN, "e2e4", "best", eval_before_cp=25, eval_after_cp=-1)
        assert (result["eval_before"], result["eval_after"]) == ("+0.25", "-0.01")


class TestFormatting:
    @pytest.mark.parametrize("cp,mate,expected", [
        (120, None, "+1.20"), (-45, None, "-0.45"), (None, 3, "mate in 3"), (None, -2, "getting mated in 2"),
        (9990, None, "a forced mate"), (-9990, None, "getting mated"), (None, None, "?"),
    ])
    def test_fmt_eval(self, cp, mate, expected):
        assert explain.fmt_eval(cp, mate) == expected

    @pytest.mark.parametrize("swing,expected", [
        (1, "wins a pawn"), (2, "wins two pawns"), (3, "wins a piece"), (5, "wins a rook"), (9, "wins the queen"),
    ])
    def test_wins_phrase(self, swing, expected):
        assert explain.wins_phrase(swing) == expected

    @pytest.mark.parametrize("accuracy,grade", [
        (97, "best"), (88, "excellent"), (78, "good"), (65, "inaccuracy"), (50, "mistake"), (30, "blunder"), (None, None),
    ])
    def test_phase_grades(self, accuracy, grade):
        assert explain.grade_from_accuracy(accuracy) == grade


class TestGameSummary:
    def test_summary_covers_accuracy_highlights_errors_and_turning_point(self):
        text = explain.game_summary(
            you="white", accuracy={"white": 81.2, "black": 70.0},
            class_counts={"white": {"brilliant": 1, "great": 2, "blunder": 1, "mistake": 2, "miss": 1}},
            weakest_phase="endgame", turning_point={"move_number": 23, "san": "Qxb7", "label": "a blunder"}, opening=None,
        )
        assert "You played White with 81.2% accuracy (your opponent: 70.0%)." in text
        assert "1 brilliant move and 2 great moves" in text
        assert "1 blunder, 2 mistakes and 1 miss" in text
        assert "move 23 (Qxb7), a blunder" in text
        assert "endgame was your weakest phase" in text

    def test_a_clean_game_says_so(self):
        text = explain.game_summary(you="black", accuracy={"white": 60, "black": 92}, class_counts={"black": {}},
                                    weakest_phase=None, turning_point=None, opening=None)
        assert "clean game" in text

import chess
import pytest

import tactics


def _motifs(fen: str, uci: str) -> set[str]:
    board = chess.Board(fen)
    return tactics.move_motifs(board, chess.Move.from_uci(uci))


class TestFork:
    def test_knight_forking_king_and_queen(self):
        # Ne6+ hits the king on f8 and the queen on d8; nothing can take the knight.
        assert {"fork", "check"} <= _motifs("3q1k2/8/8/2N5/8/8/8/4K3 w - - 0 1", "c5e6")

    def test_a_defended_forking_piece_that_gets_captured_is_not_a_fork(self):
        # Same fork square, but a black pawn on f7 simply takes the knight.
        assert "fork" not in _motifs("3q1k2/5p2/8/2N5/8/8/8/4K3 w - - 0 1", "c5e6")

    def test_quiet_move_has_no_fork(self):
        assert "fork" not in _motifs(chess.STARTING_FEN, "g1f3")


class TestPinAndSkewer:
    def test_absolute_pin_against_the_king(self):
        assert "pin" in _motifs("4k3/3n4/8/8/2B5/8/8/4K3 w - - 0 1", "c4b5")

    def test_relative_pin_against_a_queen(self):
        assert "pin" in _motifs("4q2k/3n4/8/8/2B5/8/8/4K3 w - - 0 1", "c4b5")

    def test_skewer_king_in_front_of_a_rook(self):
        motifs = _motifs("8/5r2/8/3k4/8/8/B7/4K3 w - - 0 1", "a2b3")
        assert "skewer" in motifs and "check" in motifs
        assert "pin" not in motifs


class TestDiscovered:
    def test_knight_move_uncovers_rook_attack_on_queen(self):
        assert "discovered_attack" in _motifs("8/4q2k/8/8/8/4N3/8/4R1K1 w - - 0 1", "e3c4")

    def test_the_moved_piece_itself_is_not_a_discovery(self):
        assert "discovered_attack" not in _motifs("8/4q2k/8/8/8/8/8/4R1K1 w - - 0 1", "e1e6")


class TestHangingPieces:
    def test_undefended_piece_attacked_by_a_pawn(self):
        board = chess.Board("4k3/8/8/3n4/4P3/8/8/4K3 w - - 0 1")
        assert tactics.hanging_pieces(board, chess.BLACK) == [chess.D5]

    def test_defended_piece_attacked_by_a_cheaper_piece_is_still_hanging(self):
        board = chess.Board("4k3/8/2p5/3n4/4P3/8/8/4K3 w - - 0 1")
        assert tactics.hanging_pieces(board, chess.BLACK) == [chess.D5]

    def test_defended_piece_attacked_by_an_equal_piece_is_not_hanging(self):
        board = chess.Board("4k3/8/2p5/3n4/8/2N5/8/4K3 w - - 0 1")
        assert tactics.hanging_pieces(board, chess.BLACK) == []

    def test_pawns_are_not_counted_as_pieces(self):
        board = chess.Board("4k3/8/8/3p4/4P3/8/8/4K3 w - - 0 1")
        assert tactics.hanging_pieces(board, chess.BLACK) == []

    def test_capturing_an_undefended_piece_is_a_free_piece(self):
        assert "hanging_piece" in _motifs("4k3/8/8/3b4/8/8/8/3QK3 w - - 0 1", "d1d5")

    def test_capturing_a_defended_piece_is_not_free(self):
        assert "hanging_piece" not in _motifs("4k3/8/2p5/3b4/8/8/8/3QK3 w - - 0 1", "d1d5")


class TestMates:
    def test_back_rank_mate(self):
        motifs = _motifs("6k1/5ppp/8/8/8/8/8/R3K3 w - - 0 1", "a1a8")
        assert {"mate", "back_rank_mate", "check"} <= motifs

    def test_smothered_mate(self):
        motifs = _motifs("6rk/6pp/8/6N1/8/8/8/4K3 w - - 0 1", "g5f7")
        assert {"mate", "smothered_mate"} <= motifs

    def test_ordinary_mate_has_no_pattern_name(self):
        # Fool's mate: Qh4# — mate, but neither back-rank nor smothered.
        motifs = _motifs("rnbqkbnr/pppp1ppp/8/4p3/6P1/5P2/PPPPP2P/RNBQKBNR b KQkq - 0 2", "d8h4")
        assert "mate" in motifs
        assert not motifs & {"back_rank_mate", "smothered_mate"}

    def test_mate_pattern_is_none_when_not_mate(self):
        assert tactics.mate_pattern(chess.Board()) is None


class TestOtherMotifs:
    def test_promotion(self):
        assert "promotion" in _motifs("8/P7/8/8/8/8/k6K/8 w - - 0 1", "a7a8q")

    def test_moving_a_hanging_piece_to_safety_is_defensive(self):
        assert "defensive" in _motifs("4k3/8/8/3n4/4P3/8/8/4K3 b - - 0 1", "d5b4")

    def test_a_move_that_ignores_the_hanging_piece_is_not_defensive(self):
        assert "defensive" not in _motifs("4k3/8/8/3n4/4P3/8/8/4K3 b - - 0 1", "e8d7")


class TestPuzzleThemes:
    def test_themes_follow_the_theme_key_order(self):
        themes = tactics.puzzle_themes("3q1k2/8/8/2N5/8/8/8/4K3 w - - 0 1", "c5e6")
        assert "fork" in themes

    def test_mate_in_n_theme_is_added_first(self):
        themes = tactics.puzzle_themes("6k1/5ppp/8/8/8/8/8/R3K3 w - - 0 1", "a1a8", mate_in=1)
        assert themes[0] == "mate_in_1"
        assert "mate" in themes and "back_rank_mate" in themes

    def test_illegal_or_garbage_move_yields_no_themes(self):
        assert tactics.puzzle_themes(chess.STARTING_FEN, "e2e5") == []
        assert tactics.puzzle_themes(chess.STARTING_FEN, "nonsense") == []

    @pytest.mark.parametrize("key,label", [
        ("fork", "Fork"), ("mate_in_3", "Mate in 3"), ("back_rank_mate", "Back-rank mate"), ("unknown_thing", "Unknown thing"),
    ])
    def test_theme_labels(self, key, label):
        assert tactics.theme_label(key) == label


class TestMaterial:
    def test_balance_counts_pieces_not_kings(self):
        # White is a rook up.
        board = chess.Board("4k3/8/8/8/8/8/8/R3K3 w - - 0 1")
        assert tactics.material_balance(board, chess.WHITE) == 5
        assert tactics.material_balance(board, chess.BLACK) == -5

    def test_swing_of_a_line_that_wins_a_queen_for_a_knight(self):
        # Ne6+ Ke8 Nxd8 Kxd8: White wins the queen (9) and loses the knight (3).
        swing = tactics.pv_material_swing(
            "3q1k2/8/8/2N5/8/8/8/4K3 w - - 0 1", ["c5e6", "f8e8", "e6d8", "e8d8"], chess.WHITE,
        )
        assert swing == 6

    def test_swing_stops_at_an_illegal_move(self):
        assert tactics.pv_material_swing(chess.STARTING_FEN, ["e2e4", "e2e4"], chess.WHITE) == 0

    def test_swing_is_from_the_movers_point_of_view(self):
        line = ["c5e6", "f8e8", "e6d8", "e8d8"]
        fen = "3q1k2/8/8/2N5/8/8/8/4K3 w - - 0 1"
        assert tactics.pv_material_swing(fen, line, chess.BLACK) == -6

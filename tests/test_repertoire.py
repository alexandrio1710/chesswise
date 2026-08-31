"""Unit tests for repertoire.py's structural detectors — each hand-authored
heuristic (no Stockfish, no port of any external source) that tags which
of this player's known repertoire structures a game reached, from the PGN
alone. Detectors are tested directly against constructed FENs/SAN move
lists (precise and simple) rather than full legal PGN sequences; a couple
of lighter tests exercise the top-level detect_structure(pgn_text, ...)
wrapper end-to-end.
"""

import chess

import repertoire


class TestHasIsolatedPawn:
    def test_a_pawn_with_a_neighbor_is_not_isolated(self):
        board = chess.Board("4k3/8/8/8/3PP3/8/8/4K3 w - - 0 1")  # d4 + e4
        assert repertoire._has_isolated_pawn(board, chess.WHITE, chess.square_file(chess.D4)) is False

    def test_a_pawn_with_no_neighbor_on_either_file_is_isolated(self):
        board = chess.Board("4k3/8/8/8/3P4/8/6P1/4K3 w - - 0 1")  # d4, g2 only — no c/e-file pawn
        assert repertoire._has_isolated_pawn(board, chess.WHITE, chess.square_file(chess.D4)) is True

    def test_no_pawn_on_that_file_at_all_is_not_isolated(self):
        board = chess.Board("4k3/8/8/8/8/8/8/4K3 w - - 0 1")
        assert repertoire._has_isolated_pawn(board, chess.WHITE, chess.square_file(chess.D4)) is False


class TestCastlingSides:
    def test_detects_both_sides_castling_the_same_way(self):
        sides = repertoire._castling_sides(["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5", "O-O", "O-O"])
        assert sides == {"white": "kingside", "black": "kingside"}

    def test_detects_opposite_castling(self):
        sides = repertoire._castling_sides(["d4", "Nf6", "c4", "g6", "Nc3", "Bg7", "e4", "d6",
                                             "f3", "O-O", "Be3", "Nc6", "Qd2", "a6", "O-O-O"])
        assert sides == {"white": "queenside", "black": "kingside"}

    def test_no_castling_at_all(self):
        assert repertoire._castling_sides(["e4", "e5", "Nf3", "Nc6"]) == {"white": None, "black": None}


class TestPawnStormMoveCount:
    def test_counts_only_the_given_colors_pawn_moves_on_the_given_files(self):
        # White: f3 (f-file, counts), Nc3 (piece, doesn't count).
        # Black: g5 (g-file, counts), Nf6 (piece, doesn't count).
        moves = ["f3", "Nf6", "Nc3", "g5"]
        assert repertoire._pawn_storm_move_count(moves, "white", "fgh") == 1
        assert repertoire._pawn_storm_move_count(moves, "black", "fgh") == 1

    def test_ignores_pawn_moves_on_files_not_asked_about(self):
        assert repertoire._pawn_storm_move_count(["e4"], "white", "abc") == 0

    def test_handles_captures_and_promotions(self):
        # exd5 lands on the d-file; e8=Q lands on the e-file.
        assert repertoire._pawn_storm_move_count(["exd5"], "white", "d") == 1
        assert repertoire._pawn_storm_move_count(["e8=Q"], "white", "e") == 1
        assert repertoire._pawn_storm_move_count(["exd8=Q"], "white", "d") == 1

    def test_ignores_castling_tokens(self):
        assert repertoire._pawn_storm_move_count(["O-O", "O-O-O"], "white", "abcdefgh") == 0


class TestDetectLondon:
    def test_non_london_eco_name_returns_none(self):
        board = chess.Board()
        assert repertoire._detect_london(board, "Sicilian Defense") is None

    def test_no_eco_name_returns_none(self):
        board = chess.Board()
        assert repertoire._detect_london(board, None) is None

    def test_symmetric_structure_when_d_pawn_has_a_neighbor(self):
        board = chess.Board("4k3/8/8/8/3PP3/2P5/8/4K3 w - - 0 1")
        match = repertoire._detect_london(board, "London System")
        assert match["structure"] == "london_symmetric"
        assert match["checklist"] == repertoire.LONDON_CHECKLIST

    def test_iqp_structure_when_d_pawn_is_isolated(self):
        board = chess.Board("4k3/8/8/8/3P4/8/6P1/4K3 w - - 0 1")
        match = repertoire._detect_london(board, "London System")
        assert match["structure"] == "london_iqp"


class TestDetectDragon:
    _SAN = ["d4", "Nf6", "c4", "g6", "Nc3", "Bg7", "e4", "d6",
            "f3", "O-O", "Be3", "Nc6", "Qd2", "a6", "O-O-O"]

    def test_non_dragon_eco_name_returns_none(self):
        board = chess.Board()
        assert repertoire._detect_dragon(board, self._SAN, "Caro-Kann Defense") is None

    def test_opposite_castling_is_flagged_with_a_race_note(self):
        board = chess.Board()
        match = repertoire._detect_dragon(board, self._SAN, "Sicilian Defense: Dragon Variation")
        assert match["structure"] == "dragon_opposite_castling"
        assert any("tempo count" in item for item in match["checklist"])

    def test_same_side_castling_is_not_flagged_as_opposite(self):
        san = ["e4", "c5", "Nf3", "d6", "d4", "cxd4", "Nxd4", "Nf6",
               "Nc3", "g6", "Be2", "Bg7", "O-O", "O-O"]
        match = repertoire._detect_dragon(chess.Board(), san, "Sicilian Defense: Dragon Variation")
        assert match["structure"] == "dragon_same_side"

    def test_rxc3_gets_its_own_checklist_note(self):
        san = self._SAN + ["Bd7", "h4", "Rc8", "h5", "Rxc3"]
        match = repertoire._detect_dragon(chess.Board(), san, "Sicilian Defense: Dragon Variation")
        assert any("actually played ...Rxc3" in item for item in match["checklist"])

    def test_no_rxc3_does_not_get_that_note(self):
        match = repertoire._detect_dragon(chess.Board(), self._SAN, "Sicilian Defense: Dragon Variation")
        assert not any("actually played ...Rxc3" in item for item in match["checklist"])


class TestDetectKingsIndian:
    def test_non_kid_eco_name_returns_none(self):
        board = chess.Board()
        assert repertoire._detect_kings_indian(board, [], "Grünfeld Defense") is None

    def test_locked_mar_del_plata_center_is_detected(self):
        board = chess.Board("4k3/8/3p4/3PpP2/8/8/8/4K3 w - - 0 1")  # White d5, Black d6+e5
        match = repertoire._detect_kings_indian(board, ["f5"], "King's Indian Defense")
        assert match["structure"] == "kid_mar_del_plata"
        assert any("tempo count" in item for item in match["checklist"])

    def test_no_locked_center_is_tagged_as_kid_other(self):
        board = chess.Board()
        match = repertoire._detect_kings_indian(board, [], "King's Indian Defense")
        assert match["structure"] == "kid_other"


class TestDetectGrunfeld:
    def test_non_grunfeld_eco_name_returns_none(self):
        board = chess.Board()
        assert repertoire._detect_grunfeld(board, "King's Indian Defense") is None

    def test_exchange_structure_when_white_has_c4_d4_and_black_has_no_d_pawn(self):
        board = chess.Board("4k3/ppp1pppp/8/8/2PP4/8/PP2PPPP/4K3 w - - 0 1")  # black's d-pawn is gone
        match = repertoire._detect_grunfeld(board, "Grünfeld Defense: Exchange Variation")
        assert match["structure"] == "grunfeld_exchange"

    def test_intact_black_d_pawn_is_not_exchange_structure(self):
        board = chess.Board()  # starting position: Black still has a d-pawn
        match = repertoire._detect_grunfeld(board, "Grünfeld Defense")
        assert match["structure"] == "grunfeld_other"


class TestDetectStructureEndToEnd:
    def test_a_real_pgn_gets_tagged_via_the_wrapper(self):
        pgn = """[Event "?"]

1. d4 d5 2. Nf3 Nf6 3. Bf4 e6 4. e3 Bd6 5. Bg3 O-O 6. Nbd2 c5 *
"""
        match = repertoire.detect_structure(pgn, "London System")
        assert match is not None
        assert match["structure"] in ("london_symmetric", "london_iqp")

    def test_a_game_matching_no_repertoire_structure_returns_none(self):
        pgn = """[Event "?"]

1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 *
"""
        assert repertoire.detect_structure(pgn, "Ruy Lopez") is None

    def test_no_eco_name_returns_none_regardless_of_pgn_quality(self):
        # python-chess's PGN reader is lenient about garbage text (it just
        # yields a game with no moves, not None) — with no eco_name at all,
        # none of the four detectors can match regardless.
        assert repertoire.detect_structure("not a pgn at all", None) is None

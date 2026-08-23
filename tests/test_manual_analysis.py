"""Tests for the FEN/PGN data manual_analysis.py adds to power the Analyze
Board's interactive board (analyze_fen's per-line fen_after, analyze_pgn_
oneoff's start_fen/fen_after per move). Stockfish calls are monkeypatched
out — these are pure chess-logic additions, not evaluation logic, and the
happy path is already verified live against the real engine in-browser.
"""

import manual_analysis


class TestAnalyzeFenIncludesResultingPosition:
    def test_each_top_line_carries_its_resulting_fen(self, monkeypatch):
        monkeypatch.setattr(
            manual_analysis, "get_top_lines",
            lambda fen, depth=None, num_lines=3: [
                {"move_uci": "e7e5", "move_san": "e5", "eval_cp": -30, "mate_in": None},
                {"move_uci": "c7c5", "move_san": "c5", "eval_cp": -45, "mate_in": None},
            ],
        )
        result = manual_analysis.analyze_fen("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1")

        assert result["top_lines"][0]["fen_after"] == "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
        assert result["top_lines"][1]["fen_after"] == "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"

    def test_invalid_fen_still_raises_value_error(self):
        try:
            manual_analysis.analyze_fen("not a fen")
            assert False, "expected ValueError"
        except ValueError:
            pass


class TestAnalyzePgnOneoffIncludesBoardTrace:
    _SAMPLE_PGN = (
        '[Event "Test"]\n[White "A"]\n[Black "B"]\n[Result "1-0"]\n\n'
        "1. e4 e5 2. Nf3 1-0\n"
    )

    def _fake_graded_move(self, ply, move_number, color, san, eval_cp):
        return {
            "ply": ply, "move_number": move_number, "color_moved": color, "move_san": san,
            "eval_cp": eval_cp, "eval_before_cp": 0, "eval_drop": 0, "tier": "best",
        }

    def test_start_fen_and_per_move_fen_after_are_present(self, monkeypatch):
        monkeypatch.setattr(
            manual_analysis, "_graded_moves",
            lambda pgn_text, depth: [
                self._fake_graded_move(1, 1, "white", "e4", 30),
                self._fake_graded_move(2, 1, "black", "e5", 25),
                self._fake_graded_move(3, 2, "white", "Nf3", 35),
            ],
        )
        result = manual_analysis.analyze_pgn_oneoff(self._SAMPLE_PGN)

        assert result["start_fen"] == "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        assert result["moves"][0]["fen_after"] == "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
        assert result["moves"][2]["fen_after"] == "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"
        # annotate_fen must not disturb the fields analyze_pgn_oneoff's own
        # callers (the frontend's move list/eval chart) already depend on.
        assert result["moves"][0]["eval_cp"] == 30

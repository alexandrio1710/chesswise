"""Tests for the FEN/PGN data manual_analysis.py adds to power the Analyze
Board's interactive board (analyze_fen's per-line fen_after, analyze_pgn_
oneoff's start_fen/fen_after per move). Stockfish calls are monkeypatched
out — these are pure chess-logic additions, not evaluation logic, and the
happy path is already verified live against the real engine in-browser.
"""

import itertools

import manual_analysis
import mistakes

_profile_id_counter = itertools.count(1)
_SAVE_PGN = (
    '[Event "Test"]\n[White "A"]\n[Black "B"]\n[Result "1-0"]\n\n'
    "1. e4 e5 2. Nf3 Nc6 1-0\n"
)


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

    def test_includes_legal_moves_for_the_interactive_board(self, monkeypatch):
        monkeypatch.setattr(manual_analysis, "get_top_lines", lambda fen, depth=None, num_lines=3: [])
        result = manual_analysis.analyze_fen("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1")
        assert {"from": "e7", "to": "e5"} in result["legal_moves"]
        assert {"from": "g8", "to": "f6"} in result["legal_moves"]
        # It's Black to move — White has no legal moves in this position.
        assert not any(m["from"] == "e2" for m in result["legal_moves"])


class TestApplyMove:
    """Backs the Analyze board's click/drag-to-move interactivity — same
    "server does chess logic" split as analyze_fen's fen_after: the client
    never validates a move itself, just sends from/to (and an optional
    promotion piece) and renders whatever FEN comes back.
    """

    _START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

    def test_a_legal_move_returns_the_resulting_fen_and_san(self):
        result = manual_analysis.apply_move(self._START, "e2", "e4")
        # No en passant target recorded: python-chess only sets it when a
        # legal en passant capture actually exists, and Black has no pawn
        # on d5/f5 here to make one.
        assert result["fen"] == "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
        assert result["san"] == "e4"
        assert result["uci"] == "e2e4"
        assert result["is_check"] is False
        assert result["is_checkmate"] is False
        assert result["is_game_over"] is False

    def test_an_illegal_move_raises_value_error(self):
        try:
            manual_analysis.apply_move(self._START, "e2", "e5")  # too far, blocked
            assert False, "expected ValueError"
        except ValueError:
            pass

    def test_moving_the_wrong_colors_piece_raises_value_error(self):
        try:
            manual_analysis.apply_move(self._START, "e7", "e5")  # Black, but White to move
            assert False, "expected ValueError"
        except ValueError:
            pass

    def test_invalid_fen_raises_value_error(self):
        try:
            manual_analysis.apply_move("not a fen", "e2", "e4")
            assert False, "expected ValueError"
        except ValueError:
            pass

    def test_promotion_defaults_to_queen_when_unspecified(self):
        result = manual_analysis.apply_move("8/4P3/8/8/8/8/8/k6K w - - 0 1", "e7", "e8")
        assert result["uci"] == "e7e8q"
        assert result["san"] == "e8=Q"
        assert result["fen"] == "4Q3/8/8/8/8/8/8/k6K b - - 0 1"

    def test_explicit_underpromotion_is_respected(self):
        result = manual_analysis.apply_move("8/4P3/8/8/8/8/8/k6K w - - 0 1", "e7", "e8", promotion="n")
        assert result["uci"] == "e7e8n"
        assert result["san"] == "e8=N"

    def test_checkmate_is_flagged(self):
        # Scholar's mate, final move: Qxf7#
        result = manual_analysis.apply_move(
            "r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4", "h5", "f7",
        )
        assert result["is_checkmate"] is True
        assert result["is_check"] is True
        assert result["is_game_over"] is True


class TestSaveManualGameProfile:
    """The Analyze board now has its own profile switcher (previously it
    had none, so every manually-saved game landed under whichever profile
    was created first regardless of what the user actually meant) —
    save_manual_game takes the selected profile_id straight through
    instead of always falling back to profiles.default_profile_id().
    analyze_and_store_game is monkeypatched out so this doesn't need a
    real Stockfish engine — only where the saved row's profile_id lands
    is under test here.
    """

    def _saved_game_profile_id(self, game_id: int) -> int | None:
        from db import get_connection

        conn = get_connection()
        try:
            return conn.execute("SELECT profile_id FROM games WHERE id = ?", (game_id,)).fetchone()["profile_id"]
        finally:
            conn.close()

    def test_given_profile_id_is_used_over_the_default(self, monkeypatch):
        monkeypatch.setattr(mistakes, "analyze_and_store_game", lambda game_id, pgn: None)
        from profiles import create_profile

        profile = create_profile(f"save-test-profile-{next(_profile_id_counter)}")
        game_id = manual_analysis.save_manual_game(_SAVE_PGN, "white", profile_id=profile["id"])
        assert self._saved_game_profile_id(game_id) == profile["id"]

    def test_omitted_profile_id_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setattr(mistakes, "analyze_and_store_game", lambda game_id, pgn: None)
        from profiles import default_profile_id

        game_id = manual_analysis.save_manual_game(_SAVE_PGN, "black")
        assert self._saved_game_profile_id(game_id) == default_profile_id()


class TestGradedMovesMissedMate:
    """Regression test for the one-off Analyze-board path: _graded_moves
    used to diff eval_before/eval_after raw (same bug fixed in mistakes.py's
    _classify_move — see TestClassifyMoveMissedMate in test_mistakes.py and
    the CHANGELOG v20 entry), so a move that finds a slower mate than the
    fastest one available — still completely winning either way — could
    carry an uncapped "drop" of thousands of centipawns (analysis.py
    represents mate scores as roughly +-MATE_SCORE_CP) and get graded a
    blunder for a position whose practical outcome never changed. Unlike
    _classify_move (only called for flagged mistakes), _graded_moves grades
    every move via classify_tier, so the bug showed up as a wrong `tier` on
    an ordinary move in a pasted-but-not-saved game.
    """

    _SAMPLE_PGN = (
        '[Event "Test"]\n[White "A"]\n[Black "B"]\n[Result "1-0"]\n\n'
        "1. e4 e5 2. Nf3 1-0\n"
    )

    def _fake_move(self, **overrides):
        base = {
            "ply": 1, "move_number": 1, "color_moved": "white", "move_san": "Kg1",
            "eval_before_cp": 9998, "eval_after_cp": 6000, "eval_cp": 6000,
            "non_king_piece_count": 10, "clock_seconds_remaining": 30,
        }
        base.update(overrides)
        return base

    def test_missing_the_fastest_mate_in_an_already_winning_position_grades_as_best(self, monkeypatch):
        # Both eval_before and eval_after are still deep in "completely
        # winning" territory (mate found either way) — practically nothing
        # changed, so this should grade as "best", not "blunder".
        monkeypatch.setattr(manual_analysis, "analyze_game_moves", lambda pgn_text, depth: [self._fake_move()])
        moves = manual_analysis._graded_moves(self._SAMPLE_PGN, depth=1)

        assert moves[0]["eval_drop"] == 0
        assert moves[0]["tier"] == "best"

    def test_a_real_winning_to_losing_blunder_is_still_graded_as_blunder(self, monkeypatch):
        move = self._fake_move(eval_before_cp=950, eval_after_cp=-950, eval_cp=-950)
        monkeypatch.setattr(manual_analysis, "analyze_game_moves", lambda pgn_text, depth: [move])
        moves = manual_analysis._graded_moves(self._SAMPLE_PGN, depth=1)

        assert moves[0]["tier"] == "blunder"


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

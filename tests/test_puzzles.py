import itertools
import json

import chess
import pytest

import puzzles
from db import get_connection

_counter = itertools.count(1)

# Black to move, has an undefended white knight to take: Rxd4 wins it.
FEN = "3r2k1/8/8/8/3N4/8/8/4K3 b - - 0 1"


def _puzzle(best_uci="d8d4", best_san="Rxd4", top_lines=None, themes=None, fen=FEN):
    top_lines = top_lines or [
        {"move_uci": "d8d4", "move_san": "Rxd4", "eval_cp": 400, "mate_in": None},
        {"move_uci": "d8d5", "move_san": "Rd5", "eval_cp": 380, "mate_in": None},
        {"move_uci": "g8f7", "move_san": "Kf7", "eval_cp": 100, "mate_in": None},
    ]
    return {
        "fen_before": fen, "best_move_uci": best_uci, "best_move_san": best_san, "top_lines": top_lines,
        "played_move_san": "Kf7", "best_move_explanation": "x", "played_move_explanation": "y",
        "themes": themes if themes is not None else ["hanging_piece"],
    }


class TestLastMoveBeforePly:
    PGN = "1. e4 e5 2. Nf3 Nc6 *"

    def test_returns_the_move_that_led_into_the_puzzle_position(self):
        # Ply 3 is White's Nf3, so the move that led here is Black's ...e5.
        assert puzzles.last_move_before_ply(self.PGN, 3) == {"from": "e7", "to": "e5", "san": "e5"}

    def test_second_ply_follows_the_first_move(self):
        assert puzzles.last_move_before_ply(self.PGN, 2) == {"from": "e2", "to": "e4", "san": "e4"}

    def test_no_previous_move_for_the_first_ply(self):
        assert puzzles.last_move_before_ply(self.PGN, 1) is None
        assert puzzles.last_move_before_ply(self.PGN, None) is None

    def test_ply_past_the_end_of_the_game_is_none(self):
        assert puzzles.last_move_before_ply(self.PGN, 99) is None


class TestLineCp:
    def test_plain_eval_passes_through(self):
        assert puzzles._line_cp({"eval_cp": 123, "mate_in": None}) == 123

    def test_mate_for_the_mover_is_a_huge_positive_number(self):
        assert puzzles._line_cp({"eval_cp": None, "mate_in": 3}) > 9000

    def test_mate_against_the_mover_is_a_huge_negative_number(self):
        assert puzzles._line_cp({"eval_cp": None, "mate_in": -2}) < -9000

    def test_faster_mate_ranks_higher(self):
        assert puzzles._line_cp({"mate_in": 1}) > puzzles._line_cp({"mate_in": 4})


class TestCheckAttempt:
    def test_best_move_is_correct(self):
        result = puzzles.check_attempt(_puzzle(), "d8", "d4")
        assert result["correct"] is True
        assert result["played_uci"] == "d8d4"
        assert result["best_move_uci"] == "d8d4"

    def test_a_close_alternative_in_the_top_lines_is_still_correct(self):
        assert puzzles.check_attempt(_puzzle(), "d8", "d5")["correct"] is True

    def test_a_clearly_worse_line_is_incorrect(self):
        assert puzzles.check_attempt(_puzzle(), "g8", "f7")["correct"] is False

    def test_illegal_move_raises(self):
        with pytest.raises(puzzles.IllegalMoveError):
            puzzles.check_attempt(_puzzle(), "d8", "a1")

    def test_result_carries_themes_and_labels(self):
        result = puzzles.check_attempt(_puzzle(themes=["fork", "mate_in_2"]), "d8", "d4")
        assert result["themes"] == ["fork", "mate_in_2"]
        assert result["theme_labels"] == ["Fork", "Mate in 2"]

    def test_explicit_promotion_piece_is_honoured(self):
        pz = _puzzle(
            fen="8/P7/8/8/8/8/k6K/8 w - - 0 1", best_uci="a7a8q", best_san="a8=Q+",
            top_lines=[{"move_uci": "a7a8q", "move_san": "a8=Q+", "eval_cp": 900, "mate_in": None},
                       {"move_uci": "a7a8n", "move_san": "a8=N", "eval_cp": 300, "mate_in": None}],
        )
        assert puzzles.check_attempt(pz, "a7", "a8", "n")["played_uci"] == "a7a8n"
        # No piece given: auto-queen, as before.
        assert puzzles.check_attempt(pz, "a7", "a8")["played_uci"] == "a7a8q"


class TestCheckAttemptEvaluated:
    def test_move_outside_the_top_lines_is_judged_by_the_engine(self, monkeypatch):
        monkeypatch.setattr(puzzles, "evaluate_move", lambda fen, uci, depth=0: {"cp": -300.0, "mate_in": None})
        # Kh7 isn't in the pre-computed lines; the engine (stubbed) says it's terrible.
        result = puzzles.check_attempt(_puzzle(), "g8", "h7", evaluate=True)
        assert result["correct"] is False
        assert result["verdict"] in ("mistake", "blunder")
        assert result["played_cp"] == -300.0

    def test_an_unlisted_move_that_is_just_as_good_counts_as_correct(self, monkeypatch):
        monkeypatch.setattr(puzzles, "evaluate_move", lambda fen, uci, depth=0: {"cp": 405.0, "mate_in": None})
        result = puzzles.check_attempt(_puzzle(), "g8", "h7", evaluate=True)
        assert result["correct"] is True
        assert result["verdict"] == "best"

    def test_listed_lines_use_stored_evals_without_calling_the_engine(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("no engine call for a pre-computed line")
        monkeypatch.setattr(puzzles, "evaluate_move", boom)
        result = puzzles.check_attempt(_puzzle(), "d8", "d5", evaluate=True)
        assert result["verdict"] in ("best", "excellent")

    def test_missing_a_forced_mate_is_not_correct(self, monkeypatch):
        pz = _puzzle(top_lines=[
            {"move_uci": "d8d4", "move_san": "Rxd4", "eval_cp": None, "mate_in": 2},
            {"move_uci": "d8d5", "move_san": "Rd5", "eval_cp": 200, "mate_in": None},
        ])
        result = puzzles.check_attempt(pz, "d8", "d5", evaluate=True)
        assert result["correct"] is False
        assert result["best_mate_in"] == 2

    def test_classic_mode_is_untouched_by_the_new_fields(self):
        assert "verdict" not in puzzles.check_attempt(_puzzle(), "d8", "d4")


class TestHints:
    def test_level_one_points_at_the_piece_and_names_the_theme(self):
        hint = puzzles.get_hint(_puzzle(themes=["fork", "mate_in_2"]), 1)
        assert hint == {"level": 1, "from": "d8", "themes": ["Fork"]}

    def test_level_two_reveals_the_move(self):
        hint = puzzles.get_hint(_puzzle(), 2)
        assert (hint["from"], hint["to"], hint["san"]) == ("d8", "d4", "Rxd4")


def _insert_puzzle_row(themes):
    """One game + one mistake + one puzzle, themes given as a JSON list (or None)."""
    n = next(_counter)
    conn = get_connection()
    try:
        game_id = conn.execute(
            "INSERT INTO games (source, source_game_id, date, result, color, analyzed) "
            "VALUES ('manual', ?, datetime('now'), 'win', 'white', 1)", (f"puz-test-{n}",),
        ).lastrowid
        mistake_id = conn.execute(
            "INSERT INTO mistakes (game_id, ply, move_number, move_san, color_moved, phase, severity, "
            "eval_before, eval_after, eval_drop) VALUES (?, 10, 5, 'Kf7', 'black', 'endgame', 'blunder', 0, -400, 400)",
            (game_id,),
        ).lastrowid
        puzzle_id = conn.execute(
            "INSERT INTO puzzles (mistake_id, game_id, fen_before, side_to_move, played_move_san, best_move_uci, "
            "best_move_san, top_lines, phase, severity, created_at, themes) "
            "VALUES (?, ?, ?, 'black', 'Kf7', 'd8d4', 'Rxd4', ?, 'endgame', 'blunder', datetime('now'), ?)",
            (mistake_id, game_id, FEN, json.dumps([{"move_uci": "d8d4", "move_san": "Rxd4", "eval_cp": 400, "mate_in": None}]),
             None if themes is None else json.dumps(themes)),
        ).lastrowid
        conn.commit()
        return puzzle_id
    finally:
        conn.close()


class TestThemesInTheDatabase:
    def test_queue_can_filter_by_theme_and_only_returns_matches(self):
        fork_id = _insert_puzzle_row(["fork"])
        pin_id = _insert_puzzle_row(["pin"])
        ids = {p["id"] for p in puzzles.get_puzzle_queue(theme="fork", limit=500)}
        assert fork_id in ids
        assert pin_id not in ids

    def test_mate_theme_filter_does_not_match_mate_in_n(self):
        mate_in_id = _insert_puzzle_row(["mate_in_2"])
        plain_mate_id = _insert_puzzle_row(["mate"])
        ids = {p["id"] for p in puzzles.get_puzzle_queue(theme="mate", limit=500)}
        assert plain_mate_id in ids
        assert mate_in_id not in ids

    def test_missing_themes_are_backfilled_lazily(self):
        pid = _insert_puzzle_row(None)
        puzzles.get_puzzle_queue(theme="fork", limit=5)  # triggers the lazy backfill
        conn = get_connection()
        try:
            stored = conn.execute("SELECT themes FROM puzzles WHERE id = ?", (pid,)).fetchone()["themes"]
        finally:
            conn.close()
        assert stored is not None
        assert "hanging_piece" in json.loads(stored)  # Rxd4 takes an undefended knight

    def test_get_puzzle_exposes_the_mistake_ply_for_last_move_lookup(self):
        pid = _insert_puzzle_row(["fork"])
        assert puzzles.get_puzzle(pid)["mistake_ply"] == 10

    def test_theme_counts_list_labels_in_ui_order(self):
        _insert_puzzle_row(["fork"])
        counts = puzzles.get_theme_counts()
        assert all({"key", "label", "count"} <= set(c) for c in counts)
        keys = [c["key"] for c in counts]
        assert keys == sorted(keys, key=lambda k: __import__("tactics").THEME_KEYS.index(k))

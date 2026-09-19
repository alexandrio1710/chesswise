import itertools

import pytest

import game_meta
from db import get_connection

_counter = itertools.count(1)


def _pgn(moves: str, result: str, termination: str | None = None) -> str:
    headers = f'[Event "t"]\n[White "a"]\n[Black "b"]\n[Result "{result}"]\n'
    if termination:
        headers += f'[Termination "{termination}"]\n'
    return f"{headers}\n{moves} {result}\n"


class TestTermination:
    @pytest.mark.parametrize("termination,expected", [
        ("someone won by resignation", "resignation"),
        ("someone won by checkmate", "checkmate"),
        ("someone won on time", "timeout"),
        ("someone won - game abandoned", "abandonment"),
        ("Game drawn by repetition", "repetition"),
        ("Game drawn by stalemate", "stalemate"),
        ("Game drawn by agreement", "agreement"),
        ("Game drawn by insufficient material", "insufficient_material"),
        ("Game drawn by timeout vs insufficient material", "timeout_vs_insufficient"),
        ("Time forfeit", "timeout"),
    ])
    def test_chesscom_and_lichess_text(self, termination, expected):
        result = "1/2-1/2" if "drawn" in termination else "1-0"
        assert game_meta.classify_termination(_pgn("1. e4 e5 2. Nf3", result, termination)) == expected

    def test_lichess_normal_ending_in_mate_is_checkmate(self):
        assert game_meta.classify_termination(_pgn("1. f3 e5 2. g4 Qh4#", "0-1", "Normal")) == "checkmate"

    def test_lichess_normal_decisive_without_mate_is_resignation(self):
        assert game_meta.classify_termination(_pgn("1. e4 e5 2. Nf3 Nc6", "1-0", "Normal")) == "resignation"

    def test_stalemate_is_read_off_the_final_position(self):
        moves = ("1. e3 a5 2. Qh5 Ra6 3. Qxa5 h5 4. h4 Rah6 5. Qxc7 f6 6. Qxd7+ Kf7 7. Qxb7 Qd3 "
                 "8. Qxb8 Qh7 9. Qxc8 Kg6 10. Qe6")
        assert game_meta.classify_termination(_pgn(moves, "1/2-1/2", "Normal")) == "stalemate"

    def test_a_drawn_normal_game_with_nothing_special_is_agreement(self):
        assert game_meta.classify_termination(_pgn("1. e4 e5", "1/2-1/2", "Normal")) == "agreement"

    def test_unparseable_pgn_is_other(self):
        assert game_meta.classify_termination("") == "other"


def _curve(*segments):
    """Eval curve from (start, end, plies) segments, linearly interpolated."""
    out = []
    for start, end, n in segments:
        out += [start + (end - start) * i / max(1, n - 1) for i in range(n)]
    return out


class TestShape:
    def test_a_short_game_is_balanced(self):
        assert game_meta.classify_shape([0, 10, -5]) == "balanced"

    def test_dead_equal_throughout_is_balanced(self):
        assert game_meta.classify_shape(_curve((0, 40, 20), (40, -30, 20))) == "balanced"

    def test_steadily_growing_lead_that_never_shrinks_is_smooth(self):
        assert game_meta.classify_shape(_curve((0, 450, 40), (450, 500, 10))) == "smooth"

    def test_winning_position_thrown_away_is_a_giveaway(self):
        assert game_meta.classify_shape(_curve((0, 800, 25), (800, 800, 5), (800, -100, 6))) == "giveaway"

    def test_the_lead_swapping_many_times_is_wild(self):
        curve = _curve((0, 250, 6), (250, -250, 6), (-250, 250, 6), (250, -250, 6), (-250, 250, 6), (250, -250, 6))
        assert game_meta.classify_shape(curve) == "wild"

    def test_one_lead_change_with_chances_both_ways_is_sharp(self):
        assert game_meta.classify_shape(_curve((0, 180, 15), (180, -180, 10), (-180, -100, 15))) == "sharp"

    def test_close_game_decided_by_one_move_is_sudden(self):
        curve = [10, -10, 20, 0, 15, -5, 10, 0, 20, -10] * 3 + [-600] * 6
        assert game_meta.classify_shape(curve) == "sudden"

    def test_every_shape_has_a_label_blurb(self):
        assert set(game_meta.SHAPES) == set(game_meta.SHAPE_BLURBS)


class TestEnsureMetadata:
    def _insert(self, pgn, evals):
        n = next(_counter)
        conn = get_connection()
        try:
            game_id = conn.execute(
                "INSERT INTO games (source, source_game_id, date, result, color, pgn, analyzed) "
                "VALUES ('manual', ?, datetime('now'), 'win', 'white', ?, 1)", (f"meta-{n}", pgn),
            ).lastrowid
            for i, cp in enumerate(evals, start=1):
                conn.execute(
                    "INSERT INTO game_moves (game_id, ply, move_number, color_moved, move_san, eval_cp) "
                    "VALUES (?, ?, ?, ?, 'e4', ?)", (game_id, i, (i + 1) // 2, "white" if i % 2 else "black", cp),
                )
            conn.commit()
            return game_id
        finally:
            conn.close()

    def _row(self, game_id):
        conn = get_connection()
        try:
            return dict(conn.execute(
                "SELECT termination_method, game_shape, book_plies FROM games WHERE id = ?", (game_id,)).fetchone())
        finally:
            conn.close()

    def test_fills_all_three_fields(self):
        game_id = self._insert(_pgn("1. e4 e5 2. Nf3 Nc6", "1-0", "someone won by resignation"), [20, 10] * 10)
        assert game_meta.ensure_metadata([game_id]) == 1
        row = self._row(game_id)
        assert row["termination_method"] == "resignation"
        assert row["game_shape"] == "balanced"
        assert row["book_plies"] is not None

    def test_a_second_run_has_nothing_to_do(self):
        game_id = self._insert(_pgn("1. e4 e5", "1-0", "someone won by checkmate"), [0] * 10)
        game_meta.ensure_metadata([game_id])
        assert game_meta.ensure_metadata([game_id]) == 0

    def test_empty_id_list_is_a_no_op(self):
        assert game_meta.ensure_metadata([]) == 0

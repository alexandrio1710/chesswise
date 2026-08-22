"""Unit tests for opening-family grouping (Stage D) and other stats.py fixes."""

import itertools

from db import get_connection
from stats import _is_immediately_preceding_month, annotate_fen, get_starting_fen, opening_family, opening_stats

_id_counter = itertools.count(1)

_SAMPLE_PGN = (
    '[Event "Test"]\n[White "A"]\n[Black "B"]\n[Result "1-0"]\n\n'
    "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 1-0\n"
)


class TestOpeningFamilyLichessStyle:
    """Lichess names always use "Family: Variation" — an exact split."""

    def test_splits_on_colon(self):
        assert opening_family("Sicilian Defense: Accelerated Dragon") == "Sicilian Defense"

    def test_nested_comma_variation_still_splits_on_first_colon(self):
        assert opening_family(
            "Indian Defense: Budapest Defense, Fajarowicz Variation"
        ) == "Indian Defense"

    def test_no_colon_returns_unchanged(self):
        assert opening_family("Van't Kruijs Opening") == "Van't Kruijs Opening"


class TestOpeningFamilyChesscomStyle:
    """Chess.com names have no delimiter and often trail into a literal
    move list — truncate at the first digit.
    """

    def test_truncates_at_first_digit(self):
        assert opening_family("Englund Gambit 2.dxe5") == "Englund Gambit"

    def test_short_family_names_collapse_cleanly(self):
        # These are the cases the heuristic is specifically designed to
        # get right, validated against real fetched data.
        assert opening_family("London System 3...Bf5 4.e3 e6 5.Bd3") == "London System"
        assert opening_family("Italian Game...6.Nc3 Be7 7.O O O O") == "Italian Game"
        assert opening_family("Dutch Defense") == "Dutch Defense"

    def test_strips_trailing_with_clause(self):
        assert opening_family("Modern Defense with 1 d4 2.Bf4 Bg7 3.e3") == "Modern Defense"

    def test_cross_source_names_collapse_to_the_same_family(self):
        # The real point of this function: the same opening, named
        # differently by each site, should land in the same bucket.
        chesscom_name = "Italian Game...6.Nc3 Be7 7.O O O O"
        lichess_name = "Italian Game"
        assert opening_family(chesscom_name) == opening_family(lichess_name)

    def test_no_digit_leaves_variation_words_attached(self):
        # Documented limitation: without a real ECO database, a variation
        # name with no digit in it doesn't reliably split from the family.
        assert opening_family("Sicilian Defense Nyezhmetdinov Rossolimo Attack") == \
            "Sicilian Defense Nyezhmetdinov Rossolimo Attack"


class TestOpeningFamilyEdgeCases:
    def test_empty_string_returns_unknown(self):
        assert opening_family("") == "Unknown"

    def test_none_returns_unknown(self):
        assert opening_family(None) == "Unknown"


class TestIsImmediatelyPrecedingMonth:
    def test_adjacent_months_same_year(self):
        assert _is_immediately_preceding_month("2026-08", "2026-07") is True

    def test_adjacent_months_across_year_boundary(self):
        assert _is_immediately_preceding_month("2026-01", "2025-12") is True

    def test_non_adjacent_months_with_a_gap(self):
        # trend_takeaway's own bug: monthly_trend skips months with zero
        # analyzed games rather than zero-filling, so trend[1] can be two
        # or more months back, not necessarily "last month".
        assert _is_immediately_preceding_month("2026-07", "2026-05") is False

    def test_same_month_is_not_preceding(self):
        assert _is_immediately_preceding_month("2026-07", "2026-07") is False


def _insert_game(opening_name: str, date: str = None) -> int:
    conn = get_connection()
    try:
        game_id = conn.execute(
            "INSERT INTO games (source, source_game_id, date, result, color, opening_name, analyzed) "
            "VALUES ('manual', ?, ?, 'win', 'white', ?, 1)",
            (f"stats-test-{next(_id_counter)}", date or "2026-01-01T00:00:00+00:00", opening_name),
        ).lastrowid
        conn.commit()
        return game_id
    finally:
        conn.close()


def _insert_opening_mistake(game_id: int, phase: str = "opening") -> int:
    conn = get_connection()
    try:
        mistake_id = conn.execute(
            "INSERT INTO mistakes (game_id, ply, move_number, move_san, color_moved, phase, "
            "severity, eval_before, eval_after, eval_drop) "
            "VALUES (?, 1, 1, 'e4', 'white', ?, 'blunder', 0, -500, 500)",
            (game_id, phase),
        ).lastrowid
        conn.commit()
        return mistake_id
    finally:
        conn.close()


class TestOpeningStatsEmptyOpeningName:
    def test_a_real_openings_mistake_count_is_unaffected_by_unclassified_games(self):
        # game_rows already excludes opening_name == '' games entirely
        # (nothing to attribute an unclassified opening to), so their
        # mistakes were never going to surface in the output either way —
        # the fix here is aligning mistake_rows' WHERE clause with
        # game_rows' (both now filter opening_name != ''), which stops a
        # wasted, unused aggregate rather than changing what's returned.
        # This test locks in that a real opening's own count stays correct
        # regardless of empty-opening-name games/mistakes elsewhere in the
        # DB — true before and after, worth guarding either way.
        game_id = _insert_game("Test Opening XYZ")
        _insert_opening_mistake(game_id)
        empty_game_id = _insert_game("")
        _insert_opening_mistake(empty_game_id)

        results = {r["opening_name"]: r for r in opening_stats()}
        assert results["Test Opening XYZ"]["opening_phase_mistakes"] == 1
        assert "" not in results


class TestAnnotateFen:
    """Backs the game page's interactive board — the client only ever
    renders a FEN it's given, so these must line up exactly with the moves
    actually played.
    """

    def test_starting_fen_is_the_standard_position(self):
        assert get_starting_fen(_SAMPLE_PGN) == "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

    def test_fen_after_first_move(self):
        moves = [{"ply": 1}]
        result = annotate_fen(moves, _SAMPLE_PGN)
        assert result[0]["fen_after"] == "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

    def test_fen_after_matches_every_ply_in_order(self):
        moves = [{"ply": i} for i in range(1, 7)]  # 1.e4 e5 2.Nf3 Nc6 3.Bb5 a6
        result = annotate_fen(moves, _SAMPLE_PGN)
        assert result[-1]["fen_after"] == "r1bqkbnr/1ppp1ppp/p1n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 0 4"

    def test_empty_pgn_text_returns_none_rather_than_raising(self):
        # chess.pgn.read_game returns None only for a genuinely empty
        # stream — garbage text like "not a pgn" is parsed leniently as an
        # empty game at the standard starting position, not a parse error.
        moves = [{"ply": 1}]
        result = annotate_fen(moves, "")
        assert result[0]["fen_after"] is None
        assert get_starting_fen("") is None

    def test_garbage_pgn_text_is_parsed_leniently_as_an_empty_game(self):
        assert get_starting_fen("not a pgn") == "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

    def test_does_not_mutate_the_input_list(self):
        moves = [{"ply": 1}]
        annotate_fen(moves, _SAMPLE_PGN)
        assert "fen_after" not in moves[0]

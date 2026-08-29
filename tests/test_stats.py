"""Unit tests for opening-family grouping (Stage D) and other stats.py fixes."""

import itertools

from db import get_connection
from stats import (
    _is_immediately_preceding_month,
    annotate_fen,
    capped_eval_drop,
    compute_game_acpl,
    compute_game_accuracy,
    get_starting_fen,
    opening_family,
    opening_stats,
)

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


def _move(color: str, eval_before_cp: float, eval_drop: float) -> dict:
    """`eval_drop` here is the already-capped value mistakes.py would have
    stored (see TestCappedEvalDrop below for the capping itself) — matches
    what get_game_moves() actually hands compute_game_acpl/accuracy.
    """
    return {"color_moved": color, "eval_before_cp": eval_before_cp, "eval_drop": eval_drop}


class TestCappedEvalDrop:
    """analysis.py represents a forced mate as roughly +-10000cp
    (MATE_SCORE_CP) so eval comparisons don't need special-case mate
    handling. Diffed raw, one such move (missing/walking into mate in an
    already-decided position) could carry a "drop" in the thousands of
    centipawns even though nothing practical changed — confirmed against
    real data: 11 of 59 already-analyzed games had a move with an
    eval_drop within a few hundred cp of MATE_SCORE_CP, and this class of
    move was also getting flagged as a severity-cratering blunder. Fixed
    by capping the eval magnitude (ACCURACY_EVAL_CAP_CP = 1000) on both
    sides of the diff before it feeds anything downstream — applied once
    here, at the point mistakes.py first computes eval_drop.
    """

    def test_a_mate_swing_in_an_already_winning_position_is_capped_hard(self):
        # Still winning before (900cp) and after finding a slower mate
        # (eval_after=-10000, i.e. a "drop" of 10900 raw) — the practical
        # verdict never changed, so this should cost nowhere near 10900cp.
        assert capped_eval_drop(900, -10000) == 1900.0

    def test_a_move_that_does_not_change_an_already_decided_verdict_costs_nothing(self):
        # Crushing before (1500cp, past the cap) and still crushing after
        # (1200cp, also past the cap) — the raw 300cp "drop" didn't change
        # the practical outcome, so it should cost ~0.
        assert capped_eval_drop(1500, 1200) == 0.0

    def test_a_real_winning_to_losing_swing_is_still_counted_as_a_big_mistake(self):
        # Genuinely blowing a winning position (1500cp) into a losing one
        # (-1500cp) is a real, serious mistake and must still register as
        # one — capped at 2x the ceiling (2000cp) rather than uncapped.
        assert capped_eval_drop(1500, -1500) == 2000.0

    def test_a_small_swing_well_within_the_cap_is_unaffected(self):
        assert capped_eval_drop(200, 120) == 80.0

    def test_a_move_that_improves_the_position_clamps_to_zero_not_negative(self):
        assert capped_eval_drop(50, 200) == 0.0


class TestComputeGameAccuracyMateSwingCap:
    """compute_game_accuracy trusts eval_drop is already capped (see
    TestCappedEvalDrop) by the time it sees it, so these tests exercise
    plain averaging/filtering/sample-size behavior with fixture values
    already in that post-capping shape.
    """

    def test_a_single_mate_swing_no_longer_craters_an_otherwise_clean_game(self):
        # 9 perfectly clean moves + 1 move whose already-capped eval_drop
        # is 1900 (capped_eval_drop(900, -10000), per TestCappedEvalDrop).
        # Precomputed: ACPL = 190.0, accuracy = 42.9%.
        moves = [_move("white", 50, 0) for _ in range(9)]
        moves.append(_move("white", 900, 1900))
        assert compute_game_accuracy(moves, "white") == 42.9

    def test_a_move_that_does_not_change_an_already_decided_verdict_costs_nothing(self):
        # capped_eval_drop(1500, 1200) == 0.0 (TestCappedEvalDrop) — a
        # second clean move satisfies the "at least 2 moves" minimum
        # sample size below.
        moves = [_move("white", 1500, 0), _move("white", 50, 0)]
        assert compute_game_accuracy(moves, "white") == 100.0

    def test_a_real_winning_to_losing_swing_is_still_counted_as_a_big_mistake(self):
        # capped_eval_drop(1500, -1500) == 2000.0 (TestCappedEvalDrop) — a
        # real, serious mistake that must still register as one.
        moves = [_move("white", 1500, 2000), _move("white", 50, 0)]
        acc = compute_game_accuracy(moves, "white")
        assert acc < 50.0

    def test_only_the_given_colors_own_moves_count(self):
        moves = [
            _move("white", 900, 1900), _move("white", 50, 0),
            _move("black", 50, 0), _move("black", 50, 400),
        ]
        assert compute_game_accuracy(moves, "white") != compute_game_accuracy(moves, "black")

    def test_no_moves_for_color_returns_none(self):
        moves = [_move("black", 50, 0), _move("black", 50, 10)]
        assert compute_game_accuracy(moves, "white") is None

    def test_fewer_than_two_moves_returns_none_rather_than_a_meaningless_score(self):
        # Confirmed against real data: a game the opponent abandoned right
        # after the opening ("1. e4 c5", win by abandonment) had exactly
        # one analyzed move for the winner, which happened to have ~0
        # eval_drop — scoring a meaningless 100% "accuracy" for a game
        # where no real play occurred. One move isn't a real sample.
        moves = [_move("white", 50, 0)]
        assert compute_game_accuracy(moves, "white") is None


class TestComputeGameAcpl:
    """compute_game_accuracy is now just compute_game_acpl run through an
    exponential decay — these tests lock in that the two stay in sync
    after that refactor, plus the ACPL-specific value itself (the new
    "average centipawn loss by time control" Insights card reports this
    number directly, not just the derived accuracy percentage).
    """

    def test_matches_the_precomputed_value_behind_the_accuracy_test_above(self):
        # Same fixture as the mate-swing accuracy test: 9 clean moves + 1
        # move whose already-capped eval_drop is 1900. Precomputed ACPL =
        # 190.0 (accuracy 42.9% is 100 * e^(-0.00446 * 190.0), confirmed in
        # the accuracy test above).
        moves = [_move("white", 50, 0) for _ in range(9)]
        moves.append(_move("white", 900, 1900))
        assert compute_game_acpl(moves, "white") == 190.0

    def test_zero_acpl_for_a_perfectly_played_game(self):
        moves = [_move("white", 50, 0), _move("white", 60, 0)]
        assert compute_game_acpl(moves, "white") == 0.0

    def test_fewer_than_two_moves_returns_none(self):
        assert compute_game_acpl([_move("white", 50, 0)], "white") is None

    def test_accuracy_is_derived_from_acpl_via_the_documented_formula(self):
        import math

        from stats import ACCURACY_DECAY_K

        moves = [_move("white", 200, 80), _move("white", 150, 40), _move("white", 90, 10)]
        acpl = compute_game_acpl(moves, "white")
        expected_accuracy = round(max(0.0, min(100.0, 100 * math.exp(-ACCURACY_DECAY_K * acpl))), 1)
        assert compute_game_accuracy(moves, "white") == expected_accuracy

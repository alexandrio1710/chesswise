"""Unit tests for opening-family grouping (Stage D) and other stats.py fixes."""

import itertools

import stats
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


def _ply(ply: int, color: str, eval_cp: float) -> dict:
    """get_game_moves()'s shape for the fields compute_game_accuracy
    actually reads — `moves` must be every move of the game, both colors,
    in ply order (see that function's docstring for why)."""
    return {"ply": ply, "color_moved": color, "eval_cp": eval_cp}


class TestWinPercentAndMoveAccuracy:
    """Building blocks of compute_game_accuracy — a direct port of
    Lichess's own algorithm (see stats.py's module comment above
    WIN_PERCENT_MULTIPLIER for the source links). Hand-verifiable in
    isolation, independent of the whole-game windowing/aggregation below.
    """

    def test_dead_equal_is_fifty_percent(self):
        assert stats._win_percent(0) == 50.0

    def test_positive_cp_favors_the_side_its_measured_from(self):
        assert stats._win_percent(200) > 50.0
        assert stats._win_percent(-200) < 50.0

    def test_a_mate_score_is_capped_to_the_same_ceiling_as_a_thousand_centipawns(self):
        # analysis.py represents mate as roughly +-MATE_SCORE_CP (10000);
        # WIN_PERCENT_CP_CEILING (1000) caps it the same way ACPL capping
        # does elsewhere in this app, so a mate score isn't treated as
        # infinitely more decisive than a 10-pawn material lead.
        assert stats._win_percent(10000) == stats._win_percent(1000)
        assert stats._win_percent(-10000) == stats._win_percent(-1000)

    def test_a_move_that_improves_win_percent_is_perfect(self):
        assert stats._move_accuracy(before_win_percent=40, after_win_percent=60) == 100.0

    def test_a_move_that_loses_win_percent_scores_below_perfect(self):
        acc = stats._move_accuracy(before_win_percent=60, after_win_percent=40)
        assert 0.0 < acc < 100.0

    def test_a_larger_win_percent_loss_scores_lower(self):
        small_loss = stats._move_accuracy(before_win_percent=55, after_win_percent=50)
        big_loss = stats._move_accuracy(before_win_percent=90, after_win_percent=10)
        assert big_loss < small_loss


class TestComputeGameAccuracy:
    """compute_game_accuracy is a direct port of Lichess's own algorithm —
    see stats.py's module comment for the source links. Needs the whole
    game's moves (both colors, in ply order): its volatility weighting
    looks at a sliding window across the sequence, which breaks if
    pre-filtered to one color first.
    """

    def test_a_perfectly_played_game_scores_100(self):
        # Win percent never drops for either side (both hover near dead
        # equal) — every move is "perfect" by definition.
        moves = [_ply(i, "white" if i % 2 == 1 else "black", 15) for i in range(1, 21)]
        assert compute_game_accuracy(moves, "white") == 100.0
        assert compute_game_accuracy(moves, "black") == 100.0

    def test_a_real_blunder_scores_meaningfully_lower_than_a_clean_game(self):
        clean = [_ply(i, "white" if i % 2 == 1 else "black", 20) for i in range(1, 21)]
        with_blunder = list(clean)
        # White's move 11 (ply 11) throws a comfortably-equal game into a
        # completely lost position.
        with_blunder[10] = _ply(11, "white", -900)
        assert compute_game_accuracy(with_blunder, "white") < compute_game_accuracy(clean, "white")

    def test_only_the_given_colors_own_moves_count(self):
        moves = [
            _ply(1, "white", 20), _ply(2, "black", 15), _ply(3, "white", -900), _ply(4, "black", -850),
        ]
        assert compute_game_accuracy(moves, "white") != compute_game_accuracy(moves, "black")

    def test_no_moves_for_color_returns_none(self):
        moves = [_ply(1, "black", 20), _ply(2, "black", 15)]
        assert compute_game_accuracy(moves, "white") is None

    def test_fewer_than_two_total_moves_returns_none(self):
        assert compute_game_accuracy([_ply(1, "white", 20)], "white") is None


class TestComputeGameAcpl:
    """compute_game_acpl stays a plain average of eval_drop (already
    magnitude-capped at the source — see TestCappedEvalDrop), independent
    of compute_game_accuracy above: that one is a direct port of Lichess's
    own win-probability-based algorithm now, not derived from ACPL at all,
    while this one is deliberately still a plain, literal "average
    centipawns lost per move" for the Insights ACPL-by-time-control card.
    """

    def test_a_mate_swing_in_an_already_winning_position_barely_costs_acpl(self):
        # 9 perfectly clean moves + 1 move whose already-capped eval_drop
        # is 1900 (capped_eval_drop(900, -10000), per TestCappedEvalDrop).
        moves = [_move("white", 50, 0) for _ in range(9)]
        moves.append(_move("white", 900, 1900))
        assert compute_game_acpl(moves, "white") == 190.0

    def test_zero_acpl_for_a_perfectly_played_game(self):
        moves = [_move("white", 50, 0), _move("white", 60, 0)]
        assert compute_game_acpl(moves, "white") == 0.0

    def test_fewer_than_two_moves_returns_none(self):
        assert compute_game_acpl([_move("white", 50, 0)], "white") is None

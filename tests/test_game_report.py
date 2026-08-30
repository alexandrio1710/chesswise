"""Regression test for game_report.py's stale-cache bug: generate_game_report
used to return a cached game_reports row unconditionally (unless force=True),
with no comparison against games.analyzed_at — so a game re-analyzed after
its report was first cached kept showing the old accuracy/rating/summary
indefinitely. compute_enriched_classification (the expensive, Stockfish-
backed part) is monkeypatched out here so these tests run without an engine
and without needing real game_moves data — only the cache-freshness
DECISION is under test.
"""

import itertools

import chess

import game_report
from db import get_connection

_id_counter = itertools.count(1)


def _insert_analyzed_game(analyzed_at: str) -> int:
    conn = get_connection()
    try:
        game_id = conn.execute(
            "INSERT INTO games (source, source_game_id, date, result, color, analyzed, analyzed_at) "
            "VALUES ('manual', ?, datetime('now'), 'win', 'white', 1, ?)",
            (f"report-test-{next(_id_counter)}", analyzed_at),
        ).lastrowid
        conn.commit()
        return game_id
    finally:
        conn.close()


def _insert_cached_report(game_id: int, computed_at: str, accuracy_overall: float) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO game_reports (game_id, accuracy_overall, tier_counts, summary, computed_at) "
            "VALUES (?, ?, '{}', 'test summary', ?)",
            (game_id, accuracy_overall, computed_at),
        )
        conn.commit()
    finally:
        conn.close()


class TestReportCacheInvalidation:
    def test_cache_older_than_reanalysis_is_recomputed(self, monkeypatch):
        game_id = _insert_analyzed_game(analyzed_at="2026-06-01 00:00:00")
        _insert_cached_report(game_id, computed_at="2026-01-01 00:00:00", accuracy_overall=50.0)

        calls = []
        monkeypatch.setattr(
            game_report, "compute_enriched_classification",
            lambda gid, depth=game_report.STOCKFISH_DEPTH: calls.append(gid),
        )

        game_report.generate_game_report(game_id)
        assert calls == [game_id], "a report older than the game's last analysis must be recomputed"

    def test_cache_newer_than_last_analysis_is_served_without_recomputing(self, monkeypatch):
        game_id = _insert_analyzed_game(analyzed_at="2026-01-01 00:00:00")
        _insert_cached_report(game_id, computed_at="2026-06-01 00:00:00", accuracy_overall=77.0)

        def _boom(*args, **kwargs):
            raise AssertionError("a fresh cache should not trigger recomputation")

        monkeypatch.setattr(game_report, "compute_enriched_classification", _boom)

        result = game_report.generate_game_report(game_id)
        assert result["accuracy_overall"] == 77.0

    def test_force_always_recomputes_even_when_cache_is_fresh(self, monkeypatch):
        game_id = _insert_analyzed_game(analyzed_at="2026-01-01 00:00:00")
        _insert_cached_report(game_id, computed_at="2026-06-01 00:00:00", accuracy_overall=77.0)

        calls = []
        monkeypatch.setattr(
            game_report, "compute_enriched_classification",
            lambda gid, depth=game_report.STOCKFISH_DEPTH: calls.append(gid),
        )

        game_report.generate_game_report(game_id, force=True)
        assert calls == [game_id]


def _move(eval_before_cp: float, eval_drop: float) -> dict:
    return {"eval_before_cp": eval_before_cp, "eval_drop": eval_drop}


class TestAcplMinimumSampleSize:
    """Mirrors stats.compute_game_accuracy's own minimum: one move isn't a
    real sample (a game the opponent abandoned right after the opening
    scored a meaningless ~perfect ACPL/rating from a single book move).
    """

    def test_a_single_move_returns_none_rather_than_a_meaningless_acpl(self):
        assert game_report._acpl([_move(50, 0)]) is None

    def test_two_moves_is_enough_to_compute_a_real_acpl(self):
        assert game_report._acpl([_move(50, 0), _move(50, 100)]) == 50.0


class TestTimeControlAdjustedRating:
    """estimate_performance_rating() previously ignored time control
    entirely, even though the same player's ACPL runs measurably higher at
    faster time controls from time pressure alone — confirmed as a real
    gap and fixed by dividing ACPL by a per-time-control factor before the
    rating lookup, so faster games get credit for that extra noise.
    """

    def test_bullet_acpl_is_scaled_down_before_the_rating_lookup(self):
        acpl = 100.0
        assert game_report._time_control_adjusted_acpl(acpl, "bullet") == acpl / 1.5

    def test_classical_and_daily_are_the_unadjusted_baseline(self):
        acpl = 100.0
        assert game_report._time_control_adjusted_acpl(acpl, "classical") == acpl
        assert game_report._time_control_adjusted_acpl(acpl, "daily") == acpl

    def test_unrecognized_time_control_falls_back_to_no_adjustment(self):
        acpl = 100.0
        assert game_report._time_control_adjusted_acpl(acpl, "unknown") == acpl
        assert game_report._time_control_adjusted_acpl(acpl, None) == acpl

    def test_the_same_raw_acpl_rates_higher_at_a_faster_time_control(self):
        # Same actual move quality, but bullet's time pressure means that
        # ACPL reflects less true skill deficit than it would at classical.
        acpl = 100.0
        bullet_rating = game_report.estimate_performance_rating(
            game_report._time_control_adjusted_acpl(acpl, "bullet")
        )
        classical_rating = game_report.estimate_performance_rating(
            game_report._time_control_adjusted_acpl(acpl, "classical")
        )
        assert bullet_rating > classical_rating


class TestUscfRating:
    # US Chess's own real 2024 conversion formula runs the *opposite*
    # direction from this project's old flat guess: US Chess ratings come
    # out higher than the FIDE-ish input across the whole range, not lower
    # (matches both this formula and every pre-2024 rule of thumb US Chess
    # has published — "USCF = FIDE + 100" and similar — all add, none
    # subtract).
    def test_low_range_formula(self):
        assert game_report._uscf_from_estimated_rating(1600) == 1834  # 932 + 0.564*1600

    def test_high_range_formula(self):
        assert game_report._uscf_from_estimated_rating(2500) == 2570  # 20 + 1.02*2500

    def test_continuous_at_the_breakpoint(self):
        assert game_report._uscf_from_estimated_rating(2000) == 2060
        just_below = game_report._uscf_from_estimated_rating(1999)
        just_above = game_report._uscf_from_estimated_rating(2001)
        assert abs(just_above - just_below) <= 2  # a tiny step, not a jump across the seam

    def test_floors_at_zero_rather_than_going_negative(self):
        assert game_report._uscf_from_estimated_rating(-5000) == 0

    def test_none_in_none_out(self):
        assert game_report._uscf_from_estimated_rating(None) is None


class TestSee:
    def test_no_attacker_returns_zero(self):
        board = chess.Board("4k3/8/8/3n4/8/8/8/4K3 w - - 0 1")
        assert game_report._see(board, chess.D5) == 0

    def test_undefended_capture_wins_its_full_value(self):
        board = chess.Board("4k3/8/8/3n4/8/8/8/3RK3 w - - 0 1")
        assert game_report._see(board, chess.D5) == 3  # rook takes knight, nothing recaptures

    def test_a_losing_trade_is_clamped_to_zero_rather_than_going_negative(self):
        # Rxd5 wins a knight (3) but a pawn recaptures the rook (5) — net
        # -2 for White, so SEE correctly says "don't bother" (0), not -2.
        board = chess.Board("4k3/8/4p3/3n4/8/8/8/3RK3 w - - 0 1")
        assert game_report._see(board, chess.D5) == 0

    def test_a_multi_ply_exchange_nets_the_right_material(self):
        # cxd5 (pawn takes knight, +3) then exd5 (pawn recaptures pawn,
        # -1) — no further attackers either side. Net +2 for White.
        board = chess.Board("4k3/8/4p3/3n4/2P5/8/8/4K3 w - - 0 1")
        assert game_report._see(board, chess.D5) == 2


class TestIsSacrifice:
    def test_a_hanging_queen_move_is_a_sacrifice(self):
        # Qd1-d5 is a quiet move (d5 is empty) into a square only a black
        # rook attacks, with nothing defending it — a pure giveaway.
        board = chess.Board("4k3/8/8/r7/8/8/8/3QK3 w - - 0 1")
        move = chess.Move.from_uci("d1d5")
        assert game_report._is_sacrifice(board, move) is True

    def test_an_even_trade_is_not_a_sacrifice(self):
        # Nf4-d5 (quiet) into a square attacked once (Nb6) and defended
        # once (Nc3) by equal-value pieces — capture, recapture, done.
        board = chess.Board("4k3/8/1n6/8/5N2/2N5/8/4K3 w - - 0 1")
        move = chess.Move.from_uci("f4d5")
        assert game_report._is_sacrifice(board, move) is False

    def test_a_second_attacker_beyond_the_first_defender_is_still_a_sacrifice(self):
        # Nf4-d5 (quiet) into a square attacked by a knight AND a rook,
        # but defended by only one knight: Nxd5, Nxd5, Rxd5 — White's two
        # knights fall for Black's one, a real net loss the old one-ply
        # "is there a defender at all" check couldn't see (it stopped
        # after confirming Nc3 defends, never noticing the rook behind it).
        board = chess.Board("3rk3/8/1n6/8/5N2/2N5/8/K7 w - - 0 1")
        move = chess.Move.from_uci("f4d5")
        assert game_report._is_sacrifice(board, move) is True

    def test_capturing_something_on_the_way_in_offsets_what_gets_lost_back(self):
        # Rxd5 captures a bishop (3) but a pawn recaptures the rook (5) —
        # net -2 for White, a real sacrifice (worse than SACRIFICE_MIN_NET_CP).
        board = chess.Board("4k3/8/4p3/3b4/8/8/8/3RK3 w - - 0 1")
        move = chess.Move.from_uci("d1d5")
        assert game_report._is_sacrifice(board, move) is True

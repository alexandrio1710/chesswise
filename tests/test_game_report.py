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

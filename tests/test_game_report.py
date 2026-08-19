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

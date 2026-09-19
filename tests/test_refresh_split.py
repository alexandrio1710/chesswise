"""The dashboard's refreshes are separate: 'Sync & analyze' only fetches and
analyzes (quickly); puzzles and review data are their own steps."""

import time

import pytest
from fastapi.testclient import TestClient

import batch_analyze
import cli_state
import config
import db
import puzzles
import server

client = TestClient(server.app)


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    # Refreshing remembers the usernames it was given — never let a test write the real cli_config.json.
    monkeypatch.setattr(cli_state, "STATE_PATH", tmp_path / "cli_config.json")
    server._refresh_status.clear()
    server._puzzle_build_status.update(running=False, total=0, done=0, error=None, finished_at=None)
    yield
    server._refresh_status.clear()


def _wait(pred, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _stub(monkeypatch):
    calls = {"fetch": 0, "analyze": [], "puzzles": 0, "review": 0}
    monkeypatch.setattr(db, "fetch_and_store", lambda *a, **k: (calls.__setitem__("fetch", calls["fetch"] + 1),
                                                                 {"inserted": 3, "skipped": 0, "skipped_other_profile": 0})[1])
    monkeypatch.setattr(batch_analyze, "run_batch_analysis", lambda depth=15, workers=1: (calls["analyze"].append((depth, workers)), [1, 2, 3])[1])
    monkeypatch.setattr(puzzles, "generate_all_puzzles", lambda **k: calls.__setitem__("puzzles", calls["puzzles"] + 1))
    monkeypatch.setattr(server.review, "start_bulk_review", lambda: calls.__setitem__("review", calls["review"] + 1) or True)
    monkeypatch.setattr(server.alerts, "send_alerts_for_games", lambda ids: 0)
    return calls


def _finished():
    return server._refresh_status.get(server._LOCAL_KEY, {}).get("running") is False


class TestSyncAndAnalyze:
    def test_only_fetches_and_analyzes_at_the_quick_depth(self, monkeypatch):
        calls = _stub(monkeypatch)
        assert client.post("/api/refresh", json={"lichess_user": "someone"}).json() == {"status": "started", "depth": config.QUICK_ANALYSIS_DEPTH}
        assert _wait(_finished)
        assert calls["fetch"] == 1 and calls["analyze"] == [(config.QUICK_ANALYSIS_DEPTH, config.ANALYSIS_WORKERS)]
        assert calls["puzzles"] == 0 and calls["review"] == 0  # separate steps
        status = client.get("/api/refresh/status").json()
        assert status["error"] is None and status["result"]["inserted"] == 3 and status["result"]["analyzed"] == 3
        assert status["result"]["depth"] == config.QUICK_ANALYSIS_DEPTH

    def test_the_chosen_depth_is_used(self, monkeypatch):
        calls = _stub(monkeypatch)
        client.post("/api/refresh", json={"lichess_user": "someone", "depth": 18})
        assert _wait(_finished) and calls["analyze"][0][0] == 18

    def test_analyze_only_skips_the_fetch_and_needs_no_username(self, monkeypatch):
        calls = _stub(monkeypatch)
        monkeypatch.setattr(server, "_settings_for", lambda user: {})
        assert client.post("/api/refresh", json={"fetch": False}).status_code == 200
        assert _wait(_finished) and calls["fetch"] == 0 and len(calls["analyze"]) == 1

    def test_fetching_still_needs_a_username(self, monkeypatch):
        _stub(monkeypatch)
        monkeypatch.setattr(server, "_settings_for", lambda user: {})
        assert client.post("/api/refresh", json={}).status_code == 400

    def test_silly_depths_are_rejected(self):
        assert client.post("/api/refresh", json={"lichess_user": "x", "depth": 99}).status_code == 422

    def test_quick_is_the_default_depth_and_workers_use_more_cores(self):
        assert config.QUICK_ANALYSIS_DEPTH == 12 and config.ANALYSIS_WORKERS >= 2


class TestPuzzleBuild:
    def test_runs_as_its_own_job(self, monkeypatch):
        calls = _stub(monkeypatch)
        monkeypatch.setattr(puzzles, "get_mistakes_without_puzzles", lambda: [1, 2])
        assert client.post("/api/puzzle-build").json() == {"status": "started"}
        assert _wait(lambda: server._puzzle_build_status["running"] is False)
        assert calls["puzzles"] == 1 and calls["fetch"] == 0 and calls["analyze"] == []
        status = client.get("/api/puzzle-build/status").json()
        assert status["error"] is None and status["total"] == 2 and status["finished_at"]

    def test_a_second_start_while_running_is_a_409(self):
        server._puzzle_build_status["running"] = True
        assert client.post("/api/puzzle-build").status_code == 409


def test_pending_counts(monkeypatch):
    monkeypatch.setattr(batch_analyze, "get_unanalyzed_games", lambda: [1, 2, 3])
    monkeypatch.setattr(puzzles, "get_mistakes_without_puzzles", lambda: [1])
    monkeypatch.setattr(server.review, "games_needing_review", lambda game_ids=None: [7, 8])
    assert client.get("/api/pending").json() == {"unanalyzed_games": 3, "mistakes_without_puzzles": 1, "games_without_review": 2}

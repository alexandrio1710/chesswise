"""HTTP-level tests for the Game Review endpoints: access gating, the ready
payload for a reviewed game (both players' stats, per-move coach text), the
retry-a-mistake check, and the review-coverage status."""

import itertools

from fastapi.testclient import TestClient

import puzzles
import server
import test_server as ts
from config import SESSION_COOKIE_NAME  # noqa: F401  (imported for parity with test_server)
from db import get_connection

client = TestClient(server.app)
_counter = itertools.count(1)

# White to move can play the fork Ne6+ (best); Black's reply data isn't needed.
FORK_FEN = "3q1k2/8/8/2N5/8/8/8/4K3 w - - 0 1"
PGN = f'[Event "t"]\n[White "alice"]\n[Black "bob"]\n[SetUp "1"]\n[FEN "{FORK_FEN}"]\n[Result "*"]\n\n1. Kf2 Ke8 2. Ke3 Ke7 *\n'


def _insert_reviewed_game(user_id=None, reviewed=True) -> int:
    n = next(_counter)
    conn = get_connection()
    try:
        game_id = conn.execute(
            "INSERT INTO games (source, source_game_id, date, opponent, result, color, time_control, opening_name, pgn, "
            "analyzed, analyzed_at, reviewed_at, user_id) VALUES ('manual', ?, datetime('now'), 'bob', 'loss', 'white', "
            "'blitz', '', ?, 1, '2026-01-01 00:00:00', ?, ?)",
            (f"review-api-{n}", PGN, "2026-06-01 00:00:00" if reviewed else None, user_id),
        ).lastrowid
        rows = [
            # ply, color, san, eval_cp (white POV after), eval_before (mover POV), tier, classification, best, pv
            (1, "white", "Kf2", 100, 600, "blunder", "miss", "c5e6", "c5e6 f8e8 e6d8 e8d8"),
            (2, "black", "Ke8", 90, -100, "best", "best", "f8e8", "f8e8"),
            (3, "white", "Ke3", 80, 90, "good", "good", "c5e6", "c5e6 f8e8"),
            (4, "black", "Ke7", 70, -80, "best", "best", "e8e7", "e8e7"),
        ]
        for ply, color, san, cp, before, tier, cls, best, pv in rows:
            conn.execute(
                "INSERT INTO game_moves (game_id, ply, move_number, color_moved, move_san, eval_cp, eval_before_cp, eval_drop, "
                "tier, classification, phase, best_move_uci, best_pv_uci, best_eval_cp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 'endgame', ?, ?, 600)",
                (game_id, ply, (ply + 1) // 2, color, san, cp, before, tier, cls, best, pv),
            )
        conn.commit()
        return game_id
    finally:
        conn.close()


class TestReviewRoute:
    def test_missing_game_404s(self):
        assert client.get("/api/games/999999999/review").status_code == 404

    def test_unanalyzed_game_is_409(self):
        game_id = ts._insert_game(user_id=None)
        assert client.get(f"/api/games/{game_id}/review").status_code == 409

    def test_owned_game_is_hidden_from_other_users(self):
        owner, other = ts._make_user("review-owner"), ts._make_user("review-other")
        game_id = _insert_reviewed_game(user_id=owner["id"])
        assert client.get(f"/api/games/{game_id}/review", cookies=ts._cookie(other)).status_code == 404

    def test_reviewed_game_returns_the_full_payload(self):
        game_id = _insert_reviewed_game()
        body = client.get(f"/api/games/{game_id}/review").json()
        assert body["status"] == "ready"
        assert set(body["accuracy"]) == {"white", "black"}
        assert set(body["class_counts"]) == {"white", "black"}
        assert [m["san"] for m in body["moves"]] == ["Kf2", "Ke8", "Ke3", "Ke7"]
        assert body["players"]["white"]["name"] == "alice"
        assert body["start_fen"] == FORK_FEN

    def test_moves_carry_the_best_move_and_coach_text(self):
        body = client.get(f"/api/games/{_insert_reviewed_game()}/review").json()
        first = body["moves"][0]
        assert first["classification"] == "miss"
        assert first["best"]["san"] == "Ne6+"
        assert first["best"]["effect"] == "forks the queen and the king"
        assert first["explanation"]["headline"] == "Kf2 is a miss"
        assert "Ne6+" in first["explanation"]["text"]
        assert first["fen_before"] == FORK_FEN

    def test_key_moments_are_the_reviewed_players_notable_moves(self):
        body = client.get(f"/api/games/{_insert_reviewed_game()}/review").json()
        assert body["key_moments"] == [1]  # only White's miss; Black's moves aren't "yours"
        assert body["turning_point"]["san"] == "Kf2"

    def test_a_game_without_review_data_starts_a_background_job(self, monkeypatch):
        started = []
        monkeypatch.setattr(server.review, "ensure_reviewed", lambda gid: started.append(gid))
        game_id = _insert_reviewed_game(reviewed=False)
        body = client.get(f"/api/games/{game_id}/review").json()
        assert body["status"] == "computing"


class TestRetryRoute:
    def _post(self, game_id, ply, frm, to):
        return client.post(f"/api/games/{game_id}/retry", json={"ply": ply, "from": frm, "to": to})

    def test_the_best_move_is_correct_without_an_engine_call(self, monkeypatch):
        monkeypatch.setattr(puzzles, "evaluate_move", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no engine")))
        game_id = _insert_reviewed_game()
        body = self._post(game_id, 1, "c5", "e6").json()
        assert body["correct"] is True and body["verdict"] == "best"
        assert body["best_san"] == "Ne6+"

    def test_a_worse_move_is_judged_by_the_engine(self, monkeypatch):
        monkeypatch.setattr(puzzles, "evaluate_move", lambda fen, uci, depth=0: {"cp": 50.0, "mate_in": None})
        game_id = _insert_reviewed_game()
        body = self._post(game_id, 1, "e1", "f2").json()
        assert body["correct"] is False
        assert body["verdict"] in ("mistake", "blunder")
        assert body["played_san"] == "Kf2"

    def test_an_illegal_move_is_a_400(self):
        game_id = _insert_reviewed_game()
        assert self._post(game_id, 1, "e1", "e5").status_code == 400

    def test_an_unreviewed_position_is_a_400(self):
        game_id = _insert_reviewed_game()
        assert self._post(game_id, 99, "e1", "f2").status_code == 400


class TestReviewStatus:
    def test_reports_coverage_counts(self):
        _insert_reviewed_game(reviewed=True)
        body = client.get("/api/review/status").json()
        assert body["total"] >= body["reviewed"] >= 1
        assert body["pending"] == body["total"] - body["reviewed"]
        assert {"running", "total", "done"} <= set(body["job"])

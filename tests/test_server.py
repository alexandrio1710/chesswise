"""HTTP-level tests for the FastAPI app: the ownership/access gates added
to the legacy (pre-OAuth) game/note/goal/profile routes, the Analyze Board
hardening (ply cap + rate limit), and the per-user split of refresh/
settings state.

Uses direct SQL to set up fixture rows rather than the real fetch/analyze
pipeline (slow, and needs network access) or a real Lichess OAuth exchange
(needs network access) — auth.upsert_user/create_session and raw inserts
are enough to exercise the routes under test.
"""

import itertools

import chess
import chess.pgn
import pytest
from fastapi.testclient import TestClient

import auth
import manual_analysis
import server
from config import SESSION_COOKIE_NAME
from db import get_connection

client = TestClient(server.app)

_id_counter = itertools.count(1)


def _make_user(username: str) -> dict:
    return auth.upsert_user({"id": f"test-lichess-{next(_id_counter)}", "username": username})


def _cookie(user: dict) -> dict:
    raw_token, _ = auth.create_session(user["id"])
    return {SESSION_COOKIE_NAME: raw_token}


def _insert_game(user_id: int | None) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO games (source, source_game_id, date, opponent, result, color, "
            "time_control, opening_name, pgn, analyzed, user_id) "
            "VALUES ('manual', ?, datetime('now'), 'Bot', 'win', 'white', 'blitz', '', '', 0, ?)",
            (f"test-game-{next(_id_counter)}", user_id),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _insert_note(game_id: int) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO notes (game_id, text, created_at) VALUES (?, 'test note', datetime('now'))",
            (game_id,),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _insert_profile(user_id: int | None) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO profiles (name, created_at, user_id) VALUES (?, datetime('now'), ?)",
            (f"test-profile-{next(_id_counter)}", user_id),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _insert_goal(profile_id: int | None) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO goals (profile_id, metric, comparison, target_value, description, created_at) "
            "VALUES (?, 'accuracy', '>=', 80, 'test goal', datetime('now'))",
            (profile_id,),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


class TestGameAccess:
    def test_unowned_game_not_blocked_by_ownership(self):
        game_id = _insert_game(user_id=None)
        resp = client.get(f"/api/games/{game_id}")
        # Not analyzed, so the route itself 409s past the access gate —
        # the point here is it's not the 404 the ownership check would give.
        assert resp.status_code == 409

    def test_owned_game_hidden_when_logged_out(self):
        owner = _make_user("owner-game-1")
        game_id = _insert_game(user_id=owner["id"])
        resp = client.get(f"/api/games/{game_id}")
        assert resp.status_code == 404

    def test_owned_game_hidden_from_a_different_user(self):
        owner = _make_user("owner-game-2")
        other = _make_user("other-game-2")
        game_id = _insert_game(user_id=owner["id"])
        resp = client.get(f"/api/games/{game_id}", cookies=_cookie(other))
        assert resp.status_code == 404

    def test_owned_game_reachable_by_its_owner(self):
        owner = _make_user("owner-game-3")
        game_id = _insert_game(user_id=owner["id"])
        resp = client.get(f"/api/games/{game_id}", cookies=_cookie(owner))
        assert resp.status_code == 409  # past the access gate, blocked by "not analyzed" instead

    def test_nonexistent_game_404s(self):
        resp = client.get("/api/games/999999999")
        assert resp.status_code == 404

    def test_report_route_is_also_gated(self):
        owner = _make_user("owner-game-4")
        other = _make_user("other-game-4")
        game_id = _insert_game(user_id=owner["id"])
        resp = client.get(f"/api/games/{game_id}/report", cookies=_cookie(other))
        assert resp.status_code == 404

    def test_add_note_route_is_also_gated(self):
        owner = _make_user("owner-game-5")
        other = _make_user("other-game-5")
        game_id = _insert_game(user_id=owner["id"])
        resp = client.post(
            f"/api/games/{game_id}/notes", json={"text": "hi"}, cookies=_cookie(other),
        )
        assert resp.status_code == 404


class TestMistakeAccess:
    def test_mistake_on_owned_game_hidden_from_other_user(self):
        owner = _make_user("owner-mistake-1")
        other = _make_user("other-mistake-1")
        game_id = _insert_game(user_id=owner["id"])
        conn = get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO mistakes (game_id, ply, move_number, move_san, color_moved, phase, "
                "severity, eval_before, eval_after, eval_drop) "
                "VALUES (?, 1, 1, 'e4', 'white', 'opening', 'blunder', 0, -500, 500)",
                (game_id,),
            )
            mistake_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()

        resp = client.get(f"/api/mistakes/{mistake_id}/tablebase", cookies=_cookie(other))
        assert resp.status_code == 404

    def test_nonexistent_mistake_404s(self):
        resp = client.get("/api/mistakes/999999999/tablebase")
        assert resp.status_code == 404


class TestNoteDeleteAccess:
    def test_delete_nonexistent_note_404s(self):
        resp = client.delete("/api/notes/999999999")
        assert resp.status_code == 404

    def test_delete_note_requires_the_owning_game_users_session(self):
        owner = _make_user("owner-note-1")
        other = _make_user("other-note-1")
        game_id = _insert_game(user_id=owner["id"])
        note_id = _insert_note(game_id)

        resp = client.delete(f"/api/notes/{note_id}", cookies=_cookie(other))
        assert resp.status_code == 404

        resp = client.delete(f"/api/notes/{note_id}", cookies=_cookie(owner))
        assert resp.status_code == 200
        assert resp.json() == {"status": "deleted"}

    def test_delete_note_on_unowned_game_needs_no_login(self):
        game_id = _insert_game(user_id=None)
        note_id = _insert_note(game_id)
        resp = client.delete(f"/api/notes/{note_id}")
        assert resp.status_code == 200


class TestGoalDeleteAccess:
    def test_delete_nonexistent_goal_404s(self):
        resp = client.delete("/api/goals/999999999")
        assert resp.status_code == 404

    def test_delete_goal_scoped_to_the_owning_profile(self):
        owner = _make_user("owner-goal-1")
        other = _make_user("other-goal-1")
        profile_id = _insert_profile(owner["id"])
        goal_id = _insert_goal(profile_id)

        resp = client.delete(f"/api/goals/{goal_id}", cookies=_cookie(other))
        assert resp.status_code == 404

        resp = client.delete(f"/api/goals/{goal_id}", cookies=_cookie(owner))
        assert resp.status_code == 200

    def test_delete_goal_with_no_profile_needs_no_login(self):
        goal_id = _insert_goal(profile_id=None)
        resp = client.delete(f"/api/goals/{goal_id}")
        assert resp.status_code == 200


class TestProfileAccess:
    def test_delete_nonexistent_profile_404s(self):
        resp = client.delete("/api/profiles/999999999")
        assert resp.status_code == 404

    def test_delete_owned_profile_requires_owner(self):
        owner = _make_user("owner-profile-1")
        other = _make_user("other-profile-1")
        profile_id = _insert_profile(owner["id"])

        resp = client.delete(f"/api/profiles/{profile_id}", cookies=_cookie(other))
        assert resp.status_code == 404

        resp = client.delete(f"/api/profiles/{profile_id}", cookies=_cookie(owner))
        assert resp.status_code == 200

    def test_delete_unowned_profile_needs_no_login(self):
        profile_id = _insert_profile(None)
        resp = client.delete(f"/api/profiles/{profile_id}")
        assert resp.status_code == 200

    def test_link_username_route_is_also_gated(self):
        owner = _make_user("owner-profile-2")
        other = _make_user("other-profile-2")
        profile_id = _insert_profile(owner["id"])
        resp = client.post(
            f"/api/profiles/{profile_id}/links",
            json={"source": "lichess", "username": "someone"},
            cookies=_cookie(other),
        )
        assert resp.status_code == 404


def _long_pgn(num_plies: int) -> str:
    """A synthetic, always-legal PGN with `num_plies` half-moves — a knight
    bouncing back and forth, generated move-by-move (not raw SAN text) so
    it's guaranteed parseable regardless of how many plies are requested.
    """
    board = chess.Board()
    game = chess.pgn.Game()
    node = game
    made = 0
    cycle = ("g1f3", "g8f6", "f3g1", "f6g8")
    while made < num_plies:
        move = chess.Move.from_uci(cycle[made % len(cycle)])
        node = node.add_variation(move)
        board.push(move)
        made += 1
    return game.accept(chess.pgn.StringExporter(headers=True, variations=False, comments=False))


class TestAnalyzePgnCap:
    def test_pgn_over_ply_cap_is_rejected_before_any_engine_work(self):
        pgn = _long_pgn(manual_analysis.MAX_ANALYSIS_PLIES + 10)
        resp = client.post("/api/analyze/pgn", json={"pgn": pgn})
        assert resp.status_code == 400
        assert "ply" in resp.json()["detail"] or "half-moves" in resp.json()["detail"]

    def test_pgn_under_ply_cap_is_not_rejected_by_the_cap(self):
        # Confirms the cap check itself doesn't misfire for ordinary input
        # (a bad/short PGN can still 400 for other reasons — just not this one).
        assert manual_analysis._count_plies(_long_pgn(10)) == 10


class TestAnalyzeRateLimit:
    def test_returns_429_after_the_per_ip_threshold(self):
        server._analyze_request_log.clear()
        for _ in range(server._ANALYZE_RATE_LIMIT):
            resp = client.post("/api/analyze/fen", json={"fen": "not-a-real-fen"})
            assert resp.status_code == 400
        resp = client.post("/api/analyze/fen", json={"fen": "not-a-real-fen"})
        assert resp.status_code == 429
        server._analyze_request_log.clear()


class TestSettingsPerUser:
    def test_logged_in_users_get_isolated_settings(self):
        user_a = _make_user("settings-a")
        user_b = _make_user("settings-b")

        client.post("/api/settings", json={"lichess_user": "alice"}, cookies=_cookie(user_a))
        client.post("/api/settings", json={"lichess_user": "bob"}, cookies=_cookie(user_b))

        resp_a = client.get("/api/settings", cookies=_cookie(user_a))
        resp_b = client.get("/api/settings", cookies=_cookie(user_b))
        assert resp_a.json().get("lichess_user") == "alice"
        assert resp_b.json().get("lichess_user") == "bob"


class TestRefreshStatusPerUser:
    def test_refresh_status_is_isolated_per_user(self):
        user_a = _make_user("refresh-a")
        user_b = _make_user("refresh-b")

        server._refresh_status[user_a["id"]] = {
            "running": True, "started_at": "x", "finished_at": None, "error": None, "result": None,
        }

        resp_b = client.get("/api/refresh/status", cookies=_cookie(user_b))
        assert resp_b.json()["running"] is False

        resp_a = client.get("/api/refresh/status", cookies=_cookie(user_a))
        assert resp_a.json()["running"] is True


def _insert_puzzle(user_id: int | None) -> int:
    conn = get_connection()
    try:
        game_id = conn.execute(
            "INSERT INTO games (source, source_game_id, date, result, color, analyzed, user_id) "
            "VALUES ('manual', ?, datetime('now'), 'win', 'white', 1, ?)",
            (f"test-puzzle-game-{next(_id_counter)}", user_id),
        ).lastrowid
        mistake_id = conn.execute(
            "INSERT INTO mistakes (game_id, ply, move_number, move_san, color_moved, phase, "
            "severity, eval_before, eval_after, eval_drop) "
            "VALUES (?, 1, 1, 'e4', 'white', 'opening', 'blunder', 0, -500, 500)",
            (game_id,),
        ).lastrowid
        puzzle_id = conn.execute(
            "INSERT INTO puzzles (mistake_id, game_id, fen_before, side_to_move, played_move_san, "
            "best_move_uci, best_move_san, top_lines, phase, severity, created_at, user_id) "
            "VALUES (?, ?, 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1', 'white', "
            "'e4', 'd4', 'd4', '[]', 'opening', 'blunder', datetime('now'), ?)",
            (mistake_id, game_id, user_id),
        ).lastrowid
        conn.commit()
        return puzzle_id
    finally:
        conn.close()


class TestPuzzleAccess:
    def test_unowned_puzzle_visible_without_login(self):
        puzzle_id = _insert_puzzle(user_id=None)
        resp = client.get(f"/api/puzzles/{puzzle_id}")
        assert resp.status_code == 200

    def test_owned_puzzle_hidden_from_other_user(self):
        owner = _make_user("owner-puzzle-1")
        other = _make_user("other-puzzle-1")
        puzzle_id = _insert_puzzle(user_id=owner["id"])

        resp = client.get(f"/api/puzzles/{puzzle_id}")
        assert resp.status_code == 404

        resp = client.get(f"/api/puzzles/{puzzle_id}", cookies=_cookie(other))
        assert resp.status_code == 404

        resp = client.get(f"/api/puzzles/{puzzle_id}", cookies=_cookie(owner))
        assert resp.status_code == 200

    def test_attempt_route_is_also_gated(self):
        owner = _make_user("owner-puzzle-2")
        other = _make_user("other-puzzle-2")
        puzzle_id = _insert_puzzle(user_id=owner["id"])
        resp = client.post(
            f"/api/puzzles/{puzzle_id}/attempt", json={"from": "e2", "to": "e4"}, cookies=_cookie(other),
        )
        assert resp.status_code == 404


class TestProfileFilterAccess:
    """profile_id is an optional FILTER on ~12 stats/insights/search/export
    routes, not a path resource — one representative route (/api/summary)
    is enough to exercise the shared auth.require_profile_filter_access
    dependency; every other route wires the exact same dependency.
    """

    def test_no_filter_is_never_blocked(self):
        resp = client.get("/api/summary")
        assert resp.status_code == 200

    def test_unowned_profile_filter_is_allowed(self):
        profile_id = _insert_profile(None)
        resp = client.get(f"/api/summary?profile_id={profile_id}")
        assert resp.status_code == 200

    def test_someone_elses_profile_filter_is_rejected(self):
        owner = _make_user("owner-filter-1")
        other = _make_user("other-filter-1")
        profile_id = _insert_profile(owner["id"])

        resp = client.get(f"/api/summary?profile_id={profile_id}")
        assert resp.status_code == 404

        resp = client.get(f"/api/summary?profile_id={profile_id}", cookies=_cookie(other))
        assert resp.status_code == 404

        resp = client.get(f"/api/summary?profile_id={profile_id}", cookies=_cookie(owner))
        assert resp.status_code == 200

    def test_progress_and_export_routes_use_the_same_gate(self):
        owner = _make_user("owner-filter-2")
        profile_id = _insert_profile(owner["id"])

        assert client.get(f"/api/progress?profile_id={profile_id}").status_code == 404
        assert client.get(f"/api/export/stats?profile_id={profile_id}").status_code == 404
        assert client.get(f"/api/progress?profile_id={profile_id}", cookies=_cookie(owner)).status_code == 200


class TestProfileListingAndCompare:
    def test_listing_only_shows_unowned_and_own_profiles(self):
        owner = _make_user("owner-list-1")
        other = _make_user("other-list-1")
        owned_id = _insert_profile(owner["id"])

        resp = client.get("/api/profiles", cookies=_cookie(other))
        ids = {p["id"] for p in resp.json()}
        assert owned_id not in ids

        resp = client.get("/api/profiles", cookies=_cookie(owner))
        ids = {p["id"] for p in resp.json()}
        assert owned_id in ids

    def test_compare_requires_access_to_both_profiles(self):
        owner = _make_user("owner-compare-1")
        other = _make_user("other-compare-1")
        profile_a = _insert_profile(owner["id"])
        profile_b = _insert_profile(None)

        resp = client.get(f"/api/profiles/compare?a={profile_a}&b={profile_b}", cookies=_cookie(other))
        assert resp.status_code == 404

        resp = client.get(f"/api/profiles/compare?a={profile_a}&b={profile_b}", cookies=_cookie(owner))
        assert resp.status_code == 200


class TestGoalCreateAccess:
    def test_creating_a_goal_on_someone_elses_profile_is_rejected(self):
        owner = _make_user("owner-goalcreate-1")
        other = _make_user("other-goalcreate-1")
        profile_id = _insert_profile(owner["id"])

        body = {"description": "test", "metric": "accuracy_avg", "comparison": "above", "target_value": 80, "profile_id": profile_id}
        resp = client.post("/api/goals", json=body, cookies=_cookie(other))
        assert resp.status_code == 404

        resp = client.post("/api/goals", json=body, cookies=_cookie(owner))
        assert resp.status_code == 200

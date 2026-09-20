"""Skills & plan: mistake causes, the six skill measures, opening leaks, and the plan."""

import pytest
from fastapi.testclient import TestClient

import server
import skills
from insights import Filters
from test_insights_report import PGN, _game, _profile, _tactic

client = TestClient(server.app)


def _m(ply, color, cls="blunder", phase="middlegame", eval_cp=0, drop=300, clock=None, before=0):
    return {"ply": ply, "color_moved": color, "classification": cls, "tier": cls, "phase": phase, "eval_cp": eval_cp,
            "eval_before_cp": before, "eval_drop": drop, "clock_seconds_remaining": clock, "move_san": "x", "move_number": (ply + 1) // 2}


def _g(moves, color="white", result="loss", base=300, **kw):
    return {"id": 1, "color": color, "result": result, "moves": moves, "base_seconds": base, "reviewed": True, "opponent": "o",
            "date": "2026-01-01", "termination_method": None, "book_plies": 4, "maccs": {}, "acc": None, "opening_name": "X: Y", **kw}


class TestClassifyMistake:
    def test_priority_order(self):
        g = _g([_m(5, "white")])
        m = g["moves"][0]
        assert skills.classify_mistake(g, m, {}) == "judgment"
        assert skills.classify_mistake(g, m, {(1, 5, "white"): [("left_hanging", "punished")]}) == "hung_piece"
        assert skills.classify_mistake(g, m, {(1, 6, "black"): [("fork", "found")]}) == "allowed_tactic"
        assert skills.classify_mistake(g, m, {(1, 5, "white"): [("mate", "missed")]}) == "missed_mate"
        assert skills.classify_mistake(g, m, {(1, 5, "white"): [("pin", "missed")]}) == "missed_tactic"
        assert skills.classify_mistake(g, m, {(1, 5, "white"): [("free_piece", "ignored")]}) == "missed_free_piece"
        # hanging outranks everything, a walked-into tactic outranks a missed one
        both = {(1, 5, "white"): [("left_hanging", "punished"), ("pin", "missed")], (1, 6, "black"): [("fork", "found")]}
        assert skills.classify_mistake(g, m, both) == "hung_piece"
        assert skills.classify_mistake(g, m, {(1, 5, "white"): [("pin", "missed")], (1, 6, "black"): [("fork", "found")]}) == "allowed_tactic"

    def test_time_trouble_and_phases(self):
        assert skills.classify_mistake(_g([_m(5, "white", clock=20)]), _m(5, "white", clock=20), {}) == "time_trouble"  # 20s <= 10% of 300
        assert skills.classify_mistake(_g([]), _m(5, "white", clock=200), {}) == "judgment"
        assert skills.classify_mistake(_g([]), _m(5, "white", phase="opening"), {}) == "opening"
        assert skills.classify_mistake(_g([]), _m(5, "white", phase="endgame"), {}) == "endgame"
        assert skills.classify_mistake(_g([], base=None), _m(5, "white", clock=1), {}) == "judgment"  # no known base time: can't call it time trouble

    def test_the_opponents_own_moves_are_not_your_mistakes(self):
        g = _g([_m(2, "black"), _m(3, "white", cls="best")])
        assert skills._mistakes([g], {}) == []


class TestSustainedAdvantage:
    def test_needs_two_consecutive_plies(self):
        blip = _g([_m(1, "white", eval_cp=400), _m(2, "black", eval_cp=0), _m(3, "white", eval_cp=400)])
        assert skills._sustained_from(blip, 300) is None
        held = _g([_m(1, "white", eval_cp=0), _m(2, "black", eval_cp=350), _m(3, "white", eval_cp=380)])
        assert skills._sustained_from(held, 300) == 1

    def test_black_sees_the_eval_from_its_own_side(self):
        g = _g([_m(1, "white", eval_cp=-400), _m(2, "black", eval_cp=-500)], color="black")
        assert skills._sustained_from(g, 300) == 0 and skills._sustained_from(g, 300, sign=-1) is None
        assert skills._sustained_from(_g([_m(1, "white", eval_cp=-400), _m(2, "black", eval_cp=-500)]), 300, sign=-1) == 0


class TestBuild:
    def test_end_to_end_on_stored_games(self):
        pid = _profile()
        wins = _game(pid, result="win")
        lost = _game(pid, result="loss")
        _tactic(lost, 5, "white", "left_hanging", "punished", best=None, played="f1c4")
        report = skills.build(Filters(profile_id=pid))
        assert report["coverage"] == {"games": 2, "reviewed": 2, "pending": 0}
        assert {s["key"] for s in report["skills"]} == {"opening", "tactics", "endgames", "advantage", "resourcefulness", "time"}
        assert all(s["low_sample"] for s in report["skills"])  # two games is noise, and the page says so
        assert isinstance(report["plan"], list) and report["anatomy"]["total"] >= 0

    def test_a_loss_decided_by_a_hanging_piece_tops_the_plan(self):
        pid = _profile()
        moves = [(1, "white", "e4", 30, "best", "book", "opening"), (2, "black", "e5", 30, "best", "book", "opening"),
                 (3, "white", "Nf3", 30, "best", "best", "middlegame"), (4, "black", "Nc6", 25, "best", "best", "middlegame"),
                 (5, "white", "Bb5", -400, "blunder", "blunder", "middlegame"), (6, "black", "a6", -420, "best", "best", "middlegame")]
        gid = _game(pid, result="loss", moves=moves)
        from db import get_connection
        conn = get_connection()
        try:
            conn.execute("UPDATE game_moves SET eval_drop = 430 WHERE game_id = ? AND ply = 5", (gid,))
            conn.commit()
        finally:
            conn.close()
        _tactic(gid, 5, "white", "left_hanging", "punished", best=None, played="f1b5")
        report = skills.build(Filters(profile_id=pid))
        assert report["anatomy"]["decisive"]["causes"][0]["key"] == "hung_piece"
        top = report["plan"][0]
        assert top["key"] == "hung_piece" and top["points_lost"] == 1.0
        assert {"label": "Train hanging piece puzzles", "href": "/puzzles?theme=hanging_piece"} in top["actions"]

    def test_examples_replay_the_position(self):
        pid = _profile()
        gid = _game(pid, result="loss", moves=[(5, "white", "Bb5", -400, "blunder", "blunder", "middlegame")])
        from db import get_connection
        conn = get_connection()
        try:
            conn.execute("UPDATE game_moves SET eval_drop = 400 WHERE game_id = ?", (gid,))
            conn.commit()
        finally:
            conn.close()
        _tactic(gid, 5, "white", "left_hanging", "punished", best=None, played="f1b5")
        (ex,) = skills.mistake_examples(Filters(profile_id=pid), "hung_piece")
        assert ex["game_id"] == gid and ex["turn"] == "white" and ex["played_san"] == "Bb5" and ex["fen"].count("/") == 7


class TestEndpoints:
    def test_skills_endpoint_and_validation(self):
        pid = _profile()
        _game(pid)
        body = client.get("/api/skills", params={"profile_id": pid}).json()
        assert {"skills", "anatomy", "plan", "convert", "openings", "coverage"} <= set(body)
        assert client.get("/api/skills", params={"time_class": "nope"}).status_code == 422

    def test_examples_endpoint(self):
        assert client.get("/api/skills/examples", params={"cause": "bogus"}).status_code == 422
        assert "examples" in client.get("/api/skills/examples", params={"cause": "hung_piece"}).json()


class TestRecentGames:
    def test_lists_latest_first_with_accuracy_and_blunder_counts(self):
        import stats
        from db import get_connection
        pid = _profile()
        older = _game(pid, date="2026-05-01T10:00:00+00:00")
        newer = _game(pid, date="2026-05-02T10:00:00+00:00", result="loss")
        conn = get_connection()
        try:
            conn.execute("INSERT INTO mistakes (game_id, move_number, move_san, phase, severity, eval_drop, ply, color_moved) "
                         "VALUES (?, 3, 'x', 'middlegame', 'blunder', 400, 5, 'white')", (newer,))
            conn.commit()
        finally:
            conn.close()
        rows = stats.recent_games(profile_id=pid, limit=5)
        assert [r["game_id"] for r in rows] == [newer, older]
        assert rows[0]["blunders"] == 1 and rows[1]["blunders"] == 0
        assert rows[0]["accuracy"] is not None and 0 <= rows[0]["accuracy"] <= 100
        assert client.get("/api/recent-games", params={"profile_id": pid, "limit": 1}).json()[0]["game_id"] == newer

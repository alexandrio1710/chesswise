"""Tests for the filtered Insights report (insights_report.py) and its HTTP
endpoints. Every test isolates its games behind its own profile, since the
suite shares one throwaway database."""

import itertools

import pytest
from fastapi.testclient import TestClient

import insights
import insights_report
import server
import stats
from db import get_connection
from insights import Filters

client = TestClient(server.app)
_counter = itertools.count(1)

PGN = '[Event "t"]\n[White "me"]\n[Black "them"]\n[Result "*"]\n\n1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 *\n'
# ply, color, san, eval_cp (white POV), tier, classification, phase
MOVES = [
    (1, "white", "e4", 30, "best", "book", "opening"), (2, "black", "e5", 30, "best", "book", "opening"),
    (3, "white", "Nf3", 30, "best", "best", "opening"), (4, "black", "Nc6", 25, "excellent", "excellent", "opening"),
    (5, "white", "Bb5", 25, "best", "best", "middlegame"), (6, "black", "a6", -150, "blunder", "blunder", "middlegame"),
    (7, "white", "O-O", 180, "best", "best", "middlegame"), (8, "black", "Nf6", 170, "good", "good", "endgame"),
]


def _profile() -> int:
    conn = get_connection()
    try:
        pid = conn.execute("INSERT INTO profiles (name, created_at) VALUES (?, datetime('now'))",
                           (f"insights-report-{next(_counter)}",)).lastrowid
        conn.commit()
        return pid
    finally:
        conn.close()


def _game(pid, *, date="2026-03-02T12:00:00+00:00", color="white", result="win", tc="blitz", opening="Sicilian Defense: Najdorf",
          eco="B90", termination="resignation", shape="balanced", book_plies=6, reviewed=True, moves=MOVES,
          player_rating=1500, opponent_rating=1500) -> int:
    conn = get_connection()
    try:
        gid = conn.execute(
            "INSERT INTO games (source, source_game_id, date, opponent, result, color, time_control, opening_name, eco, pgn, analyzed, "
            "analyzed_at, reviewed_at, termination_method, game_shape, book_plies, player_rating, opponent_rating, profile_id) "
            "VALUES ('lichess', ?, ?, 'them', ?, ?, ?, ?, ?, ?, 1, '2026-01-01 00:00:00', ?, ?, ?, ?, ?, ?, ?)",
            (f"ir-{next(_counter)}", date, result, color, tc, opening, eco, PGN, "2026-06-01 00:00:00" if reviewed else None,
             termination, shape, book_plies, player_rating, opponent_rating, pid),
        ).lastrowid
        for ply, mover, san, cp, tier, cls, phase in moves:
            conn.execute(
                "INSERT INTO game_moves (game_id, ply, move_number, color_moved, move_san, eval_cp, eval_drop, tier, classification, phase, "
                "best_move_uci) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
                (gid, ply, (ply + 1) // 2, mover, san, cp, tier, cls, phase, "e2e4"),
            )
        conn.commit()
        return gid
    finally:
        conn.close()


def _tactic(gid, ply, color, motif, outcome, gain=3.0, best="f1b5", played="f1c4"):
    conn = get_connection()
    try:
        conn.execute("INSERT INTO game_tactics (game_id, ply, color, motif, outcome, gain, best_uci, played_uci) VALUES (?,?,?,?,?,?,?,?)",
                     (gid, ply, color, motif, outcome, gain, best, played))
        conn.commit()
    finally:
        conn.close()


class TestFilters:
    def test_clause_adds_each_extra_filter(self):
        where, params = insights._source_clause(None, None, flt=Filters(time_class="rapid", color="black", date_from="2026-01-01", date_to="2026-02-01"))
        assert "g.time_control = ?" in where and "g.color = ?" in where
        assert params == ("rapid", "black", "2026-01-01", "2026-02-01")

    def test_no_filters_is_an_empty_clause(self):
        assert insights._source_clause(None, None, flt=Filters()) == ("", ())

    def test_report_respects_time_class_color_and_dates(self):
        pid = _profile()
        _game(pid, tc="blitz", color="white", date="2026-03-02T12:00:00+00:00")
        _game(pid, tc="rapid", color="white", date="2026-03-03T12:00:00+00:00")
        _game(pid, tc="blitz", color="black", date="2026-04-10T12:00:00+00:00")
        n = lambda **kw: insights_report.build_report(Filters(profile_id=pid, **kw))["overview"]["games"]
        assert n() == 3
        assert n(time_class="blitz") == 2
        assert n(color="black") == 1
        assert n(date_from="2026-03-03", date_to="2026-03-31") == 1


class TestOverviewAndResults:
    def test_record_and_accuracy(self):
        pid = _profile()
        _game(pid, result="win")
        _game(pid, result="loss")
        _game(pid, result="draw", termination="agreement")
        report = insights_report.build_report(Filters(profile_id=pid))
        o = report["overview"]
        assert (o["games"], o["wins"], o["draws"], o["losses"]) == (3, 1, 1, 1)
        assert o["win_rate_pct"] == pytest.approx(33.3, abs=0.1)
        assert o["accuracy"] is not None and 0 <= o["accuracy"] <= 100

    def test_results_are_labelled_from_your_side(self):
        pid = _profile()
        _game(pid, result="win", termination="resignation")
        _game(pid, result="loss", termination="timeout")
        _game(pid, result="loss", termination="checkmate")
        results = insights_report.build_report(Filters(profile_id=pid))["results"]
        assert results["win"] == [{"method": "resignation", "count": 1, "label": "Opponent resigned"}]
        assert {r["label"] for r in results["loss"]} == {"You ran out of time", "You were checkmated"}

    def test_shapes_count_games_and_results(self):
        pid = _profile()
        _game(pid, shape="sharp", result="win")
        _game(pid, shape="sharp", result="loss")
        _game(pid, shape="smooth", result="win")
        shapes = {s["shape"]: s for s in insights_report.build_report(Filters(profile_id=pid))["shapes"]}
        assert shapes["sharp"]["games"] == 2 and shapes["sharp"]["win_rate_pct"] == 50.0
        assert shapes["smooth"]["wins"] == 1 and "balanced" not in shapes

    def test_over_time_buckets_by_granularity(self):
        pid = _profile()
        _game(pid, date="2026-03-02T12:00:00+00:00")
        _game(pid, date="2026-03-03T12:00:00+00:00")
        o = insights_report.build_report(Filters(profile_id=pid))["overview"]
        assert o["granularity"] == "day" and [p["games"] for p in o["over_time"]] == [1, 1]

    def test_accuracy_by_move_number_needs_enough_samples(self):
        pid = _profile()
        for _ in range(3):
            _game(pid)
        rows = insights_report.build_report(Filters(profile_id=pid))["overview"]["accuracy_by_move"]
        assert rows and rows[0]["move"] == 1 and rows[0]["samples"] == 3


class TestPhasesAndOpenings:
    def test_phase_accuracy_and_error_rate(self):
        pid = _profile()
        _game(pid, color="black")
        phases = {p["phase"]: p for p in insights_report.build_report(Filters(profile_id=pid))["phases"]["phases"]}
        assert phases["opening"]["you"]["moves"] == 2 and phases["opening"]["opp"]["moves"] == 2
        assert phases["middlegame"]["you"]["errors_per_100"] == 100.0  # black's only middlegame move is the a6 blunder
        assert phases["endgame"]["you"]["errors_per_100"] == 0.0

    def test_openings_group_by_family_with_variations_and_book_depth(self):
        pid = _profile()
        _game(pid, color="white", opening="Sicilian Defense: Najdorf", book_plies=5)
        _game(pid, color="white", opening="Sicilian Defense: Dragon", book_plies=8)
        _game(pid, color="black", opening="French Defense", eco="C00", book_plies=5)
        openings = insights_report.build_report(Filters(profile_id=pid))["openings"]
        white = openings["white"][0]
        assert white["opening"] == "Sicilian Defense" and white["games"] == 2
        assert {v["name"] for v in white["variations"]} == {"Sicilian Defense: Najdorf", "Sicilian Defense: Dragon"}
        assert white["avg_book_moves"] == 3.5 and white["max_book_moves"] == 4  # white owns ceil(5/2)=3 and ceil(8/2)=4
        assert openings["black"][0]["avg_book_moves"] == 2.0  # black owns floor(5/2)


class TestTacticsAndMoves:
    def test_found_and_missed_are_split_by_side(self):
        pid = _profile()
        gid = _game(pid, color="white")
        _tactic(gid, 5, "white", "fork", "found")
        _tactic(gid, 5, "white", "fork", "missed")
        _tactic(gid, 6, "black", "fork", "missed")
        _tactic(gid, 5, "white", "free_piece", "taken")
        _tactic(gid, 6, "black", "left_hanging", "punished")
        t = insights_report.build_report(Filters(profile_id=pid))["tactics"]
        fork = next(r for r in t["found_missed"] if r["motif"] == "fork")
        assert (fork["you"]["found"], fork["you"]["missed"], fork["opp"]["missed"]) == (1, 1, 1)
        assert fork["you"]["rate_pct"] == 50.0
        assert t["free_pieces"]["you"]["taken"] == 1 and t["left_hanging"]["opp"]["punished"] == 1

    def test_tactic_examples_replay_the_position(self):
        pid = _profile()
        gid = _game(pid)
        _tactic(gid, 5, "white", "pin", "missed", gain=4.0)
        (ex,) = insights_report.tactic_examples(Filters(profile_id=pid), "pin", "you", "missed")
        assert ex["game_id"] == gid and ex["turn"] == "white"
        assert ex["fen"].startswith("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R")
        assert (ex["best_san"], ex["played_san"]) == ("Bb5", "Bc4")
        assert insights_report.tactic_examples(Filters(profile_id=pid), "pin", "opp", "missed") == []

    def test_move_quality_only_counts_reviewed_games(self):
        pid = _profile()
        _game(pid, reviewed=True)
        _game(pid, reviewed=False)
        report = insights_report.build_report(Filters(profile_id=pid))
        assert report["coverage"] == {"games": 2, "reviewed": 1, "pending": 1}
        q = report["moves"]["quality"]["you"]
        assert q["total"] == 4 and q["counts"]["book"] == 1 and q["counts"]["best"] == 3

    def test_pieces_and_castling(self):
        pid = _profile()
        _game(pid, color="white", result="win")
        _game(pid, color="black", result="loss")
        m = insights_report.build_report(Filters(profile_id=pid))["moves"]
        pieces = {p["piece"]: p for p in m["pieces"]}
        assert pieces["K"]["moves"] == 1  # white's castling counts as a king move
        assert pieces["P"]["moves"] == 3  # e4, e5, a6
        castling = {c["side"]: c for c in m["castling"]}
        assert castling["kingside"]["games"] == 1 and castling["none"]["games"] == 1


class TestCalendar:
    def test_hours_and_weekdays_use_the_viewers_timezone(self):
        pid = _profile()
        _game(pid, date="2026-03-02T01:30:00+00:00")  # Monday 01:30 UTC
        utc = insights_report.build_report(Filters(profile_id=pid), tz_offset=0)["calendar"]
        assert utc["hour"][1]["games"] == 1 and utc["weekday"][0]["games"] == 1
        # JS getTimezoneOffset() for UTC-5 is +300: local time is Sunday 20:30.
        local = insights_report.build_report(Filters(profile_id=pid), tz_offset=300)["calendar"]
        assert local["hour"][20]["games"] == 1 and local["weekday"][6]["games"] == 1
        assert local["days"] == [{"date": "2026-03-01", "games": 1, "wins": 1, "draws": 0, "losses": 0,
                                  "win_rate_pct": 100.0, "draw_rate_pct": 0.0, "loss_rate_pct": 0.0}]


class TestMoveAccuracies:
    def test_per_move_accuracy_is_perfect_when_the_position_does_not_worsen(self):
        moves = [{"ply": 1, "color_moved": "white", "eval_cp": 30}, {"ply": 2, "color_moved": "black", "eval_cp": 30}]
        assert stats.move_accuracies(moves) == {1: 100.0, 2: 100.0}

    def test_a_blunder_scores_low_for_the_mover_only(self):
        moves = [{"ply": 1, "color_moved": "white", "eval_cp": 30}, {"ply": 2, "color_moved": "black", "eval_cp": 30},
                 {"ply": 3, "color_moved": "white", "eval_cp": -600}]
        acc = stats.move_accuracies(moves)
        assert acc[3] < 20 and acc[1] == 100.0 and acc[2] == 100.0


class TestEndpoints:
    def test_report_endpoint_returns_every_section(self):
        pid = _profile()
        _game(pid)
        body = client.get("/api/insights/report", params={"profile_id": pid, "tz_offset": 0}).json()
        assert {"overview", "results", "shapes", "phases", "openings", "tactics", "moves", "calendar", "legacy", "coverage"} <= set(body)
        assert body["overview"]["games"] == 1

    def test_range_preset_becomes_a_date_filter(self):
        pid = _profile()
        _game(pid, date="2020-01-01T00:00:00+00:00")
        body = client.get("/api/insights/report", params={"profile_id": pid, "range": "30d"}).json()
        assert body["overview"]["games"] == 0 and body["filters"]["date_from"]

    @pytest.mark.parametrize("params", [{"time_class": "hyperbullet"}, {"color": "green"}, {"range": "forever"}, {"date_from": "yesterday"}])
    def test_bad_filters_are_422(self, params):
        assert client.get("/api/insights/report", params=params).status_code == 422

    def test_tactic_examples_endpoint(self):
        pid = _profile()
        gid = _game(pid)
        _tactic(gid, 5, "white", "pin", "missed")
        body = client.get("/api/insights/tactic-examples", params={"profile_id": pid, "motif": "pin", "side": "you", "outcome": "missed"}).json()
        assert len(body["examples"]) == 1
        assert client.get("/api/insights/tactic-examples", params={"motif": "pin", "side": "them", "outcome": "missed"}).status_code == 422


class TestGeography:
    def _opponent_game(self, pid, name, source="chesscom", result="win", rating=1500):
        gid = _game(pid, result=result, opponent_rating=rating)
        conn = get_connection()
        try:
            conn.execute("UPDATE games SET opponent = ?, source = ? WHERE id = ?", (name, source, gid))
            conn.commit()
        finally:
            conn.close()
        return gid

    def test_lookup_caches_results_and_groups_games_by_country(self, monkeypatch):
        import geography
        pid = _profile()
        tag = next(_counter)
        a, b, c = f"geo-a-{tag}", f"geo-b-{tag}", f"geo-c-{tag}"
        self._opponent_game(pid, a, result="win", rating=1400)
        self._opponent_game(pid, a, result="loss", rating=1600)
        self._opponent_game(pid, b, result="win")
        self._opponent_game(pid, c, result="loss")
        answers = {a: ("ok", "US"), b: ("ok", "US"), c: ("none", None)}
        calls = []
        monkeypatch.setattr(geography, "_fetch_country", lambda source, name: (calls.append(name), answers[name])[1])
        flt = Filters(profile_id=pid)

        assert len(geography.pending_opponents(flt)) == 3
        geography.run_lookup(flt, delay=0)
        assert sorted(calls) == sorted([a, b, c]) and geography.pending_opponents(flt) == []
        geography.run_lookup(flt, delay=0)
        assert len(calls) == 3  # cached — nothing asked twice

        report = geography.report(flt)
        (us,) = report["countries"]
        assert (us["code"], us["name"], us["games"], us["opponents"]) == ("US", "United States", 3, 2)
        assert us["avg_opponent_rating"] == 1500 and us["wins"] == 2 and us["losses"] == 1
        assert report["no_country"] == 1 and report["unresolved"] == 0 and report["located"] == 2
        assert report["frequent_opponents"][0]["opponent"] == a and report["frequent_opponents"][0]["games"] == 2

    def test_errors_stay_pending_until_they_are_stale(self, monkeypatch):
        import geography
        pid = _profile()
        name = f"geo-err-{next(_counter)}"
        self._opponent_game(pid, name)
        monkeypatch.setattr(geography, "_fetch_country", lambda source, n: ("error", None))
        flt = Filters(profile_id=pid)
        geography.run_lookup(flt, delay=0)
        assert geography.pending_opponents(flt) == []  # just failed: not retried for an hour
        conn = get_connection()
        try:
            conn.execute("UPDATE opponent_countries SET fetched_at = datetime('now', '-2 hours') WHERE username = ?", (name,))
            conn.commit()
        finally:
            conn.close()
        assert geography.pending_opponents(flt) == [("chesscom", name)]
        assert geography.report(flt)["unresolved"] == 1

    def test_chesscom_and_lichess_profile_parsing(self, monkeypatch):
        import fetchers
        import geography

        class Resp:
            def __init__(self, status, body):
                self.status_code, self._body = status, body

            def json(self):
                return self._body

        replies = {
            "api.chess.com": Resp(200, {"country": "https://api.chess.com/pub/country/DE"}),
            "lichess.org": Resp(200, {"profile": {"country": "fr"}}),
        }
        monkeypatch.setattr(fetchers, "_request_with_retry", lambda method, url, **kw: next(v for k, v in replies.items() if k in url))
        assert geography._fetch_country("chesscom", "Someone") == ("ok", "DE")
        assert geography._fetch_country("lichess", "someone") == ("ok", "FR")
        replies["lichess.org"] = Resp(200, {"profile": {}})
        assert geography._fetch_country("lichess", "someone") == ("none", None)
        replies["api.chess.com"] = Resp(404, {})
        assert geography._fetch_country("chesscom", "gone") == ("none", None)
        replies["api.chess.com"] = Resp(503, {})
        assert geography._fetch_country("chesscom", "flaky") == ("error", None)

    def test_country_names(self):
        import countries
        assert countries.country_name("US") == "United States" and countries.country_name("GB-ENG") == "England"
        assert countries.country_name("ZZ") == "ZZ" and countries.country_name(None) == "Unknown"

    def test_endpoints(self, monkeypatch):
        import geography
        pid = _profile()
        self._opponent_game(pid, f"geo-ep-{next(_counter)}")
        monkeypatch.setattr(geography, "run_lookup", lambda flt, delay=0: None)  # don't hit the network from the thread
        body = client.get("/api/insights/geography", params={"profile_id": pid}).json()
        assert body["opponents"] == 1 and body["unresolved"] == 1 and "job" in body
        assert client.post("/api/insights/geography/start", params={"profile_id": pid}).json() == {"started": True}
        assert client.get("/api/insights/geography", params={"time_class": "nope"}).status_code == 422
        geography.job.update(running=False)  # the stubbed lookup never clears the flag it set

from datetime import datetime, timezone

from fastapi.testclient import TestClient

import practice
import server
from db import get_connection

client = TestClient(server.app)
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def _attempts(*stamps, correct=1):
    conn = get_connection()
    try:
        conn.execute("PRAGMA foreign_keys = OFF")  # attempts reference puzzles; these tests only care about the timestamps
        conn.execute("DELETE FROM puzzle_attempts")
        for stamp in stamps:
            conn.execute("INSERT INTO puzzle_attempts (puzzle_id, correct, session_type, attempted_at) VALUES (1, ?, 'practice', ?)", (correct, stamp))
        conn.commit()
    finally:
        conn.close()


class TestDailyProgress:
    def test_counts_today_and_the_streak(self):
        _attempts("2026-09-20 08:00:00", "2026-09-20 09:00:00", "2026-09-19 10:00:00", "2026-09-18 10:00:00", "2026-09-15 10:00:00")
        p = practice.daily_progress(0, 5, now=NOW)
        assert (p["today"], p["streak"], p["best_streak"]) == (2, 3, 3)
        assert [d["count"] for d in p["days"]][-3:] == [1, 1, 2] and len(p["days"]) == 7

    def test_a_streak_is_not_broken_before_you_have_practised_today(self):
        _attempts("2026-09-19 10:00:00", "2026-09-18 10:00:00")
        p = practice.daily_progress(0, 5, now=NOW)
        assert p["today"] == 0 and p["streak"] == 2

    def test_a_missed_day_resets_it(self):
        _attempts("2026-09-17 10:00:00", "2026-09-16 10:00:00", "2026-09-15 10:00:00")
        p = practice.daily_progress(0, 5, now=NOW)
        assert p["streak"] == 0 and p["best_streak"] == 3

    def test_days_are_the_viewers_local_days(self):
        _attempts("2026-09-20 03:00:00")  # 03:00 UTC is still Sept 19 evening at UTC-5 (getTimezoneOffset = +300)
        assert practice.daily_progress(300, 5, now=datetime(2026, 9, 20, 4, 0, tzinfo=timezone.utc))["today"] == 1
        assert practice.daily_progress(300, 5, now=datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc))["today"] == 0

    def test_no_attempts(self):
        _attempts()
        assert practice.daily_progress(0, 5, now=NOW) == {"today": 0, "goal": 5, "streak": 0, "best_streak": 0,
                                                            "days": [{"date": d, "count": 0, "solved": 0} for d in
                                                                     ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18", "2026-09-19", "2026-09-20"]]}

    def test_endpoint(self):
        _attempts()
        body = client.get("/api/practice", params={"goal": 3}).json()
        assert body["goal"] == 3 and "streak" in body
        assert client.get("/api/practice", params={"goal": 0}).status_code == 422

"""Regression test for insights.py's crash on a game with an unparseable
`date` — reproduced (per the audit that found it) by inserting a game with
a non-ISO date and confirming win_rate_by_day_of_week/win_rate_by_time_of_day
no longer raise TypeError.
"""

import itertools

from db import get_connection
from insights import win_rate_by_day_of_week, win_rate_by_time_of_day

_id_counter = itertools.count(1)


def _insert_game_with_date(date: str) -> int:
    conn = get_connection()
    try:
        game_id = conn.execute(
            "INSERT INTO games (source, source_game_id, date, result, color, analyzed) "
            "VALUES ('manual', ?, ?, 'win', 'white', 1)",
            (f"insights-test-{next(_id_counter)}", date),
        ).lastrowid
        conn.commit()
        return game_id
    finally:
        conn.close()


class TestUnparseableDateDoesNotCrash:
    def test_win_rate_by_day_of_week_skips_bad_date_instead_of_raising(self):
        # Dot-separated is what fetchers._combine_lichess_datetime used to
        # fall back to on a parse failure — SQLite's strftime() returns
        # NULL for it, which used to be indexed directly into _DAY_NAMES.
        _insert_game_with_date("2026.08.10")
        result = win_rate_by_day_of_week()
        assert isinstance(result, dict)

    def test_win_rate_by_time_of_day_skips_bad_date_instead_of_raising(self):
        _insert_game_with_date("2026.08.10")
        result = win_rate_by_time_of_day()
        assert isinstance(result, dict)

    def test_valid_iso_date_is_still_counted(self):
        game_id = _insert_game_with_date("2026-08-10T14:30:00+00:00")
        by_day = win_rate_by_day_of_week()
        total_games = sum(v["games"] for v in by_day.values())
        assert total_games >= 1

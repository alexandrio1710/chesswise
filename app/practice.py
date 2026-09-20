"""
Daily practice: how many puzzles you did today and how many days in a row you
practised — the small habit loop every improvement tool leans on, because
steady short sessions beat occasional long ones. Built from the puzzle attempt
log (every puzzle mode records into it), so nothing extra is tracked.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from db import get_connection


def _local_day(utc_text: str, tz_offset: int) -> str | None:
    """'2026-09-19 03:26:49' (UTC, as SQLite stores it) -> the viewer's local
    date. `tz_offset` is JavaScript's getTimezoneOffset(): minutes to add to
    local time to get UTC."""
    try:
        dt = datetime.strptime(utc_text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return (dt - timedelta(minutes=tz_offset)).date().isoformat()


def daily_progress(tz_offset: int = 0, goal: int = 5, days: int = 7, now: datetime | None = None) -> dict:
    """{today, goal, streak, best_streak, days: [{date, count, solved}]}.

    The streak counts consecutive local days with at least one attempt, ending
    today — or yesterday, so a streak isn't shown as broken before you've had
    the chance to practise today."""
    conn = get_connection()
    try:
        rows = conn.execute("SELECT attempted_at, correct FROM puzzle_attempts").fetchall()
    finally:
        conn.close()

    attempts: dict[str, int] = {}
    solved: dict[str, int] = {}
    for r in rows:
        day = _local_day(r["attempted_at"], tz_offset)
        if day:
            attempts[day] = attempts.get(day, 0) + 1
            solved[day] = solved.get(day, 0) + (1 if r["correct"] else 0)

    now = now or datetime.now(timezone.utc)
    today = (now - timedelta(minutes=tz_offset)).date()

    def streak_ending(day):
        n = 0
        while day.isoformat() in attempts:
            n += 1
            day -= timedelta(days=1)
        return n

    current = streak_ending(today) or streak_ending(today - timedelta(days=1))

    best = run = 0
    previous = None
    for day in sorted(datetime.strptime(d, "%Y-%m-%d").date() for d in attempts):
        run = run + 1 if previous is not None and (day - previous).days == 1 else 1
        best = max(best, run)
        previous = day

    return {
        "today": attempts.get(today.isoformat(), 0), "goal": goal, "streak": current, "best_streak": best,
        "days": [{"date": (today - timedelta(days=i)).isoformat(), "count": attempts.get((today - timedelta(days=i)).isoformat(), 0),
                  "solved": solved.get((today - timedelta(days=i)).isoformat(), 0)} for i in range(days - 1, -1, -1)],
    }

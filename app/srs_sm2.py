"""
Web platform, Section 2 — per-user SuperMemo-2 spaced repetition for puzzles.

Deliberately separate from the existing srs.py (Leitner-box system, keyed
by puzzle_id alone — see migrations.py's migration 12 docstring for why
that table can't be made multi-user-correct by just adding a user_id
column: the Leitner box for "this puzzle" would still be one shared value
across every user who's ever attempted it). This module is scoped to
(user_id, puzzle_id) throughout, backed by the `puzzle_progress` table.

SM-2 reference: https://en.wikipedia.org/wiki/SuperMemo#Description_of_SM-2_algorithm
`quality` is the standard 0-5 self-assessed recall grade:
    5 = perfect response
    4 = correct after hesitation
    3 = correct but with real difficulty
    2 = incorrect, but the right answer felt familiar
    1 = incorrect, remembered on seeing the answer
    0 = complete blackout
"""

from datetime import datetime, timedelta, timezone

from db import get_connection

MIN_EASINESS_FACTOR = 1.3
DEFAULT_EASINESS_FACTOR = 2.5


def _next_state(repetition_count: int, easiness_factor: float, interval_days: int, quality: int) -> tuple[int, float, int]:
    """Pure SM-2 transition — split out from record_puzzle_attempt so the
    scheduling math can be unit-tested without a database.
    """
    if quality < 3:
        # Any failed recall restarts the repetition streak and schedules
        # the puzzle back in tomorrow, but does NOT reset easiness_factor —
        # SM-2 treats "how hard this item is in general" and "did I get it
        # this time" as separate signals.
        return 0, easiness_factor, 1

    if repetition_count == 0:
        new_interval = 1
    elif repetition_count == 1:
        new_interval = 6
    else:
        new_interval = round(interval_days * easiness_factor)

    new_ef = easiness_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    new_ef = max(MIN_EASINESS_FACTOR, new_ef)

    return repetition_count + 1, new_ef, new_interval


def record_puzzle_attempt(user_id: int, puzzle_id: int, quality: int) -> dict:
    """Update (or create) this user's SM-2 state for one puzzle after an
    attempt. `quality` must be 0-5 (see module docstring); callers translating
    a simple correct/incorrect puzzle result into a quality grade typically
    map correct -> 4 or 5 (optionally 5 vs 4 based on speed/hint usage) and
    incorrect -> 1 or 2, reserving 0/3 for cases with an explicit signal
    SM-2 can't infer from a boolean (e.g. 0 for "gave up without trying").
    """
    if not 0 <= quality <= 5:
        raise ValueError(f"quality must be 0-5, got {quality}")

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT repetition_count, easiness_factor, interval_days FROM puzzle_progress "
            "WHERE user_id = ? AND puzzle_id = ?",
            (user_id, puzzle_id),
        ).fetchone()

        repetition_count = row["repetition_count"] if row else 0
        easiness_factor = row["easiness_factor"] if row else DEFAULT_EASINESS_FACTOR
        interval_days = row["interval_days"] if row else 0

        new_repetition_count, new_ef, new_interval = _next_state(
            repetition_count, easiness_factor, interval_days, quality
        )
        # UTC, not local time — get_due_puzzles/get_progress_summary compare
        # this against SQLite's date('now'), which SQLite documents as UTC.
        # date.today() (local) could shift next_review_date a day off from
        # what "now" resolves to server-side, depending on timezone and
        # time of day.
        next_review_date = (datetime.now(timezone.utc).date() + timedelta(days=new_interval)).isoformat()

        conn.execute(
            """
            INSERT INTO puzzle_progress
                (user_id, puzzle_id, repetition_count, easiness_factor, interval_days,
                 next_review_date, last_reviewed_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(user_id, puzzle_id) DO UPDATE SET
                repetition_count = excluded.repetition_count,
                easiness_factor = excluded.easiness_factor,
                interval_days = excluded.interval_days,
                next_review_date = excluded.next_review_date,
                last_reviewed_at = excluded.last_reviewed_at
            """,
            (user_id, puzzle_id, new_repetition_count, new_ef, new_interval, next_review_date),
        )
        conn.commit()
        return {
            "puzzle_id": puzzle_id,
            "repetition_count": new_repetition_count,
            "easiness_factor": round(new_ef, 3),
            "interval_days": new_interval,
            "next_review_date": next_review_date,
        }
    finally:
        conn.close()


def get_due_puzzles(user_id: int, limit: int = 10) -> list[dict]:
    """Puzzles due for this user today, most-overdue first; if fewer than
    `limit` are due, fills the rest with puzzles this user owns but has
    never attempted (brand-new puzzles are always eligible — otherwise a
    user with no review history yet would see an empty queue forever).
    Never returns another user's puzzles: `puzzles.user_id` is the
    ownership boundary (see migrations.py migration 11).
    """
    conn = get_connection()
    try:
        due_rows = conn.execute(
            """
            SELECT p.id, pr.next_review_date, pr.interval_days
            FROM puzzle_progress pr
            JOIN puzzles p ON p.id = pr.puzzle_id
            WHERE pr.user_id = ? AND p.user_id = ? AND pr.next_review_date <= date('now')
            ORDER BY pr.next_review_date ASC
            LIMIT ?
            """,
            (user_id, user_id, limit),
        ).fetchall()
        due = [dict(r) for r in due_rows]

        remaining = limit - len(due)
        if remaining > 0:
            new_rows = conn.execute(
                """
                SELECT p.id
                FROM puzzles p
                LEFT JOIN puzzle_progress pr ON pr.puzzle_id = p.id AND pr.user_id = ?
                LEFT JOIN mistakes m ON m.id = p.mistake_id
                WHERE p.user_id = ? AND pr.puzzle_id IS NULL
                ORDER BY m.eval_drop DESC
                LIMIT ?
                """,
                (user_id, user_id, remaining),
            ).fetchall()
            due.extend({"id": r["id"], "next_review_date": None, "interval_days": None} for r in new_rows)

        return due
    finally:
        conn.close()


def get_progress_summary(user_id: int) -> dict:
    """Small health-check summary, analogous to srs.get_review_stats() but
    scoped to one user and SM-2's own vocabulary (easiness/interval rather
    than Leitner boxes).
    """
    conn = get_connection()
    try:
        total_puzzles = conn.execute(
            "SELECT COUNT(*) as n FROM puzzles WHERE user_id = ?", (user_id,)
        ).fetchone()["n"]
        reviewed = conn.execute(
            "SELECT COUNT(*) as n, AVG(easiness_factor) as avg_ef FROM puzzle_progress WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        due_count = conn.execute(
            """
            SELECT COUNT(*) as n FROM puzzle_progress
            WHERE user_id = ? AND next_review_date <= date('now')
            """,
            (user_id,),
        ).fetchone()["n"]

        return {
            "total_puzzles": total_puzzles,
            "reviewed_count": reviewed["n"] or 0,
            "never_reviewed": total_puzzles - (reviewed["n"] or 0),
            "avg_easiness_factor": round(reviewed["avg_ef"], 2) if reviewed["avg_ef"] else None,
            "due_count": due_count,
        }
    finally:
        conn.close()

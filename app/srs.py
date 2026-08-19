"""
Advanced features, Section 4 — Spaced-repetition scheduling for puzzle
review, and attempt history for both Puzzle Rush sessions and ordinary
practice.

Uses a plain Leitner-system approach (5 boxes, correct answers move up a
box and get scheduled further out; any wrong answer drops straight back
to box 1 and comes back tomorrow) rather than a more elaborate algorithm
(SM-2 and its descendants) — simple, well-understood, and easy to tune by
eye, which matters more here than optimizing review scheduling to the
last percent for a personal tool with a few hundred puzzles.
"""

from datetime import datetime, timedelta, timezone

from db import get_connection

MIN_BOX = 1
MAX_BOX = 5

# Days until next review, by box. Tune these if reviews feel too frequent
# or too sparse once you've used it for a while.
LEITNER_INTERVALS_DAYS = {1: 1, 2: 3, 3: 7, 4: 14, 5: 30}


def record_attempt(puzzle_id: int, correct: bool, time_taken_ms: int | None = None, session_type: str = "practice") -> dict:
    """Log one puzzle attempt and update its spaced-repetition state:
    correct moves it up a box (reviewed further out); incorrect resets it
    to box 1 (reviewed again tomorrow) — the standard Leitner rule. Called
    from the puzzle attempt endpoint itself so every mode (browsing,
    practice, rush, review) feeds the same history without relying on the
    frontend to remember to record it separately.
    """
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO puzzle_attempts (puzzle_id, correct, time_taken_ms, session_type, attempted_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (puzzle_id, int(correct), time_taken_ms, session_type),
        )

        row = conn.execute(
            "SELECT leitner_box, total_attempts, total_correct FROM puzzle_review_state WHERE puzzle_id = ?",
            (puzzle_id,),
        ).fetchone()
        box = row["leitner_box"] if row else MIN_BOX
        total_attempts = (row["total_attempts"] if row else 0) + 1
        total_correct = (row["total_correct"] if row else 0) + int(correct)

        box = min(MAX_BOX, box + 1) if correct else MIN_BOX
        # SQLite's own datetime() text format ("YYYY-MM-DD HH:MM:SS", UTC),
        # NOT datetime.isoformat() ("...T...", local time) — get_due_puzzle_ids
        # compares this column against datetime('now') as plain strings, and
        # isoformat()'s "T" sorts after datetime('now')'s " " (0x54 > 0x20),
        # which made every scheduled review compare as not-yet-due regardless
        # of the actual date/time (same bug/fix as auth.py's create_session).
        next_review_at = (
            datetime.now(timezone.utc) + timedelta(days=LEITNER_INTERVALS_DAYS[box])
        ).strftime("%Y-%m-%d %H:%M:%S")

        conn.execute(
            """
            INSERT INTO puzzle_review_state (puzzle_id, leitner_box, next_review_at, total_attempts, total_correct, last_reviewed_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(puzzle_id) DO UPDATE SET
                leitner_box = excluded.leitner_box,
                next_review_at = excluded.next_review_at,
                total_attempts = excluded.total_attempts,
                total_correct = excluded.total_correct,
                last_reviewed_at = excluded.last_reviewed_at
            """,
            (puzzle_id, box, next_review_at, total_attempts, total_correct),
        )
        conn.commit()
        return {
            "leitner_box": box, "next_review_at": next_review_at,
            "total_attempts": total_attempts, "total_correct": total_correct,
        }
    finally:
        conn.close()


def get_due_puzzle_ids(source: str | None = None, phase: str | None = None,
                        severity: str | None = None, limit: int = 30) -> list[int]:
    """Puzzle ids due for review right now: never attempted (new puzzles
    are always due — otherwise a fresh install's review queue would start
    empty), or past their scheduled next_review_at. Ordered so the most
    overdue (or, for brand-new puzzles, the worst mistakes) come first.
    """
    where = []
    params: list = []
    if source:
        where.append("g.source = ?")
        params.append(source)
    if phase:
        where.append("p.phase = ?")
        params.append(phase)
    if severity:
        where.append("p.severity = ?")
        params.append(severity)
    where_clause = (" AND " + " AND ".join(where)) if where else ""

    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT p.id, p.severity, m.eval_drop,
                   rs.next_review_at, rs.leitner_box
            FROM puzzles p
            JOIN games g ON p.game_id = g.id
            JOIN mistakes m ON p.mistake_id = m.id
            LEFT JOIN puzzle_review_state rs ON rs.puzzle_id = p.id
            WHERE (rs.puzzle_id IS NULL OR rs.next_review_at <= datetime('now')) {where_clause}
            ORDER BY
                CASE WHEN rs.puzzle_id IS NULL THEN 0 ELSE 1 END,
                rs.next_review_at ASC,
                m.eval_drop DESC
            LIMIT ?
            """,
            params + [limit],
        ).fetchall()
        return [row["id"] for row in rows]
    finally:
        conn.close()


def get_review_stats(source: str | None = None) -> dict:
    """Box distribution and due count — a small SRS-health summary."""
    where = " AND g.source = ?" if source else ""
    params = (source,) if source else ()

    conn = get_connection()
    try:
        box_rows = conn.execute(
            f"""
            SELECT rs.leitner_box, COUNT(*) as n
            FROM puzzle_review_state rs
            JOIN puzzles p ON p.id = rs.puzzle_id
            JOIN games g ON p.game_id = g.id
            WHERE 1=1 {where}
            GROUP BY rs.leitner_box
            """,
            params,
        ).fetchall()
        box_counts = {row["leitner_box"]: row["n"] for row in box_rows}

        total_puzzles = conn.execute(
            f"""
            SELECT COUNT(*) as n FROM puzzles p JOIN games g ON p.game_id = g.id WHERE 1=1 {where}
            """,
            params,
        ).fetchone()["n"]

        due_count = len(get_due_puzzle_ids(source=source, limit=10_000))

        return {
            "total_puzzles": total_puzzles,
            "due_count": due_count,
            "box_counts": {str(b): box_counts.get(b, 0) for b in range(MIN_BOX, MAX_BOX + 1)},
            "never_reviewed": total_puzzles - sum(box_counts.values()),
        }
    finally:
        conn.close()

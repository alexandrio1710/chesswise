"""
Web platform, Section 4 — the Celery task that actually runs analysis.

Wraps mistakes.analyze_and_store_game() (the same function batch_analyze.py
calls synchronously across a ProcessPoolExecutor) with lifecycle bookkeeping
on games.analysis_status. A Celery worker has no HTTP request to report
progress back through, so status lives entirely in the database —
/api/analyze/status reads it from there, same as any other client would.
"""

import logging

from celery_app import celery_app
from config import STOCKFISH_DEPTH
from db import get_connection, get_pgn

logger = logging.getLogger(__name__)


def _set_status(game_id: int, status: str, error: str | None = None) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE games SET analysis_status = ?, analysis_error = ? WHERE id = ?",
            (status, error, game_id),
        )
        conn.commit()
    finally:
        conn.close()


@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def analyze_game_task(self, game_id: int, user_id: int, depth: int = STOCKFISH_DEPTH) -> dict:
    """Analyze one game with Stockfish and persist its flagged mistakes and
    puzzles. `user_id` doesn't affect the analysis itself (Stockfish
    doesn't care who owns the game) — it's threaded through so a failure
    can be attributed/audited, and so callers building a per-user status
    view don't need a second lookup to connect a task back to its owner.

    On failure, retries up to `max_retries` times (transient issues — e.g.
    the engine subprocess failing to start under load) before giving up and
    recording analysis_error for the UI to surface. A permanently
    unsupported game (a non-standard variant) is NOT a failure here —
    analyze_and_store_game() already marks those `analyzed = 1` with a
    skip_reason internally and returns None, which this task treats as a
    normal completion, not an error to retry.
    """
    from mistakes import analyze_and_store_game
    from puzzles import generate_all_puzzles

    _set_status(game_id, "processing")
    try:
        pgn_text = get_pgn(game_id)
        flagged = analyze_and_store_game(game_id, pgn_text, depth=depth)
        if flagged:
            generate_all_puzzles()
        _set_status(game_id, "completed")
        return {"game_id": game_id, "mistakes_flagged": len(flagged) if flagged else 0}
    except Exception as e:
        logger.exception(f"Analysis failed for game {game_id} (user {user_id})")
        if self.request.retries < self.max_retries:
            _set_status(game_id, "pending")  # picked up again by the retry
            raise self.retry(exc=e)
        _set_status(game_id, "failed", error=str(e))
        raise

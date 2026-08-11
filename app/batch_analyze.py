"""
Stage 5 — Batch process every unanalyzed game in the DB.

Resumable by construction: analyze_and_store_game() (Stage 4) commits the
`analyzed = 1` flag per game as soon as that game's mistakes are stored, so
if this script crashes or is interrupted partway through, re-running it
just picks up wherever it left off instead of re-analyzing finished games.

The "already analyzed" flag doubles as the cache Final Pass 5 asked for:
re-running this script never repeats Stockfish work for a game that's
already been through it — get_unanalyzed_games() only selects rows where
analyzed = 0. (analyze_and_store_game() itself will always redo one game
if called directly with a specific game_id — that's the intentional
"force re-analyze a single game" path, not the cache.)

Games are analyzed in parallel across worker processes (Final Pass 5,
config.ANALYSIS_WORKERS) since each game's analysis is CPU-bound and
independent of every other game's.
"""

import logging
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

from config import ANALYSIS_WORKERS, STOCKFISH_DEPTH
from db import get_connection, get_pgn
from mistakes import analyze_and_store_game

logger = logging.getLogger(__name__)


def get_unanalyzed_games() -> list:
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT id, source, date, color, opponent FROM games "
            "WHERE analyzed = 0 ORDER BY date ASC"
        ).fetchall()
    finally:
        conn.close()


def _analyze_one(game_id: int, depth: int) -> tuple[int, str, object]:
    """Worker-process entry point. Must be a plain top-level function (not
    a closure/method) so it can be pickled and sent to a worker process on
    every platform, including Windows, which spawns fresh interpreters
    rather than forking.

    Fetches its own PGN and opens its own DB connection rather than
    receiving them from the parent — a sqlite3.Connection can't cross a
    process boundary, and passing the PGN string as an argument in the
    "since = 0" case works fine but re-fetching by id keeps this function's
    signature simple to call the same way from sequential and parallel paths.

    Returns (game_id, status, payload) where status is one of
    "ok" / "skipped" (unsupported variant) / "failed" (payload is the
    error message).
    """
    try:
        flagged = analyze_and_store_game(game_id, get_pgn(game_id), depth=depth)
    except Exception as e:
        return (game_id, "failed", str(e))
    if flagged is None:
        return (game_id, "skipped", None)
    return (game_id, "ok", len(flagged))


def run_batch_analysis(depth: int = STOCKFISH_DEPTH, workers: int = ANALYSIS_WORKERS) -> None:
    games = get_unanalyzed_games()
    total = len(games)

    if total == 0:
        logger.info("All games already analyzed. Nothing to do.")
        return

    # Parallelism only pays for itself once there's more than a couple of
    # games — process startup overhead would dominate a batch of 1-2.
    workers = max(1, workers)
    if total <= 1:
        workers = 1

    labels = {g["id"]: f"{g['source']} | {g['date']} | {g['color']} vs {g['opponent']}" for g in games}
    game_ids = list(labels.keys())

    logger.info(f"Analyzing {total} unanalyzed game(s) at depth {depth} with {workers} worker(s)...")
    start = time.time()

    total_mistakes = 0
    skipped = 0
    completed = 0

    if workers == 1:
        results = (_analyze_one(gid, depth) for gid in game_ids)
        for game_id, status, payload in results:
            completed += 1
            total_mistakes, skipped = _handle_result(
                game_id, status, payload, labels, completed, total, total_mistakes, skipped
            )
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_analyze_one, gid, depth): gid for gid in game_ids}
            for future in as_completed(futures):
                game_id, status, payload = future.result()
                completed += 1
                total_mistakes, skipped = _handle_result(
                    game_id, status, payload, labels, completed, total, total_mistakes, skipped
                )

    elapsed = time.time() - start
    remaining = len(get_unanalyzed_games())
    summary = f"Done in {elapsed:.1f}s. {total_mistakes} total mistakes flagged across this run"
    if skipped:
        summary += f", {skipped} game(s) skipped (unsupported variant)"
    logger.info(summary + ".")
    if remaining:
        logger.warning(f"{remaining} game(s) still unanalyzed (failed runs) — re-run this script to retry.")


def _handle_result(game_id, status, payload, labels, completed, total, total_mistakes, skipped) -> tuple[int, int]:
    label = labels[game_id]
    if status == "failed":
        logger.warning(
            f"Analysis failed for game {completed}/{total} (game_id={game_id}, {label}): {payload}. "
            "Skipping — it stays unanalyzed and will be retried next run."
        )
    elif status == "skipped":
        skipped += 1
        logger.info(f"Game {completed}/{total} ({label}): skipped, unsupported variant")
    else:
        total_mistakes += payload
        logger.info(f"Game {completed}/{total} ({label}): {payload} mistakes flagged")
    return total_mistakes, skipped


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    depth = int(sys.argv[1]) if len(sys.argv) > 1 else STOCKFISH_DEPTH
    run_batch_analysis(depth=depth)

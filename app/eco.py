"""
Web platform, Section 3 — ECO (Encyclopedia of Chess Openings) classification.

Matches a game's move sequence against the local `eco_codes` table (see
eco_import.py for how that table gets populated) using longest-prefix
matching: a game continuing 1.e4 c5 2.Nf3 d6 3.d4 should classify as
whatever ECO entry is the LONGEST recognized prefix of its actual moves
(the Najdorf/Sicilian line it's heading into), not just "B20 Sicilian
Defence" (the first-move entry every Sicilian game also matches). The
in-memory index this relies on is small (a few thousand rows) and read far
more often than it changes, so it's loaded once and cached rather than
querying eco_codes on every game.
"""

import io
import logging

import chess.pgn

from db import get_connection

logger = logging.getLogger(__name__)

# ECO-table openings rarely run past ~20 full moves; capping the search
# bounds the worst case (a very long, quiet game) to a fixed amount of work
# rather than scanning its entire move list.
MAX_ECO_PLY = 40

_index_cache: dict[str, tuple[str, str]] | None = None


def _load_index() -> dict[str, tuple[str, str]]:
    """{"e4 c5 Nf3 d6": ("B50", "Sicilian Defense"), ...}, loaded once per
    process and cached — reload_eco_index() forces a refresh (e.g. after
    re-running eco_import.py against a long-running server).
    """
    global _index_cache
    if _index_cache is None:
        conn = get_connection()
        try:
            rows = conn.execute("SELECT pgn, eco, name FROM eco_codes").fetchall()
        finally:
            conn.close()
        _index_cache = {row["pgn"]: (row["eco"], row["name"]) for row in rows}
        logger.info(f"Loaded {len(_index_cache)} ECO openings into memory")
    return _index_cache


def reload_eco_index() -> None:
    global _index_cache
    _index_cache = None
    _load_index()


def classify_game_opening(game_moves: list[str]) -> dict | None:
    """`game_moves` is a plain SAN move list in play order (e.g.
    ["e4", "c5", "Nf3", "d6", ...] — no move numbers, no side-to-move
    markers; see moves_from_pgn()). Returns the deepest ECO entry whose
    move sequence is an exact prefix of `game_moves`, as
    {"eco", "name", "ply_count"}, or None if not even the first move
    matches a known opening.
    """
    if not game_moves:
        return None

    index = _load_index()
    upper_bound = min(len(game_moves), MAX_ECO_PLY)
    for ply in range(upper_bound, 0, -1):
        match = index.get(" ".join(game_moves[:ply]))
        if match:
            eco, name = match
            return {"eco": eco, "name": name, "ply_count": ply}
    return None


def moves_from_pgn(pgn_text: str) -> list[str]:
    """Plain SAN move list for a stored game's mainline, in the shape
    classify_game_opening() expects. Returns [] for unparseable PGN rather
    than raising — an opening classification failing shouldn't block the
    game it belongs to from being stored/analyzed.
    """
    try:
        game = chess.pgn.read_game(io.StringIO(pgn_text))
    except Exception:
        return []
    if game is None:
        return []

    board = game.board()
    moves = []
    for move in game.mainline_moves():
        moves.append(board.san(move))
        board.push(move)
    return moves


def classify_and_store_game_opening(game_id: int, pgn_text: str) -> dict | None:
    """Standalone convenience: classify one game and write eco/opening_name
    onto its row, opening its own connection. Used by backfill_missing_eco()
    and any other single-game caller — NOT used by db.save_games()'s
    ingestion loop, which is mid its own open write transaction when a new
    game is inserted and instead calls classify_game_opening() directly and
    writes through its own connection, to avoid a second writer connection
    colliding with the first's uncommitted transaction.
    """
    result = classify_game_opening(moves_from_pgn(pgn_text))
    if result is None:
        return None

    conn = get_connection()
    try:
        conn.execute(
            "UPDATE games SET eco = ?, opening_name = ? WHERE id = ?",
            (result["eco"], result["name"], game_id),
        )
        conn.commit()
    finally:
        conn.close()
    return result


def backfill_missing_eco(batch_size: int = 500) -> int:
    """Classifies every stored game without an eco code yet — games
    imported before this feature existed, or ones whose classification
    failed the first time (e.g. eco_codes was empty because eco_import.py
    hadn't been run yet). Safe to re-run: only touches `eco IS NULL` rows,
    and a game whose moves still don't match anything is left NULL again
    rather than looping forever. Returns the number of games updated.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, pgn FROM games WHERE eco IS NULL AND pgn IS NOT NULL LIMIT ?",
            (batch_size,),
        ).fetchall()
    finally:
        conn.close()

    updated = 0
    for row in rows:
        if classify_and_store_game_opening(row["id"], row["pgn"]) is not None:
            updated += 1
    return updated


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    n = backfill_missing_eco(batch_size=100_000)
    logger.info(f"Classified {n} game(s).")

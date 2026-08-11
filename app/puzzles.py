"""
Stage B — Personalized puzzle generator.

For every move flagged as a 'mistake' or 'blunder' (not 'inaccuracy' — those
are too minor to make good puzzles), generates a tactics puzzle: the board
position right before the mistake, Stockfish's top engine lines there, and
the move the player actually played. Resumable and idempotent like Stage
5's batch analysis — re-running only generates puzzles for mistakes that
don't have one yet.
"""

import io
import json
import logging

import chess
import chess.pgn

from analysis import get_engine
from config import PUZZLE_DEPTH, PUZZLE_TOP_LINES
from db import get_connection, get_pgn

logger = logging.getLogger(__name__)

# Puzzles are only worth generating for moves substantial enough to be
# instructive. Inaccuracies (100-199cp) are usually too subtle/ambiguous
# for a clean "find the best move" puzzle.
PUZZLE_SEVERITIES = ("mistake", "blunder")


def board_before_ply(pgn_text: str, target_ply: int) -> chess.Board:
    """Replay a game and return the board position right before the move
    at `target_ply` (1-indexed half-move, matching analysis.py's `ply`) is
    played.
    """
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    board = game.board()
    node = game
    ply = 0
    while node.variations:
        if ply == target_ply - 1:
            return board
        next_node = node.variations[0]
        board.push(next_node.move)
        ply += 1
        node = next_node
    raise ValueError(f"ply {target_ply} not found in game (game has {ply} plies)")


def get_top_lines(fen: str, depth: int = PUZZLE_DEPTH, num_lines: int = PUZZLE_TOP_LINES) -> list[dict]:
    """Top engine lines at a position, evaluation from the perspective of
    whoever is about to move there (positive = good for them) — the
    `stockfish` package's default turn-relative convention.
    """
    engine = get_engine(depth)
    engine.set_fen_position(fen)
    top_moves = engine.get_top_moves(num_lines)

    board = chess.Board(fen)
    lines = []
    for m in top_moves:
        move = chess.Move.from_uci(m["Move"])
        lines.append({
            "move_uci": m["Move"],
            "move_san": board.san(move),
            "eval_cp": m["Centipawn"],
            "mate_in": m["Mate"],
        })
    return lines


def generate_puzzle_for_mistake(mistake_row, pgn_text: str) -> dict:
    board = board_before_ply(pgn_text, mistake_row["ply"])
    fen_before = board.fen()
    side_to_move = "white" if board.turn == chess.WHITE else "black"

    top_lines = get_top_lines(fen_before)
    if not top_lines:
        raise ValueError(f"no legal moves found at ply {mistake_row['ply']} (mistake_id={mistake_row['id']})")
    best = top_lines[0]

    return {
        "mistake_id": mistake_row["id"],
        "game_id": mistake_row["game_id"],
        "fen_before": fen_before,
        "side_to_move": side_to_move,
        "played_move_san": mistake_row["move_san"],
        "best_move_uci": best["move_uci"],
        "best_move_san": best["move_san"],
        "top_lines": top_lines,
        "phase": mistake_row["phase"],
        "severity": mistake_row["severity"],
    }


def store_puzzle(p: dict) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO puzzles
                (mistake_id, game_id, fen_before, side_to_move, played_move_san,
                 best_move_uci, best_move_san, top_lines, phase, severity, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                p["mistake_id"], p["game_id"], p["fen_before"], p["side_to_move"],
                p["played_move_san"], p["best_move_uci"], p["best_move_san"],
                json.dumps(p["top_lines"]), p["phase"], p["severity"],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_mistakes_without_puzzles() -> list:
    placeholders = ",".join("?" * len(PUZZLE_SEVERITIES))
    conn = get_connection()
    try:
        return conn.execute(
            f"""
            SELECT m.* FROM mistakes m
            LEFT JOIN puzzles p ON p.mistake_id = m.id
            WHERE m.severity IN ({placeholders}) AND p.id IS NULL AND m.ply IS NOT NULL
            ORDER BY m.game_id, m.ply
            """,
            PUZZLE_SEVERITIES,
        ).fetchall()
    finally:
        conn.close()


def generate_all_puzzles() -> None:
    todo = get_mistakes_without_puzzles()
    total = len(todo)

    if total == 0:
        logger.info("No new puzzles to generate (every mistake/blunder already has one, "
                     "or run batch_analyze.py first if mistakes are missing `ply` data).")
        return

    logger.info(f"Generating {total} puzzle(s) at depth {PUZZLE_DEPTH}...")

    pgn_cache: dict[int, str] = {}
    generated = 0
    failed = 0

    for i, row in enumerate(todo, start=1):
        game_id = row["game_id"]
        if game_id not in pgn_cache:
            pgn_cache[game_id] = get_pgn(game_id)

        try:
            puzzle = generate_puzzle_for_mistake(row, pgn_cache[game_id])
            store_puzzle(puzzle)
            generated += 1
        except Exception as e:
            failed += 1
            logger.warning(f"Puzzle generation failed for mistake_id={row['id']} (game_id={game_id}): {e}")
            continue

        if i % 10 == 0 or i == total:
            logger.info(f"{i}/{total} processed ({generated} generated, {failed} failed)")

    logger.info(f"Done. {generated} puzzles generated, {failed} failed.")


# ---------------------------------------------------------------------------
# Read-side queries for the puzzle-review page (server.py)
# ---------------------------------------------------------------------------

def _source_clause(source: str | None) -> tuple[str, tuple]:
    if source:
        return " AND g.source = ?", (source,)
    return "", ()


def get_puzzle_queue(source: str | None = None, phase: str | None = None, limit: int = 20) -> list[dict]:
    """Puzzle summaries only (no FEN/answer) for populating a queue list.
    Ordered worst-first (biggest eval drop) so the most instructive puzzles
    in the selected category surface first.
    """
    where, params = _source_clause(source)
    if phase:
        where += " AND p.phase = ?"
        params = params + (phase,)

    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT p.id, p.phase, p.severity, g.source, g.date, g.opponent,
                   m.eval_drop
            FROM puzzles p
            JOIN games g ON p.game_id = g.id
            JOIN mistakes m ON p.mistake_id = m.id
            WHERE 1=1 {where}
            ORDER BY m.eval_drop DESC
            LIMIT ?
            """,
            params + (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_puzzle(puzzle_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT p.*, g.source, g.date, g.opponent, g.color as game_color,
                   g.time_control, g.opening_name
            FROM puzzles p
            JOIN games g ON p.game_id = g.id
            WHERE p.id = ?
            """,
            (puzzle_id,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["top_lines"] = json.loads(d["top_lines"])
        return d
    finally:
        conn.close()


def legal_moves_for_fen(fen: str) -> list[dict]:
    board = chess.Board(fen)
    seen = set()
    moves = []
    for move in board.legal_moves:
        key = (move.from_square, move.to_square)
        if key in seen:
            continue  # collapse promotion variants (assume queen in the UI)
        seen.add(key)
        moves.append({
            "from": chess.square_name(move.from_square),
            "to": chess.square_name(move.to_square),
        })
    return moves


class IllegalMoveError(Exception):
    pass


def check_attempt(puzzle: dict, from_square: str, to_square: str, promotion: str | None = None) -> dict:
    """Validate a puzzle attempt against the actual rules of chess (not just
    string-matching the target square), then grade it.

    "Correct" means either the engine's single best move, or one of the
    other pre-fetched top lines within a small eval margin of the best one
    (an equally good alternative) — graded from data already fetched at
    generation time, no extra engine call needed here.
    """
    board = chess.Board(puzzle["fen_before"])
    try:
        move = chess.Move.from_uci(f"{from_square}{to_square}")
    except ValueError:
        raise IllegalMoveError(f"'{from_square}{to_square}' isn't a valid pair of squares")

    if move not in board.legal_moves:
        # Auto-promote to queen if the raw move needs a promotion piece and
        # the UI didn't send one (kept out of the click-to-move interaction).
        queen_move = chess.Move(move.from_square, move.to_square, promotion=chess.QUEEN)
        if queen_move in board.legal_moves:
            move = queen_move
        else:
            raise IllegalMoveError(f"{from_square}-{to_square} is not a legal move here")

    played_san = board.san(move)
    played_uci = move.uci()

    top_lines = puzzle["top_lines"]
    best = top_lines[0]
    correct = played_uci == best["move_uci"]

    if not correct:
        for line in top_lines:
            if line["move_uci"] != played_uci:
                continue
            if best["mate_in"] is not None or line["mate_in"] is not None:
                correct = line["mate_in"] == best["mate_in"]
            else:
                correct = abs((line["eval_cp"] or 0) - (best["eval_cp"] or 0)) <= 20
            break

    return {
        "correct": correct,
        "played_san": played_san,
        "best_move_san": best["move_san"],
        "original_mistake_move_san": puzzle["played_move_san"],
        "top_lines": top_lines,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    generate_all_puzzles()

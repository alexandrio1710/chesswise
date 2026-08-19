"""
Stage 3 — Single-game Stockfish analysis.

Steps through a game's PGN move by move with python-chess and evaluates
each resulting position with Stockfish. Evaluations are normalized to
centipawns from White's perspective (positive = good for White), with
mate scores converted to a large centipawn value so downstream comparisons
(Stage 4) can use plain arithmetic.
"""

import io
import logging

import chess
import chess.pgn
from stockfish import Stockfish

from config import STOCKFISH_DEPTH, STOCKFISH_PATH

logger = logging.getLogger(__name__)

# Mate scores are converted to this many centipawns (plus/minus remaining
# mate distance) so eval comparisons don't need special-case mate handling.
MATE_SCORE_CP = 10000


def get_engine(depth: int = STOCKFISH_DEPTH) -> Stockfish:
    """Start a Stockfish engine subprocess. Wrapped so a broken install
    (wrong architecture, corrupted download, missing execute permission)
    fails with a message pointing at the actual binary path, rather than
    whatever raw OSError/subprocess exception the `stockfish` package
    happens to raise.
    """
    try:
        return Stockfish(path=STOCKFISH_PATH, depth=depth, parameters={"Threads": 1, "Hash": 128})
    except Exception as e:
        logger.error(f"Failed to start Stockfish at '{STOCKFISH_PATH}': {e}")
        raise RuntimeError(
            f"Could not start the Stockfish engine at '{STOCKFISH_PATH}'. "
            "It may be corrupted, the wrong architecture for this machine, or "
            "missing execute permissions. Try reinstalling it (see README), or "
            "set STOCKFISH_PATH to a different binary."
        ) from e


def evaluate_position_cp(engine: Stockfish, white_to_move: bool) -> float:
    """Return the current position's evaluation in centipawns, normalized
    to White's perspective (positive = good for White), with mate scores
    mapped to a large finite value.

    The `stockfish` package reports evaluations relative to the side to
    move (standard UCI convention), so we flip sign when it's Black's move.
    """
    ev = engine.get_evaluation()
    if ev["type"] == "cp":
        value = float(ev["value"])
    else:
        # ev["type"] == "mate"; mate-in-N relative to the side to move
        # (positive = side to move mates, negative = side to move gets mated).
        mate_in = ev["value"]
        if mate_in > 0:
            value = MATE_SCORE_CP - mate_in
        elif mate_in < 0:
            value = -MATE_SCORE_CP - mate_in
        else:
            value = 0.0

    return value if white_to_move else -value


def analyze_game_moves(pgn_text: str, depth: int = STOCKFISH_DEPTH) -> list[dict]:
    """Step through every move of a game and evaluate the position before
    and after each one. Returns a list of per-move dicts:

        {
            "move_number": int (full-move number, 1-based),
            "ply": int (half-move index, 1-based),
            "color_moved": "white" | "black",
            "move_san": str,
            "eval_before_cp": float (centipawns, White's perspective, pre-move),
            "eval_after_cp": float (centipawns, White's perspective, post-move),
            "eval_cp": float (alias for eval_after_cp, kept for convenience),
            "non_king_piece_count": int (pieces on board after the move, excl. kings),
            "clock_seconds_remaining": int | None,
        }
    """
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return []

    engine = get_engine(depth)
    try:
        board = game.board()
        results = []

        # Eval of the starting position, before any move has been made, so
        # move 1 has a real "before" value instead of an assumed 0.
        engine.set_fen_position(board.fen())
        prev_eval_cp = evaluate_position_cp(engine, white_to_move=board.turn == chess.WHITE)

        node = game
        ply = 0
        while node.variations:
            next_node = node.variations[0]
            move = next_node.move
            san = board.san(move)
            color_moved = "white" if board.turn == chess.WHITE else "black"

            board.push(move)
            ply += 1

            if board.is_checkmate():
                # Stockfish can't evaluate a position with no legal moves.
                # The side to move (board.turn) is the one who just got mated.
                eval_after_cp = -MATE_SCORE_CP if board.turn == chess.WHITE else MATE_SCORE_CP
            elif board.is_game_over():
                # Stalemate, insufficient material, repetition, 50-move rule, etc.
                eval_after_cp = 0.0
            else:
                engine.set_fen_position(board.fen())
                eval_after_cp = evaluate_position_cp(engine, white_to_move=board.turn == chess.WHITE)

            full_move_number = (ply + 1) // 2
            non_king_piece_count = len(board.piece_map()) - 2  # exclude both kings

            results.append({
                "move_number": full_move_number,
                "ply": ply,
                "color_moved": color_moved,
                "move_san": san,
                "eval_before_cp": prev_eval_cp,
                "eval_after_cp": eval_after_cp,
                "eval_cp": eval_after_cp,
                "non_king_piece_count": non_king_piece_count,
                "clock_seconds_remaining": _extract_clock_seconds(next_node),
            })

            prev_eval_cp = eval_after_cp
            node = next_node

        return results
    finally:
        # Stockfish.__del__ would eventually quit this subprocess via plain
        # refcounting, but an exception raised mid-loop keeps `engine`
        # alive for as long as its traceback is (e.g. a caller collecting
        # per-game errors across a batch — see batch_analyze.py) — explicit
        # cleanup here means a malformed game can't leak a running Stockfish
        # process for the lifetime of that error.
        engine.send_quit_command()


def _extract_clock_seconds(node: chess.pgn.GameNode) -> int | None:
    """Extract remaining clock time (in seconds) from a move's PGN comment,
    e.g. '[%clk 0:04:32]', which both Lichess and (post-normalization,
    see fetchers.py) Chess.com PGNs use.
    """
    comment = node.comment or ""
    if "%clk" not in comment:
        return None
    try:
        clk_part = comment.split("%clk", 1)[1].strip().split("]")[0].strip()
        h, m, s = clk_part.split(":")
        return int(h) * 3600 + int(m) * 60 + int(s)
    except (ValueError, IndexError):
        return None


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from db import get_all_games

    games = get_all_games()
    if not games:
        print("No games in DB yet. Run db.py first (Stage 2).")
        sys.exit(1)

    test_game = games[0]
    print(f"Analyzing test game: {test_game['source']} | {test_game['date']} | "
          f"{test_game['color']} vs {test_game['opponent']}\n")

    moves = analyze_game_moves(test_game["pgn"])
    for m in moves:
        clock = f"{m['clock_seconds_remaining']}s" if m['clock_seconds_remaining'] is not None else "?"
        print(f"  {m['move_number']:>3}. ({m['color_moved']:<5}) {m['move_san']:<8} "
              f"eval={m['eval_cp']:>7.0f}cp  clock={clock}")

    print(f"\nTotal moves analyzed: {len(moves)}")

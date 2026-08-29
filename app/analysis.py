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
import chess.engine
import chess.pgn

from config import STOCKFISH_DEPTH, STOCKFISH_PATH

logger = logging.getLogger(__name__)

# Mate scores are converted to this many centipawns (plus/minus remaining
# mate distance) so eval comparisons don't need special-case mate handling.
MATE_SCORE_CP = 10000


# --- Game phase (opening/middlegame/endgame): a direct port of Lichess's
# own Divider algorithm, replacing a move-number-only heuristic.
# https://github.com/lichess-org/scalachess/blob/master/core/src/main/scala/Divider.scala
#
# A fixed "move <= 10 is opening" cutoff can't tell a slow, closed opening
# (still opening-like play well past move 10) from a sharp, early-trading
# line (already middlegame-like material by move 6) — Lichess's own
# detector instead looks at the actual position: middlegame starts at the
# first ply where material has thinned to 10 or fewer majors/minors, back
# ranks have emptied out (pieces developed), or a "mixedness" score (how
# contested/interpenetrated the two sides' pieces are, scored region by
# region across the board) crosses a threshold; endgame starts at the
# first *later* ply with 6 or fewer majors/minors. Also fixes a real
# inaccuracy in this project's old heuristic: majors/minors deliberately
# excludes pawns (the standard chess-theory basis for "how much material
# is left"), whereas the old ENDGAME_PIECE_COUNT counted pawns too — a
# piece-down endgame with most pawns still on the board never crossed that
# threshold, so it was never tagged "endgame" at all.
MIDGAME_MAJORS_MINORS_CEILING = 10
ENDGAME_MAJORS_MINORS_CEILING = 6
MIXEDNESS_MIDGAME_THRESHOLD = 150

_MIXEDNESS_SMALL_SQUARE = 0x0303  # a 2x2 block of squares (2 files x 2 ranks)
# Every 2x2 sub-square of the board, scanned left-to-right then rank-by-rank
# (49 = 7x7 possible top-left corners) — same sliding-window setup as the
# Scala original, so index i's (file, rank) — and therefore its `y` below —
# lines up with it exactly.
_MIXEDNESS_REGIONS = [_MIXEDNESS_SMALL_SQUARE << (x + 8 * y) for y in range(7) for x in range(7)]


def _majors_and_minors(board: chess.Board) -> int:
    return bin(board.occupied & ~board.pawns & ~board.kings).count("1")


def _backrank_sparse(board: chess.Board) -> bool:
    """Fewer than 4 pieces remain on either side's own back rank — a sign
    pieces have developed off it, even before any have been traded off."""
    white_backrank = bin(board.occupied_co[chess.WHITE] & chess.BB_RANK_1).count("1")
    black_backrank = bin(board.occupied_co[chess.BLACK] & chess.BB_RANK_8).count("1")
    return white_backrank < 4 or black_backrank < 4


def _mixedness_region_score(y: int, white: int, black: int) -> int:
    """Score for one 2x2 region, `y` ranks up from the back rank (1-7) —
    a direct, literal transcription of Divider.scala's own `score` table;
    see that file for the reasoning behind the specific numbers."""
    if white == 0:
        if black == 1:
            return 1 + y
        if black == 2:
            return 2 + (6 - y) if y < 6 else 0
        if black in (3, 4):
            return 3 + (7 - y) if y < 7 else 0
        return 0
    if white == 1:
        if black == 0:
            return 1 + (8 - y)
        if black == 1:
            return 5 + abs(4 - y)
        if black == 2:
            return 4 + (7 - y)
        if black == 3:
            return 5 + (7 - y)
        return 0
    if white == 2:
        if black == 0:
            return 2 + (y - 2) if y > 2 else 0
        if black == 1:
            return 4 + (y - 1)
        if black == 2:
            return 7
        return 0
    if white == 3:
        if black == 0:
            return 3 + (y - 1) if y > 1 else 0
        if black == 1:
            return 5 + (y - 1)
        return 0
    if white == 4:
        if black == 0:
            return 3 + (y - 1) if y > 1 else 0
        return 0
    return 0


def _mixedness(board: chess.Board) -> int:
    """How contested/interpenetrated the two sides' pieces are, board-wide
    — the more the position has left a "lined up on your own side" opening
    setup, the higher this scores, regardless of whether material has
    actually been traded yet."""
    total = 0
    for i, region in enumerate(_MIXEDNESS_REGIONS):
        y = (i // 7) + 1
        white_count = bin(board.occupied_co[chess.WHITE] & region).count("1")
        black_count = bin(board.occupied_co[chess.BLACK] & region).count("1")
        total += _mixedness_region_score(y, white_count, black_count)
    return total


def _is_midgame_position(board: chess.Board) -> bool:
    return (
        _majors_and_minors(board) <= MIDGAME_MAJORS_MINORS_CEILING
        or _backrank_sparse(board)
        or _mixedness(board) > MIXEDNESS_MIDGAME_THRESHOLD
    )


def _is_endgame_position(board: chess.Board) -> bool:
    return _majors_and_minors(board) <= ENDGAME_MAJORS_MINORS_CEILING


def _compute_phase_boundaries(midgame_flags: list[bool], endgame_flags: list[bool]) -> tuple[int | None, int | None]:
    """1-based ply numbers where the middlegame/endgame begin (or None if
    that phase is never reached) — `midgame_flags[i]`/`endgame_flags[i]`
    is whether ply i+1's position (the position right after that ply was
    played) satisfies the midgame/endgame material condition.

    endgame_flags is scanned from the very start of the game regardless of
    where midgame_flags first triggers (matching Divider.scala exactly) —
    for a real game midgame always triggers first or not at all, but if an
    artificial "from position" game somehow starts within 6 majors/minors
    of its own, this discards the midgame boundary rather than reporting
    an endgame that supposedly started before the middlegame did.
    """
    middle_idx = next((i for i, f in enumerate(midgame_flags) if f), None)
    end_idx = next((i for i, f in enumerate(endgame_flags) if f), None) if middle_idx is not None else None
    if middle_idx is not None and end_idx is not None and middle_idx >= end_idx:
        middle_idx = None
    middle_ply = middle_idx + 1 if middle_idx is not None else None
    end_ply = end_idx + 1 if end_idx is not None else None
    return middle_ply, end_ply


def _phase_for_ply(ply: int, middle_ply: int | None, end_ply: int | None) -> str:
    if middle_ply is not None and ply < middle_ply:
        return "opening"
    if end_ply is not None and ply >= end_ply:
        return "endgame"
    if middle_ply is None:
        return "opening"
    return "middlegame"


def get_engine() -> chess.engine.SimpleEngine:
    """Start a Stockfish engine subprocess via python-chess's own UCI
    wrapper — the same one Lichess's own puzzle generator uses
    (https://github.com/ornicar/lichess-puzzler/blob/master/generator/generator.py),
    rather than the third-party `stockfish` PyPI package this project used
    to depend on separately from python-chess (already a dependency for
    board/PGN handling). Wrapped so a broken install (wrong architecture,
    corrupted download, missing execute permission) fails with a message
    pointing at the actual binary path, rather than a raw OSError.
    """
    try:
        engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)
        engine.configure({"Threads": 1, "Hash": 128})
        return engine
    except Exception as e:
        logger.error(f"Failed to start Stockfish at '{STOCKFISH_PATH}': {e}")
        raise RuntimeError(
            f"Could not start the Stockfish engine at '{STOCKFISH_PATH}'. "
            "It may be corrupted, the wrong architecture for this machine, or "
            "missing execute permissions. Try reinstalling it (see README), or "
            "set STOCKFISH_PATH to a different binary."
        ) from e


def evaluate_position_cp(engine: chess.engine.SimpleEngine, board: chess.Board, depth: int) -> float:
    """Return `board`'s evaluation in centipawns, normalized to White's
    perspective (positive = good for White), with mate scores mapped to a
    large finite value.

    `PovScore.white()` does the side-to-move-to-White conversion natively
    (no manual sign flip needed), and `.score(mate_score=...)` flattens a
    mate score to a signed finite value the same way the hand-rolled
    version here used to — the exact mate distance baked into that value
    doesn't matter downstream (every consumer clamps to +-1000 or just
    checks "is this near the ceiling", see stats.WIN_PERCENT_CP_CEILING
    and mistakes.MATE_SCORE_DETECTION_THRESHOLD_CP).
    """
    info = engine.analyse(board, chess.engine.Limit(depth=depth))
    return info["score"].white().score(mate_score=MATE_SCORE_CP)


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
            "phase": "opening" | "middlegame" | "endgame",
            "clock_seconds_remaining": int | None,
        }
    """
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return []

    engine = get_engine()
    try:
        board = game.board()
        results = []
        midgame_flags = []
        endgame_flags = []

        # Eval of the starting position, before any move has been made, so
        # move 1 has a real "before" value instead of an assumed 0.
        prev_eval_cp = evaluate_position_cp(engine, board, depth)

        node = game
        ply = 0
        while node.variations:
            next_node = node.variations[0]
            move = next_node.move
            san = board.san(move)
            color_moved = "white" if board.turn == chess.WHITE else "black"

            board.push(move)
            ply += 1
            midgame_flags.append(_is_midgame_position(board))
            endgame_flags.append(_is_endgame_position(board))

            if board.is_checkmate():
                # Stockfish can't evaluate a position with no legal moves.
                # The side to move (board.turn) is the one who just got mated.
                eval_after_cp = -MATE_SCORE_CP if board.turn == chess.WHITE else MATE_SCORE_CP
            elif board.is_game_over():
                # Stalemate, insufficient material, repetition, 50-move rule, etc.
                eval_after_cp = 0.0
            else:
                eval_after_cp = evaluate_position_cp(engine, board, depth)

            full_move_number = (ply + 1) // 2

            results.append({
                "move_number": full_move_number,
                "ply": ply,
                "color_moved": color_moved,
                "move_san": san,
                "eval_before_cp": prev_eval_cp,
                "eval_after_cp": eval_after_cp,
                "eval_cp": eval_after_cp,
                "clock_seconds_remaining": _extract_clock_seconds(next_node),
            })

            prev_eval_cp = eval_after_cp
            node = next_node

        middle_ply, end_ply = _compute_phase_boundaries(midgame_flags, endgame_flags)
        for m in results:
            m["phase"] = _phase_for_ply(m["ply"], middle_ply, end_ply)

        return results
    finally:
        # SimpleEngine's __del__ would eventually quit this subprocess via
        # plain refcounting, but an exception raised mid-loop keeps `engine`
        # alive for as long as its traceback is (e.g. a caller collecting
        # per-game errors across a batch — see batch_analyze.py) — explicit
        # cleanup here means a malformed game can't leak a running Stockfish
        # process for the lifetime of that error.
        engine.quit()


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

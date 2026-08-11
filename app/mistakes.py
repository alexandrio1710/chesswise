"""
Stage 4 — Mistake detection.

Takes the per-move Stockfish evals from analysis.py, converts them to the
mover's own perspective (a "good" move should never look bad to the person
who played it), classifies each move's eval drop by severity and the game
phase it happened in, and stores flagged moves in the `mistakes` table.

All thresholds below are intentionally named constants — tune them here if
the classification feels too strict or too lenient.
"""

import re

from analysis import analyze_game_moves
from config import STOCKFISH_DEPTH
from db import get_connection

# Lichess/Chess.com PGNs tag non-standard rule sets with a Variant header
# (e.g. "Three-check", "Horde", "Atomic", "Crazyhouse"). Those use board
# setups or win conditions Stockfish (a standard-chess engine) can't
# evaluate meaningfully — python-chess even produces non-standard FEN for
# some of them (e.g. Horde's pawn-flooded start, Three-check's embedded
# check counter), which Stockfish rejects outright. We skip these rather
# than erroring, since no amount of retrying will make them analyzable.
STANDARD_VARIANT_TAGS = {None, "Standard", "From Position"}


def get_pgn_variant(pgn_text: str) -> str | None:
    match = re.search(r'\[Variant\s+"([^"]*)"\]', pgn_text)
    return match.group(1) if match else None

# --- Severity thresholds --------------------------------------------------
# Eval drop is centipawns lost by the mover on a single move, measured from
# the mover's own perspective (positive = the position got worse for them).
INACCURACY_THRESHOLD_CP = 100   # 100-199 => inaccuracy
MISTAKE_THRESHOLD_CP = 200      # 200-399 => mistake
BLUNDER_THRESHOLD_CP = 400      # 400+    => blunder

# --- Game phase thresholds -------------------------------------------------
OPENING_MOVE_CUTOFF = 10     # moves 1-10 => opening
ENDGAME_MOVE_CUTOFF = 30     # move 30+ => endgame, regardless of material
ENDGAME_PIECE_COUNT = 7      # fewer than 7 non-king pieces on board => endgame


def classify_severity(eval_drop_cp: float) -> str | None:
    """Grade a single move by how much the position's evaluation dropped
    for the player who made it (already converted to their own
    perspective by the caller, so a positive number always means "this
    move made things worse for them" regardless of color).

    The thresholds (100/200/400cp) are a judgment call, not a rule from
    chess theory: they roughly follow the bands common chess sites use for
    "inaccuracy" vs "mistake" vs "blunder", picked so a single pawn's
    worth of inaccuracy (~100cp) is the noise floor — engines wobble by
    that much between very similar quiet moves — while a full piece or a
    missed tactic (~400cp+) reliably lands as a blunder. Tune the *_THRESHOLD_CP
    constants above if this feels too strict or too lenient for your games.

    Returns None if the drop didn't clear even the inaccuracy bar — most
    moves in most games, since only real errors get flagged at all.
    """
    if eval_drop_cp >= BLUNDER_THRESHOLD_CP:
        return "blunder"
    if eval_drop_cp >= MISTAKE_THRESHOLD_CP:
        return "mistake"
    if eval_drop_cp >= INACCURACY_THRESHOLD_CP:
        return "inaccuracy"
    return None


def classify_phase(move_number: int, non_king_piece_count: int) -> str:
    """Bucket a move into opening/middlegame/endgame by a simple, cheap
    heuristic rather than real positional understanding (no engine can
    reliably say "this is strategically an endgame" without much deeper
    analysis than this project does per move).

    Opening is purely move-count based (moves 1-10) since that's genuinely
    how openings are understood — a fixed number of moves before both
    sides have mostly developed, regardless of the actual position.

    Endgame is the OR of two independent signals, either being enough:
    material has thinned out (fewer than ENDGAME_PIECE_COUNT non-king
    pieces — the classic "few pieces left" definition), OR the game has
    simply gone long (move 30+) even if material is still on the board,
    since long grinding games function like endgames strategically even
    when pieces remain. This means a move-30+ middlegame-material position
    still gets called "endgame" — a deliberate simplification, not a bug.

    Everything else falls through to middlegame.
    """
    if move_number <= OPENING_MOVE_CUTOFF:
        return "opening"
    if non_king_piece_count < ENDGAME_PIECE_COUNT or move_number >= ENDGAME_MOVE_CUTOFF:
        return "endgame"
    return "middlegame"


def _classify_move(m: dict) -> dict | None:
    """Grade one move (a dict from analyze_game_moves) and return its
    mistake record, or None if it wasn't inaccurate enough to flag.
    Factored out of detect_mistakes() so analyze_and_store_game() can
    derive flagged mistakes AND store the full move trace from a single
    analyze_game_moves() pass, instead of analyzing the same game twice.
    """
    is_white = m["color_moved"] == "white"
    eval_before = m["eval_before_cp"] if is_white else -m["eval_before_cp"]
    eval_after = m["eval_after_cp"] if is_white else -m["eval_after_cp"]
    eval_drop = eval_before - eval_after

    severity = classify_severity(eval_drop)
    if severity is None:
        return None

    phase = classify_phase(m["move_number"], m["non_king_piece_count"])

    return {
        "ply": m["ply"],
        "move_number": m["move_number"],
        "move_san": m["move_san"],
        "color_moved": m["color_moved"],
        "phase": phase,
        "severity": severity,
        "eval_before": eval_before,
        "eval_after": eval_after,
        "eval_drop": eval_drop,
        "clock_seconds_remaining": m["clock_seconds_remaining"],
    }


def detect_mistakes(pgn_text: str, depth: int = STOCKFISH_DEPTH) -> list[dict]:
    """Analyze a game and return the list of flagged mistakes (moves whose
    eval drop, from the mover's perspective, meets the inaccuracy threshold
    or worse).
    """
    moves = analyze_game_moves(pgn_text, depth=depth)
    return [flagged for m in moves if (flagged := _classify_move(m)) is not None]


def analyze_and_store_game(game_id: int, pgn_text: str, depth: int = STOCKFISH_DEPTH) -> list[dict] | None:
    """Run mistake detection for one game and persist results to the
    `mistakes` table. Marks the game as analyzed so Stage 5's batch runner
    can skip it on re-runs.

    Returns None (instead of a list) if the game uses a non-standard
    variant and was skipped rather than analyzed.
    """
    variant = get_pgn_variant(pgn_text)
    if variant not in STANDARD_VARIANT_TAGS:
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE games SET analyzed = 1, skip_reason = ?, analyzed_at = datetime('now') WHERE id = ?",
                (f"unsupported variant: {variant}", game_id),
            )
            conn.commit()
        finally:
            conn.close()
        return None

    moves = analyze_game_moves(pgn_text, depth=depth)
    flagged = [m for move in moves if (m := _classify_move(move)) is not None]

    conn = get_connection()
    try:
        # Re-analyzing a game whose mistakes already have generated
        # puzzles (Stage B) would otherwise violate puzzles.mistake_id's
        # foreign key when the old mistakes rows are deleted below. Their
        # puzzles would be stale anyway (still pointing at eval/position
        # data for a mistake that's about to be replaced), so drop them
        # too — puzzles.py will regenerate for whatever this re-analysis
        # finds instead, next time it's run.
        conn.execute(
            "DELETE FROM puzzles WHERE mistake_id IN (SELECT id FROM mistakes WHERE game_id = ?)",
            (game_id,),
        )
        conn.execute("DELETE FROM mistakes WHERE game_id = ?", (game_id,))
        conn.executemany(
            """
            INSERT INTO mistakes
                (game_id, ply, move_number, move_san, color_moved, phase, severity,
                 eval_before, eval_after, eval_drop, clock_seconds_remaining)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    game_id, m["ply"], m["move_number"], m["move_san"], m["color_moved"],
                    m["phase"], m["severity"], m["eval_before"], m["eval_after"],
                    m["eval_drop"], m["clock_seconds_remaining"],
                )
                for m in flagged
            ],
        )

        # Full per-move eval trace (Final Pass extension — game analysis
        # view), from the same analyze_game_moves() pass, so a full-game
        # review doesn't cost a second round of Stockfish work.
        conn.execute("DELETE FROM game_moves WHERE game_id = ?", (game_id,))
        conn.executemany(
            """
            INSERT INTO game_moves
                (game_id, ply, move_number, color_moved, move_san, eval_cp, clock_seconds_remaining)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    game_id, move["ply"], move["move_number"], move["color_moved"],
                    move["move_san"], move["eval_cp"], move["clock_seconds_remaining"],
                )
                for move in moves
            ],
        )

        conn.execute(
            "UPDATE games SET analyzed = 1, analyzed_at = datetime('now') WHERE id = ?",
            (game_id,),
        )
        conn.commit()
    finally:
        conn.close()

    return flagged


if __name__ == "__main__":
    from db import get_all_games

    games = get_all_games()
    if not games:
        print("No games in DB yet. Run db.py first (Stage 2).")
        raise SystemExit(1)

    test_game = games[0]
    print(f"Detecting mistakes in: {test_game['source']} | {test_game['date']} | "
          f"{test_game['color']} vs {test_game['opponent']}\n")

    flagged = analyze_and_store_game(test_game["id"], test_game["pgn"])

    if not flagged:
        print("No mistakes flagged (>= inaccuracy threshold) in this game.")
    for m in flagged:
        print(f"  move {m['move_number']:>3} ({m['color_moved']:<5}) {m['move_san']:<8} "
              f"{m['severity']:<11} drop={m['eval_drop']:>7.0f}cp "
              f"[{m['eval_before']:.0f} -> {m['eval_after']:.0f}] "
              f"phase={m['phase']:<11} clock={m['clock_seconds_remaining']}")

    print(f"\n{len(flagged)} mistakes flagged and stored for game_id={test_game['id']}")

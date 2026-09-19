"""
Game Report — full ten-tier move classification, phase-by-phase accuracy,
and a short coach-style summary. This is this project's version of what
other chess sites call a post-game "Game Review"/"Game Report": Full Game
Review (migration 4) already grades every move Best/Excellent/Good/
Inaccuracy/Mistake/Blunder; this module adds four more move labels those
six can't express on their own — Brilliant, Great, Book, Miss — plus a
summary that ties the whole game together.

Deliberately does NOT include a per-game "estimated rating" from move
quality — tried twice (see CHANGELOG), and the underlying signal turned
out not to exist: this player's own real rating history shows essentially
zero correlation between one game's accuracy and their actual rating at
any time control. No calibration scheme fixes a relationship that isn't
there in the data.

None of this is a clone of any commercial site's proprietary algorithm
(none of them publish one) or its wording — the classification rules
below are this project's own documented heuristics, described plainly so
their limits are visible rather than implied to be more precise than
they are.

Deliberately separate from the routine analysis pipeline
(mistakes.analyze_and_store_game, used by batch_analyze.py and the Celery
task in tasks.py): classifying Brilliant/Great needs to know which move
was actually the engine's top choice at each position, which costs a
second engine query per move (MultiPV=2) on top of the eval query routine
analysis already does — roughly double the engine time. Worth paying for
an on-demand "give me the full report" view of one game; not worth paying
for every game in a bulk batch run. Requires the game to already be
analyzed (game_moves populated) — this only adds the extra layer on top.
"""

import io
import json
import logging
from collections import Counter
from datetime import datetime, timezone

import chess
import chess.engine
import chess.pgn

import engine_pool
import stats
from analysis import MATE_SCORE_CP, get_engine
from config import STOCKFISH_DEPTH, STOCKFISH_MAX_SECONDS_PER_POSITION
from db import get_connection
from eco import classify_game_opening, moves_from_pgn

logger = logging.getLogger(__name__)

PIECE_VALUES = {
    chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
    chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0,
}

# A "best" move only counts as Brilliant if the position wasn't already
# lopsided — sacrificing material when you're already crushing (or lost)
# isn't the same kind of find as one that creates the advantage.
BRILLIANT_MAX_EVAL_CP = 500
# Minimum uncompensated material (in pawns-worth) the mover has to offer,
# net of whatever they captured, to call a move a sacrifice.
SACRIFICE_MIN_NET_CP = 2  # roughly a minor piece
# Eval gap to the runner-up move below which a position counts as "sharp
# enough that only the top move keeps the advantage" — the proxy this
# module uses for "Great" (an only-good-move find that wasn't literally
# the single best line but was the only other move that also held up).
GREAT_GAP_THRESHOLD_CP = 150
# How much of an advantage the mover already had before a subsequent
# mistake/blunder counts as "missed a win" (Miss) rather than "let the
# position get worse" (plain Mistake/Blunder).
MISS_MIN_PRIOR_ADVANTAGE_CP = 150


# Per-game "estimated rating" was removed entirely (see CHANGELOG for the
# two prior attempts at fixing it, and why they weren't enough). The final
# check: correlation between a game's own Lichess-formula accuracy% and
# this player's actual rating across their own real analyzed games came
# out at -0.08 (bullet), 0.09 (blitz), -0.22 (rapid) — statistical noise,
# not a relationship. A single game's move quality genuinely doesn't
# predict a real rating for this player at any of their time controls, so
# no calibration scheme fixes it; the honest fix is not showing a number
# that isn't there. For a rating figure with real predictive grounding,
# see stats.py's FIDE Tournament Performance Rating below — that's built
# from actual game RESULTS against real opponents, not move quality.


# --- Move classification enrichment -----------------------------------------

def _top_moves_cp(engine: chess.engine.SimpleEngine, board: chess.Board, depth: int,
                   n: int = 2) -> list[tuple[str, float]]:
    """[(uci, cp_from_white's_perspective), ...] for the engine's top `n`
    moves at `board`, best first — same sign convention as
    analysis.evaluate_position_cp so a difference between two of these
    values is meaningful regardless of whose move it is.
    """
    limit = chess.engine.Limit(depth=depth, time=STOCKFISH_MAX_SECONDS_PER_POSITION)
    infos = engine.analyse(board, limit, multipv=n)
    return [(info["pv"][0].uci(), info["score"].white().score(mate_score=MATE_SCORE_CP)) for info in infos]


def _is_sacrifice(board_before: chess.Board, move: chess.Move) -> bool:
    """True if the opponent's best full capture sequence on move.to_square
    (see _see) nets them at least SACRIFICE_MIN_NET_CP more than the mover
    just captured getting there — a genuine, uncompensated material
    offer, not an ordinary trade.

    Uses Static Exchange Evaluation
    (https://www.chessprogramming.org/Static_Exchange_Evaluation) rather
    than the one-ply "is there any defender at all" check this used to be:
    that couldn't tell a real sacrifice from a square defended once but
    attacked twice (the second attacker gets punished by a third
    defender, so the FIRST attacker taking is still fine for the mover),
    and it always assumed capturing was worth it for the opponent the
    moment any of their pieces attacked the square, even when a deeper
    look shows they'd come out behind by doing so.

    Still not an engine call: it can't see a sacrifice that only pays off
    several moves later, or one that leaves some other already-loose
    piece hanging instead of the piece just moved. Acceptable misses for
    something whose job is narrowing "best, top-choice moves" down to the
    ones worth flagging as Brilliant, not proving a sacrifice is
    objectively sound.
    """
    captured_value = _capture_value(board_before, move)

    board_after = board_before.copy()
    board_after.push(move)

    opponent_gain = _see(board_after, move.to_square)
    net_material_offered = opponent_gain - captured_value
    return net_material_offered >= SACRIFICE_MIN_NET_CP


# SEE needs a real, large value for "a king is capturing" so it's only
# ever used as a last resort (no other legal recapture exists) — the King
# entry in PIECE_VALUES is 0, but that's a placeholder for a king being
# *captured*, which never happens in legal chess and is never exercised.
_SEE_KING_ATTACKER_VALUE = 1000


def _see_attacker_value(board: chess.Board, square: chess.Square) -> int:
    piece = board.piece_at(square)
    return _SEE_KING_ATTACKER_VALUE if piece.piece_type == chess.KING else PIECE_VALUES[piece.piece_type]


def _capture_value(board: chess.Board, move: chess.Move) -> int:
    """Value (pawns) of whatever `move` captures on `board`, 0 if it's not
    a capture. Handles en passant, where the captured pawn doesn't sit on
    move.to_square."""
    if board.is_en_passant(move):
        return PIECE_VALUES[chess.PAWN]
    captured = board.piece_at(move.to_square)
    return PIECE_VALUES[captured.piece_type] if captured else 0


def _see(board: chess.Board, square: chess.Square) -> int:
    """Static Exchange Evaluation: net material (pawns) the side to move
    in `board` can force by capturing on `square` and continuing to trade
    only when it's still profitable, cheapest attacker first each time —
    the standard recursive formulation
    (https://www.chessprogramming.org/Static_Exchange_Evaluation).
    0 if they have no legal capture there, or if capturing isn't worth it
    (a side can always choose not to continue an exchange that loses them
    material, which is what the max(0, ...) below encodes).

    Walks the sequence with real Board.push() calls and real legal moves
    rather than hand-rolled attacker bitboards, so promotions, en passant,
    and check/pin legality all fall out for free — including cases a
    typical bitboard SEE glosses over, like a piece that can't recapture
    because it's pinned to its own king.
    """
    candidates = [m for m in board.legal_moves if m.to_square == square and board.is_capture(m)]
    if not candidates:
        return 0
    # PIECE_VALUES[KING] is 0 — a placeholder that's never exercised for a
    # *captured* piece (kings are never actually captured in legal chess)
    # but would be very wrong here, where the king can be one of the
    # candidates doing the capturing: picking the "cheapest" attacker by
    # that value would greedily grab with the king first, when SEE needs
    # the opposite — spend it only when it's the sole legal recapture.
    move = min(candidates, key=lambda m: _see_attacker_value(board, m.from_square))
    captured_value = _capture_value(board, move)

    board = board.copy()
    board.push(move)
    return max(0, captured_value - _see(board, square))


def _classify_enriched(*, tier: str, eval_before_cp: float, is_top_choice: bool,
                        gap_to_runner_up: float | None, is_book: bool, is_sacrifice: bool) -> str:
    """`eval_before_cp` is already in the MOVER's own perspective (the
    convention game_moves.eval_before_cp is stored in — see
    mistakes.analyze_and_store_game), so no color-based sign flip is
    needed here. `tier` is the existing six-tier grade for this move
    (best/excellent/good/inaccuracy/mistake/blunder); this either upgrades
    it to one of the four new labels or returns it unchanged.
    """
    if is_book:
        return "book"

    if tier in ("mistake", "blunder"):
        if eval_before_cp >= MISS_MIN_PRIOR_ADVANTAGE_CP:
            return "miss"
        return tier

    if tier == "best":
        if is_top_choice and is_sacrifice and abs(eval_before_cp) < BRILLIANT_MAX_EVAL_CP:
            return "brilliant"
        return "best"

    if tier == "excellent":
        if is_top_choice and gap_to_runner_up is not None and gap_to_runner_up >= GREAT_GAP_THRESHOLD_CP:
            return "great"
        return "excellent"

    return tier  # good / inaccuracy pass through unchanged


def _line_value(line: dict) -> float:
    """One comparable number for an engine line (mover's perspective), a
    forced mate mapped onto the same +-MATE_SCORE_CP scale the analysis
    pipeline stores."""
    if line.get("mate") is not None:
        return float(MATE_SCORE_CP - abs(line["mate"])) * (1 if line["mate"] > 0 else -1)
    return float(line["cp"] or 0)


def compute_enriched_classification(game_id: int, depth: int = STOCKFISH_DEPTH) -> list[dict]:
    """The full review pass over an already-analyzed game: searches every
    position (both colors) with MultiPV=2 and writes, onto each game_moves
    row, the enriched classification (Brilliant/Great/Book/Miss on top of the
    six routine tiers), is_top_choice, and the engine's best move and line in
    the position BEFORE that move (best_move_uci / best_pv_uci / best_eval_cp
    / best_mate_in, mover's perspective) — what "Best was Nf3" and the
    coach's explanations are built from. Idempotent — re-running it just
    recomputes and overwrites. Raises ValueError if the game hasn't been
    analyzed yet (no game_moves rows).

    Independent positions, so the searches run in parallel across the shared
    engine_pool (a typical 40-move game takes a few seconds instead of the
    ~30s a single sequential engine needs) — callers on a request/response
    path should still run this in the background and poll (see server.py).
    """
    conn = get_connection()
    try:
        game_row = conn.execute("SELECT pgn FROM games WHERE id = ?", (game_id,)).fetchone()
        move_rows = conn.execute(
            "SELECT ply, move_number, color_moved, eval_before_cp, eval_drop, tier, phase "
            "FROM game_moves WHERE game_id = ? ORDER BY ply",
            (game_id,),
        ).fetchall()
    finally:
        conn.close()

    if game_row is None:
        raise ValueError(f"Game {game_id} not found")
    if not move_rows:
        raise ValueError(f"Game {game_id} hasn't been analyzed yet — run analysis first.")

    game = chess.pgn.read_game(io.StringIO(game_row["pgn"]))
    if game is None:
        raise ValueError(f"Game {game_id}'s PGN couldn't be parsed")

    eco_match = classify_game_opening(moves_from_pgn(game_row["pgn"]))
    book_ply_cutoff = eco_match["ply_count"] if eco_match else 0

    move_rows_by_ply = {r["ply"]: r for r in move_rows}
    board = game.board()
    todo = []  # (ply, board before the move, move)
    for ply, node in enumerate(game.mainline(), start=1):
        if ply in move_rows_by_ply:
            todo.append((ply, board.copy(), node.move))
        board.push(node.move)

    all_lines = engine_pool.analyse_many([b.fen() for _, b, _ in todo], depth, multipv=2)

    updates = []
    for (ply, board_before, move), lines in zip(todo, all_lines):
        row = move_rows_by_ply[ply]
        is_top_choice = bool(lines) and lines[0]["uci"] == move.uci()
        gap = abs(_line_value(lines[0]) - _line_value(lines[1])) if len(lines) >= 2 else None
        sac = _is_sacrifice(board_before, move) if is_top_choice else False

        classification = _classify_enriched(
            tier=row["tier"], eval_before_cp=row["eval_before_cp"],
            is_top_choice=is_top_choice, gap_to_runner_up=gap,
            is_book=ply <= book_ply_cutoff, is_sacrifice=sac,
        )
        best = lines[0] if lines else None
        updates.append((
            classification, int(is_top_choice),
            best["uci"] if best else None, " ".join(best["pv"]) if best else None,
            best["cp"] if best else None, best["mate"] if best else None,
            game_id, ply,
        ))

    conn = get_connection()
    try:
        conn.executemany(
            "UPDATE game_moves SET classification = ?, is_top_choice = ?, best_move_uci = ?, best_pv_uci = ?, "
            "best_eval_cp = ?, best_mate_in = ? WHERE game_id = ? AND ply = ?",
            updates,
        )
        conn.execute("UPDATE games SET reviewed_at = datetime('now') WHERE id = ?", (game_id,))
        conn.commit()
    finally:
        conn.close()

    return [
        {"ply": u[7], "classification": u[0], "is_top_choice": bool(u[1]), "best_move_uci": u[2]}
        for u in updates
    ]


# --- Game Report -------------------------------------------------------------

def _build_summary(accuracy: float | None, tier_counts: dict, phase_accuracy: dict) -> str:
    if accuracy is None:
        return "Not enough analyzed moves to summarize this game."

    parts = [f"You played this game at {accuracy}% accuracy."]

    brilliant = tier_counts.get("brilliant", 0)
    if brilliant:
        parts.append(f"You found {brilliant} brilliant move{'s' if brilliant != 1 else ''}.")

    great = tier_counts.get("great", 0)
    if great:
        parts.append(f"{great} great move{'s' if great != 1 else ''} held the position together in a sharp moment.")

    miss = tier_counts.get("miss", 0)
    if miss:
        parts.append(f"You missed {miss} winning tactic{'s' if miss != 1 else ''} — worth reviewing in Puzzles.")

    present = {p: a for p, a in phase_accuracy.items() if a is not None}
    if len(present) > 1:
        weakest = min(present, key=present.get)
        parts.append(f"Your {weakest} was the weakest phase this game ({present[weakest]}% accuracy).")

    return " ".join(parts)


def _report_row_to_dict(row) -> dict:
    return {
        "game_id": row["game_id"],
        "accuracy_overall": row["accuracy_overall"],
        "accuracy_opening": row["accuracy_opening"],
        "accuracy_middlegame": row["accuracy_middlegame"],
        "accuracy_endgame": row["accuracy_endgame"],
        "tier_counts": json.loads(row["tier_counts"]),
        "summary": row["summary"],
        "computed_at": row["computed_at"],
    }


def generate_game_report(game_id: int, force: bool = False) -> dict:
    """The main entry point: returns a cached report if one already exists
    AND is still fresh (unless `force`), otherwise runs
    compute_enriched_classification() and builds+caches a new one. Raises
    ValueError if the game doesn't exist or hasn't been analyzed yet.
    """
    conn = get_connection()
    try:
        game = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        if not force and game is not None:
            cached = conn.execute("SELECT * FROM game_reports WHERE game_id = ?", (game_id,)).fetchone()
            # analyzed_at is bumped on every analyze_and_store_game() call,
            # including a forced re-analysis of an already-analyzed game
            # (mistakes.py's own docstring calls this out as a supported,
            # deliberate use). Without this comparison, a game re-analyzed
            # after its report was cached kept showing the stale report
            # (old accuracy/rating/tier_counts/summary) indefinitely —
            # /api/games/{id} (uncached, reads game_moves live) and
            # /api/games/{id}/report (cached) would silently disagree.
            if cached and (game["analyzed_at"] is None or cached["computed_at"] >= game["analyzed_at"]):
                return _report_row_to_dict(cached)
    finally:
        conn.close()

    if game is None:
        raise ValueError(f"Game {game_id} not found")
    if not game["analyzed"]:
        raise ValueError("This game hasn't been analyzed yet.")

    compute_enriched_classification(game_id)

    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT ply, color_moved, phase, eval_cp, eval_drop, eval_before_cp, classification "
            "FROM game_moves WHERE game_id = ?",
            (game_id,),
        ).fetchall()
    finally:
        conn.close()

    all_moves = [dict(m) for m in rows]
    own_moves = [m for m in all_moves if m["color_moved"] == game["color"]]

    # compute_game_accuracy (a direct port of Lichess's own algorithm) needs
    # the unfiltered, both-colors move list — its volatility weighting looks
    # at a sliding window across the whole game, which breaks if pre-
    # filtered to one color.
    accuracy_overall = stats.compute_game_accuracy(all_moves, game["color"])

    phase_accuracy = {
        phase: stats.compute_game_accuracy(
            [m for m in all_moves if m["phase"] == phase], game["color"],
        )
        for phase in ("opening", "middlegame", "endgame")
    }

    tier_counts = dict(Counter(m["classification"] for m in own_moves if m["classification"]))
    summary = _build_summary(accuracy_overall, tier_counts, phase_accuracy)

    report = {
        "game_id": game_id,
        "accuracy_overall": accuracy_overall,
        "accuracy_opening": phase_accuracy["opening"],
        "accuracy_middlegame": phase_accuracy["middlegame"],
        "accuracy_endgame": phase_accuracy["endgame"],
        "tier_counts": tier_counts,
        "summary": summary,
    }

    # Computed in Python (not SQL's datetime('now')) so the same value can
    # go straight into the returned dict below without a second read —
    # formatted to match SQLite's own datetime() text format for
    # consistency with every other timestamp column in this database.
    computed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO game_reports
                (game_id, accuracy_overall, accuracy_opening, accuracy_middlegame, accuracy_endgame,
                 tier_counts, summary, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_id) DO UPDATE SET
                accuracy_overall = excluded.accuracy_overall,
                accuracy_opening = excluded.accuracy_opening,
                accuracy_middlegame = excluded.accuracy_middlegame,
                accuracy_endgame = excluded.accuracy_endgame,
                tier_counts = excluded.tier_counts,
                summary = excluded.summary,
                computed_at = excluded.computed_at
            """,
            (
                game_id, accuracy_overall, phase_accuracy["opening"], phase_accuracy["middlegame"],
                phase_accuracy["endgame"], json.dumps(tier_counts), summary, computed_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    report["computed_at"] = computed_at
    return report

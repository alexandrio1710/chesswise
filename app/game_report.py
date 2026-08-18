"""
Game Report — full ten-tier move classification, an estimated per-game
performance rating, phase-by-phase accuracy, and a short coach-style
summary. This is this project's version of what other chess sites call a
post-game "Game Review"/"Game Report": Full Game Review (migration 4)
already grades every move Best/Excellent/Good/Inaccuracy/Mistake/Blunder;
this module adds four more move labels those six can't express on their
own — Brilliant, Great, Book, Miss — plus a rating estimate and a summary
that ties the whole game together.

None of this is a clone of any commercial site's proprietary algorithm
(none of them publish one) or its wording — the classification rules and
the rating curve below are this project's own documented heuristics,
described plainly so their limits are visible rather than implied to be
more precise than they are.

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
import math
from collections import Counter
from datetime import datetime, timezone

import chess
import chess.pgn

import stats
from analysis import MATE_SCORE_CP, get_engine
from config import STOCKFISH_DEPTH
from db import get_connection
from eco import classify_game_opening, moves_from_pgn
from mistakes import classify_phase

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


# --- Estimated performance rating -------------------------------------------
# Piecewise-linear interpolation between (ACPL, rating) anchor points. This
# is NOT a statistically fitted model — it's a rough, documented
# approximation following the well-known *direction* of the relationship
# between average centipawn loss and playing strength (stronger players
# lose fewer centipawns per move on average), calibrated by eye against
# commonly-cited ballpark ACPL ranges per rating band, not against a real
# dataset. Treat the output as "roughly what strength this one game's move
# quality resembles", not a measurement of the player's actual rating —
# a single game's small sample size and the engine's own move-to-move
# noise make it noisy by nature, same caveat stats.compute_game_accuracy
# already carries for the accuracy score this rating is derived alongside.
ACPL_RATING_ANCHORS = [
    (10, 2700), (20, 2400), (35, 2200), (50, 2000), (70, 1800),
    (100, 1600), (140, 1400), (190, 1200), (250, 1000), (350, 800), (500, 600),
]


def estimate_performance_rating(acpl: float) -> int:
    anchors = ACPL_RATING_ANCHORS
    if acpl <= anchors[0][0]:
        return anchors[0][1]
    if acpl >= anchors[-1][0]:
        return anchors[-1][1]
    for (acpl_lo, rating_lo), (acpl_hi, rating_hi) in zip(anchors, anchors[1:]):
        if acpl_lo <= acpl <= acpl_hi:
            frac = (acpl - acpl_lo) / (acpl_hi - acpl_lo)
            return round(rating_lo + frac * (rating_hi - rating_lo))
    return anchors[-1][1]  # unreachable given the bounds checks above


# --- Move classification enrichment -----------------------------------------

def _top_moves_cp(engine, white_to_move: bool, n: int = 2) -> list[tuple[str, float]]:
    """[(uci, cp_from_white's_perspective), ...] for the engine's top `n`
    moves at whatever position is currently set, best first — same sign
    convention as analysis.evaluate_position_cp so a difference between two
    of these values is meaningful regardless of whose move it is.
    """
    raw = engine.get_top_moves(n)
    out = []
    for m in raw:
        if m.get("Mate") is not None:
            mate_in = m["Mate"]
            cp = (MATE_SCORE_CP - mate_in) if mate_in > 0 else (-MATE_SCORE_CP - mate_in) if mate_in < 0 else 0.0
        else:
            cp = float(m["Centipawn"])
        out.append((m["Move"], cp if white_to_move else -cp))
    return out


def _is_sacrifice(board_before: chess.Board, move: chess.Move) -> bool:
    """True if `move` puts its own piece on a square the opponent can
    capture AND the mover has nothing defending that square — a genuine,
    uncompensated material offer, not an ordinary trade.

    Both conditions matter. Checking only "the opponent can capture on
    move.to_square" isn't enough: most developed pieces sit on squares
    something could technically capture on but a pawn or piece also
    guards, so taking it just costs the opponent their own piece back —
    an ordinary trade, not a sacrifice. And checking "is anything on the
    board capturable afterward" (rather than specifically the piece that
    just moved) is worse: in a rough game with an unrelated piece already
    hanging from an earlier move, that flagged ordinary quiet moves —
    developing a knight, castling — as sacrifices with no connection to
    the move actually played.

    Still a static one-ply heuristic, not an engine call: it can't see a
    sacrifice that only pays off several moves later, or one that leaves
    some other already-loose piece hanging instead of the piece just
    moved, and it can't tell a genuine sacrifice from one the opponent
    can't safely take (e.g. the recapturing piece is pinned). Acceptable
    misses for something whose job is narrowing "best, top-choice moves"
    down to the ones worth flagging as Brilliant, not proving a sacrifice
    is objectively sound.
    """
    mover_color = board_before.turn
    moved_piece = board_before.piece_at(move.from_square)
    moved_value = PIECE_VALUES[moved_piece.piece_type] if moved_piece else 0
    captured = board_before.piece_at(move.to_square)
    captured_value = PIECE_VALUES[captured.piece_type] if captured else 0

    board_after = board_before.copy()
    board_after.push(move)

    opponent_can_capture = any(
        reply.to_square == move.to_square
        for reply in board_after.legal_moves
        if board_after.is_capture(reply)
    )
    if not opponent_can_capture:
        return False

    # If the mover still has a piece defending the square it just landed
    # on, the opponent capturing there just leads to an even recapture —
    # a completely ordinary trade (e.g. developing a knight to a square a
    # pawn already guards), not a sacrifice. Without this check, any
    # defended piece offered for trade looked identical to a real
    # giveaway — this was the actual bug behind the false positives this
    # heuristic first shipped with.
    if board_after.attackers(mover_color, move.to_square):
        return False

    net_material_offered = moved_value - captured_value
    return net_material_offered >= SACRIFICE_MIN_NET_CP


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


def compute_enriched_classification(game_id: int, depth: int = STOCKFISH_DEPTH) -> list[dict]:
    """Runs the extra MultiPV=2 pass over an already-analyzed game and
    writes classification/is_top_choice/phase onto every game_moves row.
    Idempotent — re-running it just recomputes and overwrites. Raises
    ValueError if the game hasn't been analyzed yet (no game_moves rows).

    Even at the routine analysis depth (not PUZZLE_DEPTH's deeper search),
    this still costs roughly one extra engine query per move on top of
    what analyze_and_store_game() already did (~0.5s/move measured on the
    dev machine this was built on — a ~40-move game is on the order of
    20-30s) — callers on a request/response path should run this in the
    background and poll rather than blocking on it (see server.py's
    /api/games/{id}/report, which does exactly that).
    """
    conn = get_connection()
    try:
        game_row = conn.execute("SELECT pgn FROM games WHERE id = ?", (game_id,)).fetchone()
        move_rows = conn.execute(
            "SELECT ply, move_number, color_moved, eval_before_cp, eval_drop, tier "
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
    engine = get_engine(depth)
    board = game.board()

    updates = []
    node = game
    ply = 0
    while node.variations:
        next_node = node.variations[0]
        move = next_node.move
        ply += 1
        row = move_rows_by_ply.get(ply)
        if row is None:
            # A move without a stored eval trace (shouldn't normally
            # happen for a fully analyzed game) — skip rather than crash
            # the whole report over one gap.
            board.push(move)
            node = next_node
            continue

        white_to_move = board.turn == chess.WHITE
        engine.set_fen_position(board.fen())
        top = _top_moves_cp(engine, white_to_move, n=2)
        is_top_choice = bool(top) and top[0][0] == move.uci()
        gap = abs(top[0][1] - top[1][1]) if len(top) >= 2 else None
        sac = _is_sacrifice(board, move) if is_top_choice else False

        classification = _classify_enriched(
            tier=row["tier"], eval_before_cp=row["eval_before_cp"],
            is_top_choice=is_top_choice, gap_to_runner_up=gap,
            is_book=ply <= book_ply_cutoff, is_sacrifice=sac,
        )

        board.push(move)
        non_king_piece_count = len(board.piece_map()) - 2
        phase = classify_phase(row["move_number"], non_king_piece_count)

        updates.append((classification, int(is_top_choice), phase, game_id, ply))
        node = next_node

    conn = get_connection()
    try:
        conn.executemany(
            "UPDATE game_moves SET classification = ?, is_top_choice = ?, phase = ? "
            "WHERE game_id = ? AND ply = ?",
            updates,
        )
        conn.commit()
    finally:
        conn.close()

    return [
        {"ply": u[4], "classification": u[0], "is_top_choice": bool(u[1]), "phase": u[2]}
        for u in updates
    ]


# --- Game Report -------------------------------------------------------------

def _accuracy_from_acpl(acpl: float | None) -> float | None:
    if acpl is None:
        return None
    return round(max(0.0, min(100.0, 100 * math.exp(-stats.ACCURACY_DECAY_K * acpl))), 1)


def _acpl(moves: list[dict]) -> float | None:
    drops = [max(0.0, m["eval_drop"]) for m in moves if m["eval_drop"] is not None]
    return sum(drops) / len(drops) if drops else None


def _build_summary(accuracy: float | None, rating: int | None, tier_counts: dict, phase_accuracy: dict) -> str:
    if accuracy is None:
        return "Not enough analyzed moves to summarize this game."

    parts = [f"You played this game at {accuracy}% accuracy"]
    parts[0] += f", roughly the move quality of a {rating}-rated player." if rating else "."

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
        "estimated_rating": row["estimated_rating"],
        "tier_counts": json.loads(row["tier_counts"]),
        "summary": row["summary"],
        "computed_at": row["computed_at"],
    }


def generate_game_report(game_id: int, force: bool = False) -> dict:
    """The main entry point: returns a cached report if one already exists
    (unless `force`), otherwise runs compute_enriched_classification() and
    builds+caches a new one. Raises ValueError if the game doesn't exist or
    hasn't been analyzed yet.
    """
    conn = get_connection()
    try:
        if not force:
            cached = conn.execute("SELECT * FROM game_reports WHERE game_id = ?", (game_id,)).fetchone()
            if cached:
                return _report_row_to_dict(cached)
        game = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
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
            "SELECT color_moved, phase, eval_drop, classification FROM game_moves WHERE game_id = ?",
            (game_id,),
        ).fetchall()
    finally:
        conn.close()

    own_moves = [dict(m) for m in rows if m["color_moved"] == game["color"]]

    overall_acpl = _acpl(own_moves)
    accuracy_overall = _accuracy_from_acpl(overall_acpl)
    estimated_rating = estimate_performance_rating(overall_acpl) if overall_acpl is not None else None

    phase_accuracy = {
        phase: _accuracy_from_acpl(_acpl([m for m in own_moves if m["phase"] == phase]))
        for phase in ("opening", "middlegame", "endgame")
    }

    tier_counts = dict(Counter(m["classification"] for m in own_moves if m["classification"]))
    summary = _build_summary(accuracy_overall, estimated_rating, tier_counts, phase_accuracy)

    report = {
        "game_id": game_id,
        "accuracy_overall": accuracy_overall,
        "accuracy_opening": phase_accuracy["opening"],
        "accuracy_middlegame": phase_accuracy["middlegame"],
        "accuracy_endgame": phase_accuracy["endgame"],
        "estimated_rating": estimated_rating,
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
                 estimated_rating, tier_counts, summary, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_id) DO UPDATE SET
                accuracy_overall = excluded.accuracy_overall,
                accuracy_opening = excluded.accuracy_opening,
                accuracy_middlegame = excluded.accuracy_middlegame,
                accuracy_endgame = excluded.accuracy_endgame,
                estimated_rating = excluded.estimated_rating,
                tier_counts = excluded.tier_counts,
                summary = excluded.summary,
                computed_at = excluded.computed_at
            """,
            (
                game_id, accuracy_overall, phase_accuracy["opening"], phase_accuracy["middlegame"],
                phase_accuracy["endgame"], estimated_rating, json.dumps(tier_counts), summary, computed_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    report["computed_at"] = computed_at
    return report

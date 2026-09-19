"""
Per-game metadata Insights groups by, all derivable without an engine:

- how the game ended (checkmate, resignation, timeout, abandonment, stalemate,
  repetition, agreement, ...) — from the PGN's own Termination header where the
  site provides one (Chess.com does, in words), else inferred from the final
  position (Lichess only says "Normal" or "Time forfeit");
- the game's "shape" (balanced, sharp, wild, giveaway, smooth, sudden,
  intense) from its evaluation curve — chess.com Insights groups games this
  way but has never published the rules, only one-line descriptions ("one player
  was winning, then gave it away"; "a close game lost by a mistake"; ...), so
  these thresholds are this project's own reading of those descriptions;
- how many plies the game stayed in known opening theory.

Stored on the games row (termination_method / game_shape / book_plies, NULL
until computed) by ensure_metadata(), which Insights calls before querying.
"""

from __future__ import annotations

import io
import logging

import chess
import chess.pgn

from db import get_connection
from eco import classify_game_opening, moves_from_pgn
from stats import win_percent

logger = logging.getLogger(__name__)

TERMINATION_METHODS = [
    "checkmate", "resignation", "timeout", "abandonment", "stalemate", "repetition",
    "insufficient_material", "timeout_vs_insufficient", "agreement", "fifty_move", "other",
]

TERMINATION_LABELS = {
    "checkmate": "Checkmate", "resignation": "Resignation", "timeout": "On time",
    "abandonment": "Abandonment", "stalemate": "Stalemate", "repetition": "Repetition",
    "insufficient_material": "Insufficient material", "timeout_vs_insufficient": "Timeout vs. insufficient material",
    "agreement": "Agreement", "fifty_move": "50-move rule", "other": "Other",
}

SHAPES = ["balanced", "sharp", "wild", "giveaway", "smooth", "sudden", "intense"]

SHAPE_BLURBS = {
    "balanced": "Neither player had a real advantage.",
    "sharp": "A back-and-forth game where both players had chances.",
    "wild": "A chaotic game — the lead changed hands again and again.",
    "giveaway": "One player was winning, then gave it away.",
    "smooth": "One player took the advantage and never let go.",
    "sudden": "A close game decided by a single mistake.",
    "intense": "A serious fight with mistakes on both sides.",
}


def classify_termination(pgn_text: str) -> str:
    """One of TERMINATION_METHODS."""
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return "other"
    term = game.headers.get("Termination", "").lower()
    result = game.headers.get("Result", "*")

    # Chess.com spells it out ("someone won by resignation", "drawn by ...").
    if "timeout vs insufficient" in term:
        return "timeout_vs_insufficient"
    if "checkmate" in term:
        return "checkmate"
    if "resignation" in term:
        return "resignation"
    if "abandon" in term:
        return "abandonment"
    if "on time" in term or "time forfeit" in term or "timeout" in term:
        return "timeout"
    if "repetition" in term:
        return "repetition"
    if "stalemate" in term:
        return "stalemate"
    if "insufficient" in term:
        return "insufficient_material"
    if "agreement" in term:
        return "agreement"
    if "50" in term or "fifty" in term:
        return "fifty_move"

    # Lichess only says "Normal" — read the answer off the final position.
    board = game.end().board()
    if board.is_checkmate():
        return "checkmate"
    if board.is_stalemate():
        return "stalemate"
    if board.is_insufficient_material():
        return "insufficient_material"
    if result in ("1-0", "0-1"):
        return "resignation"
    if result == "1/2-1/2":
        if board.can_claim_threefold_repetition() or board.is_repetition(3):
            return "repetition"
        if board.halfmove_clock >= 100:
            return "fifty_move"
        return "agreement"
    return "other"


def classify_shape(evals_white_cp: list[float]) -> str:
    """The game's shape from its evaluation curve: `evals_white_cp` is the
    engine eval (centipawns, White's perspective) after each ply, in order.

    Everything works in win-percentage points relative to 50 ("advantage"),
    so a +3 lead in a quiet position and a +3 lead in a wild one are judged by
    how likely they were to convert, not by pawn count.
    """
    if len(evals_white_cp) < 8:
        return "balanced"
    adv = [win_percent(cp) - 50.0 for cp in evals_white_cp]
    peak_white, peak_black = max(adv), -min(adv)
    biggest = max(abs(a) for a in adv)

    # Lead changes: swings between a clear White edge and a clear Black edge.
    side = 0
    lead_changes = 0
    for a in adv:
        now = 1 if a > 10 else -1 if a < -10 else 0
        if now and side and now != side:
            lead_changes += 1
        if now:
            side = now
    swings = [abs(adv[i] - adv[i - 1]) for i in range(1, len(adv))]
    big_swings = sum(1 for s in swings if s >= 25)

    # Giveaway: a side was clearly winning (>= ~90% expected score) and the
    # advantage later collapsed by more than 25 points.
    for sign in (1, -1):
        series = [a * sign for a in adv]
        best_so_far = float("-inf")
        for a in series:
            best_so_far = max(best_so_far, a)
            if best_so_far >= 40 and a <= best_so_far - 25 and a <= 15:
                return "giveaway"

    if lead_changes >= 3 or (lead_changes >= 2 and big_swings >= 3):
        return "wild"
    if lead_changes >= 1 and peak_white >= 15 and peak_black >= 15:
        return "sharp"
    if biggest < 20:
        return "balanced"

    # Sudden: a close game (small edges throughout) until one move swung it.
    if swings:
        k = max(range(len(swings)), key=lambda i: swings[i]) + 1
        before = max(abs(a) for a in adv[:k]) if k else 0
        if swings[k - 1] >= 30 and before < 20 and abs(adv[-1]) >= 30:
            return "sudden"

    # Smooth: someone got clearly ahead and stayed there.
    lead_sign = 1 if peak_white >= peak_black else -1
    lead = [a * lead_sign for a in adv]
    first_big = next((i for i, a in enumerate(lead) if a >= 25), None)
    if first_big is not None and all(a >= 0 for a in lead[first_big:]):
        return "smooth"

    return "intense"


def _shape_for_game(conn, game_id: int) -> str | None:
    rows = conn.execute("SELECT eval_cp FROM game_moves WHERE game_id = ? ORDER BY ply", (game_id,)).fetchall()
    if not rows:
        return None
    return classify_shape([r["eval_cp"] for r in rows])


def _book_plies(pgn_text: str) -> int:
    match = classify_game_opening(moves_from_pgn(pgn_text))
    return match["ply_count"] if match else 0


def ensure_metadata(game_ids: list[int] | None = None) -> int:
    """Fill termination_method / game_shape / book_plies for analyzed games
    that don't have them yet. Cheap (pure python-chess over stored PGNs and
    eval traces); returns how many games were updated."""
    conn = get_connection()
    try:
        where = "analyzed = 1 AND skip_reason IS NULL AND (termination_method IS NULL OR game_shape IS NULL OR book_plies IS NULL)"
        params: tuple = ()
        if game_ids is not None:
            if not game_ids:
                return 0
            where += f" AND id IN ({','.join('?' * len(game_ids))})"
            params = tuple(game_ids)
        rows = conn.execute(f"SELECT id, pgn FROM games WHERE {where}", params).fetchall()
        updated = 0
        for row in rows:
            try:
                termination = classify_termination(row["pgn"])
                shape = _shape_for_game(conn, row["id"])
                book = _book_plies(row["pgn"])
            except Exception as e:  # one malformed PGN mustn't block every other game
                logger.warning(f"Couldn't compute metadata for game {row['id']}: {e}")
                continue
            conn.execute(
                "UPDATE games SET termination_method = ?, game_shape = COALESCE(?, game_shape), book_plies = ? WHERE id = ?",
                (termination, shape, book, row["id"]),
            )
            updated += 1
        conn.commit()
        return updated
    finally:
        conn.close()

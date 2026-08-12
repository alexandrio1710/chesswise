"""
Advanced features, Section 3 — Endgame Tablebase Integration.

For positions with 7 or fewer pieces, queries Lichess's free, public
tablebase API (https://tablebase.lichess.ovh) for the theoretically
PERFECT result (win/draw/loss — ground truth, not an engine estimate) and
best move. Used to sharpen endgame-mistake review beyond "the eval
dropped" into "this was a theoretical draw before your move, a loss
after" — and to power a small trainer that replays your own
tablebase-solvable mistakes against provably perfect defense.
"""

import logging
import time

import chess
import requests

from config import API_BACKOFF_BASE_SECONDS, API_MAX_RETRIES

logger = logging.getLogger(__name__)

TABLEBASE_USER_AGENT = "ChessMistakeTracker/1.0 (personal project)"
TABLEBASE_BASE_URL = "https://tablebase.lichess.ovh/standard"
TABLEBASE_MAX_PIECES = 7

# Cached per FEN for the server process's lifetime — tablebase results
# never change, so there's no reason to ever re-fetch the same position.
_tablebase_cache: dict[str, dict | None] = {}

# A tablebase move-list's `category` is reported for the position AFTER
# that move — i.e. from the perspective of whoever moves next (the
# opponent), not the player choosing between the moves. Every other
# eval/result in this app is framed from the mover's own perspective, so
# results get inverted through this map before being handed back.
INVERSE_CATEGORY = {
    "win": "loss", "loss": "win",
    "cursed-win": "blessed-loss", "blessed-loss": "cursed-win",
    "maybe-win": "maybe-loss", "maybe-loss": "maybe-win",
    "draw": "draw", "unknown": "unknown",
}


def is_tablebase_eligible(fen: str) -> bool:
    return len(chess.Board(fen).piece_map()) <= TABLEBASE_MAX_PIECES


def query_tablebase(fen: str) -> dict | None:
    """Raw tablebase lookup for a FEN. Returns None if the position isn't
    eligible (too many pieces), the position has no tablebase entry, or
    the service is unreachable — callers should treat all three the same
    way (this game/position just isn't tablebase-backed right now), not
    crash the page around it.
    """
    if fen in _tablebase_cache:
        return _tablebase_cache[fen]

    if not is_tablebase_eligible(fen):
        _tablebase_cache[fen] = None
        return None

    headers = {"User-Agent": TABLEBASE_USER_AGENT}
    last_error = None
    for attempt in range(1, API_MAX_RETRIES + 1):
        try:
            resp = requests.get(TABLEBASE_BASE_URL, params={"fen": fen}, headers=headers, timeout=15)
            if resp.status_code == 429 and attempt < API_MAX_RETRIES:
                time.sleep(API_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
                continue
            if resp.status_code == 404:
                _tablebase_cache[fen] = None
                return None
            resp.raise_for_status()
            data = resp.json()
            _tablebase_cache[fen] = data
            return data
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < API_MAX_RETRIES:
                time.sleep(API_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

    logger.warning(f"Tablebase API unreachable for this position: {last_error}")
    _tablebase_cache[fen] = None
    return None


def _mover_perspective_moves(tb_data: dict) -> list[dict]:
    moves = []
    for m in tb_data.get("moves", []):
        moves.append({
            "uci": m["uci"],
            "san": m["san"],
            "category": INVERSE_CATEGORY.get(m["category"], m["category"]),
            "dtz": -m["dtz"] if m.get("dtz") is not None else None,
        })
    return moves


def get_tablebase_result(fen: str) -> dict | None:
    """Simplified, mover-perspective tablebase summary: category
    (win/draw/loss/... — for whoever is about to move), best move, and the
    full ranked move list. None if not tablebase-backed right now.
    """
    data = query_tablebase(fen)
    if data is None:
        return None
    moves = _mover_perspective_moves(data)
    return {
        "category": data["category"],
        "dtz": data.get("dtz"),
        "dtm": data.get("dtm"),
        "checkmate": data.get("checkmate", False),
        "stalemate": data.get("stalemate", False),
        "best_move": moves[0] if moves else None,
        "moves": moves,
    }


def analyze_tablebase_mistake(fen_before: str, move_uci: str) -> dict | None:
    """For a flagged endgame mistake: was the position tablebase-solvable,
    and if so, did the actual move played change the theoretical result?
    Both categories are reported from the SAME player's perspective (the
    one who made the mistake) so they're directly comparable — e.g.
    category_before='draw', category_after='loss' reads as "this was a
    theoretical draw before your move, a loss after."

    Returns None if the pre-move position isn't tablebase-eligible/backed
    (most middlegame-adjacent "endgame" mistakes by this app's move-count-
    based phase heuristic won't be — that's expected, not an error).
    """
    before = get_tablebase_result(fen_before)
    if before is None:
        return None

    board = chess.Board(fen_before)
    try:
        move = chess.Move.from_uci(move_uci)
        if move not in board.legal_moves:
            return None
    except ValueError:
        return None
    board.push(move)

    after_raw = query_tablebase(board.fen())
    category_after = INVERSE_CATEGORY.get(after_raw["category"], after_raw["category"]) if after_raw else None

    return {
        "tablebase_solvable": True,
        "category_before": before["category"],
        "category_after": category_after,
        "result_changed": category_after is not None and category_after != before["category"],
        "best_move_san": before["best_move"]["san"] if before["best_move"] else None,
        "best_move_uci": before["best_move"]["uci"] if before["best_move"] else None,
    }


# --- Endgame Trainer ---------------------------------------------------------

def find_endgame_trainer_positions(source: str | None = None, limit: int = 8, scan_limit: int = 40) -> list[dict]:
    """Scan the player's worst endgame-phase mistakes for ones that are
    tablebase-solvable, for the Endgame Trainer queue. Each check is a
    network call, so this is bounded on both ends: only the `scan_limit`
    worst candidates are checked at all, and scanning stops as soon as
    `limit` solvable positions are found.
    """
    from db import get_connection
    from puzzles import board_before_ply, get_pgn

    where = " AND g.source = ?" if source else ""
    params = (source,) if source else ()

    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT m.id as mistake_id, m.game_id, m.ply, m.move_number, m.move_san,
                   m.severity, m.eval_drop, g.opponent, g.date, g.source, g.color
            FROM mistakes m JOIN games g ON m.game_id = g.id
            WHERE m.phase = 'endgame' AND m.severity IN ('mistake', 'blunder') {where}
            ORDER BY m.eval_drop DESC
            LIMIT ?
            """,
            params + (scan_limit,),
        ).fetchall()
    finally:
        conn.close()

    found = []
    pgn_cache: dict[int, str] = {}
    for row in rows:
        if len(found) >= limit:
            break
        game_id = row["game_id"]
        if game_id not in pgn_cache:
            pgn_cache[game_id] = get_pgn(game_id)
        try:
            board = board_before_ply(pgn_cache[game_id], row["ply"])
        except ValueError:
            continue
        fen = board.fen()
        result = get_tablebase_result(fen)
        if result is None:
            continue
        found.append({
            "mistake_id": row["mistake_id"], "game_id": game_id, "fen": fen,
            "move_number": row["move_number"], "move_san": row["move_san"],
            "severity": row["severity"], "eval_drop": row["eval_drop"],
            "opponent": row["opponent"], "date": row["date"], "source": row["source"],
            "category_before": result["category"],
        })
    return found


def trainer_attempt_move(fen: str, move_uci: str) -> dict:
    """One ply of Endgame Trainer play: validate the human's move, grade
    it against tablebase-perfect play (did the theoretical result hold?),
    and — if the position isn't over — have the tablebase reply with its
    own best move, so the human keeps playing against provably perfect
    defense/attack rather than a one-shot puzzle guess.
    """
    before = get_tablebase_result(fen)
    if before is None:
        raise ValueError("Position is not tablebase-solvable")

    board = chess.Board(fen)
    move = chess.Move.from_uci(move_uci)
    if move not in board.legal_moves:
        raise ValueError(f"{move_uci} is not legal here")
    played_san = board.san(move)
    board.push(move)

    if board.is_checkmate():
        return {
            "played_san": played_san, "category_after_human": "win", "correct": True,
            "reply_san": None, "fen_final": board.fen(), "game_over": True,
        }
    if board.is_stalemate() or board.is_insufficient_material():
        return {
            "played_san": played_san, "category_after_human": "draw",
            "correct": before["category"] == "draw",
            "reply_san": None, "fen_final": board.fen(), "game_over": True,
        }

    after_raw = query_tablebase(board.fen())
    category_after_human = INVERSE_CATEGORY.get(after_raw["category"], after_raw["category"]) if after_raw else None
    correct = category_after_human == before["category"]

    reply_san = None
    fen_final = board.fen()
    game_over = False
    if after_raw and after_raw.get("moves"):
        best = after_raw["moves"][0]
        reply_move = chess.Move.from_uci(best["uci"])
        reply_san = board.san(reply_move)
        board.push(reply_move)
        fen_final = board.fen()
        game_over = board.is_checkmate() or board.is_stalemate()
    elif after_raw:
        game_over = after_raw.get("checkmate", False) or after_raw.get("stalemate", False)

    return {
        "played_san": played_san,
        "category_after_human": category_after_human,
        "correct": correct,
        "reply_san": reply_san,
        "fen_final": fen_final,
        "game_over": game_over,
    }

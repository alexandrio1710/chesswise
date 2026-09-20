"""
Play a position out against Stockfish at a chosen strength.

Chess.com and Lichess both let you "play from here" after a mistake; this is
that. Strength comes from Stockfish's own UCI options: UCI_LimitStrength +
UCI_Elo for the 1350-and-up levels, and "Skill Level" for the two gentler ones
(UCI_Elo bottoms out at 1320). A fresh engine is started per reply and closed
right after — a reply takes a fraction of a second, and python-chess keeps a
non-daemon thread per open engine, which would otherwise stop the server from
exiting cleanly.
"""

from __future__ import annotations

import chess
import chess.engine

from analysis import get_engine

# key -> (label, UCI options, seconds to think)
LEVELS: dict[str, dict] = {
    "beginner": {"label": "Beginner", "options": {"UCI_LimitStrength": False, "Skill Level": 1}, "seconds": 0.1},
    "casual": {"label": "Casual", "options": {"UCI_LimitStrength": False, "Skill Level": 5}, "seconds": 0.15},
    "1350": {"label": "~1350", "options": {"UCI_LimitStrength": True, "UCI_Elo": 1350}, "seconds": 0.25},
    "1600": {"label": "~1600", "options": {"UCI_LimitStrength": True, "UCI_Elo": 1600}, "seconds": 0.3},
    "1900": {"label": "~1900", "options": {"UCI_LimitStrength": True, "UCI_Elo": 1900}, "seconds": 0.4},
    "2200": {"label": "~2200", "options": {"UCI_LimitStrength": True, "UCI_Elo": 2200}, "seconds": 0.5},
    "max": {"label": "Full strength", "options": {"UCI_LimitStrength": False, "Skill Level": 20}, "seconds": 1.0},
}
DEFAULT_LEVEL = "1600"


def outcome_of(board: chess.Board) -> dict | None:
    """How the game ended, or None while it's on. `winner` is 'white'/'black'/None (draw)."""
    if board.is_checkmate():
        return {"kind": "checkmate", "winner": "black" if board.turn == chess.WHITE else "white"}
    if board.is_stalemate():
        return {"kind": "stalemate", "winner": None}
    if board.is_insufficient_material():
        return {"kind": "insufficient material", "winner": None}
    if board.can_claim_threefold_repetition() or board.is_fivefold_repetition():
        return {"kind": "repetition", "winner": None}
    if board.can_claim_fifty_moves():
        return {"kind": "fifty-move rule", "winner": None}
    return None


def _configure(engine: chess.engine.SimpleEngine, options: dict) -> None:
    supported = engine.options
    wanted = {}
    for name, value in options.items():
        if name not in supported:
            continue
        opt = supported[name]
        if isinstance(value, int) and not isinstance(value, bool) and opt.min is not None and opt.max is not None:
            value = max(opt.min, min(opt.max, value))
        wanted[name] = value
    engine.configure(wanted)


def reply(fen: str, level: str = DEFAULT_LEVEL) -> dict:
    """The engine's move in `fen` at `level`. Raises ValueError for a bad FEN,
    an unknown level, or a position with no legal moves."""
    if level not in LEVELS:
        raise ValueError(f"Unknown level: {level}")
    try:
        board = chess.Board(fen)
    except ValueError:
        raise ValueError("That isn't a valid position.")
    if not board.is_valid():
        raise ValueError("That position isn't legal.")
    if board.is_game_over():
        raise ValueError("The game is already over.")

    spec = LEVELS[level]
    engine = get_engine()
    try:
        engine.configure({"Hash": 32})
        _configure(engine, spec["options"])
        result = engine.play(board, chess.engine.Limit(time=spec["seconds"]))
    finally:
        engine.quit()
    move = result.move
    san = board.san(move)
    board.push(move)
    return {"uci": move.uci(), "san": san, "fen": board.fen(), "outcome": outcome_of(board)}

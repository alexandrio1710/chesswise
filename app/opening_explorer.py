"""
Advanced features, Section 2 — Opening Explorer.

For any position (reached by clicking through moves from the start),
shows two things side by side:
  - Community reference stats from Lichess's free, public opening explorer
    API (https://explorer.lichess.org/lichess) — aggregate win/draw/loss
    rates across the wider Lichess player base.
  - The player's own stats at that same position, computed by replaying
    every stored, analyzed game (no precomputed index — with a few dozen
    games this is fast enough to do per-request; see get_my_stats_at_position).

Nothing here reproduces Lichess's UI, branding, or any commercial site's
specific product — it's this project's own read of two data sources
(a free public API, and the player's own database) side by side.
"""

import io
import logging
import time

import chess
import chess.pgn
import requests

from config import API_BACKOFF_BASE_SECONDS, API_MAX_RETRIES
from db import get_all_games

logger = logging.getLogger(__name__)

EXPLORER_USER_AGENT = "ChessMistakeTracker/1.0 (personal project)"
EXPLORER_BASE_URL = "https://explorer.lichess.org/lichess"

# Community reference calls are the slow part of this feature (an external
# network round-trip on every click through the explorer) — this project's
# own dataset never changes mid-session, but the community's does slowly
# enough that caching it for the life of the server process is a fine
# trade for the "feel responsive" requirement. Keyed by FEN.
_community_cache: dict[str, dict | None] = {}

# Divergence flag thresholds (Section 2's own spec, implemented literally):
# a candidate move I play less than this often...
DIVERGENCE_MY_MAX_PCT = 5.0
# ...that the community plays at least this often...
DIVERGENCE_COMMUNITY_MIN_PCT = 20.0
# ...at a win rate at least this many points above my own best move's.
DIVERGENCE_MIN_WIN_RATE_EDGE = 10.0


def query_community_explorer(fen: str) -> dict | None:
    """Aggregate stats from Lichess's public opening explorer for a FEN.
    Returns None (rather than raising) on failure — this is reference data
    from a third-party service; its own stats should still render if the
    explorer API is briefly unreachable.
    """
    if fen in _community_cache:
        return _community_cache[fen]

    params = {"variant": "standard", "fen": fen, "speeds": "blitz,rapid,classical"}
    headers = {"User-Agent": EXPLORER_USER_AGENT, "Accept": "application/json"}

    last_error = None
    for attempt in range(1, API_MAX_RETRIES + 1):
        try:
            resp = requests.get(EXPLORER_BASE_URL, params=params, headers=headers, timeout=15)
            if resp.status_code == 429 and attempt < API_MAX_RETRIES:
                wait = API_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            _community_cache[fen] = data
            return data
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < API_MAX_RETRIES:
                time.sleep(API_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

    logger.warning(f"Opening explorer API unreachable for this position: {last_error}")
    _community_cache[fen] = None
    return None


def _board_at_moves(move_ucis: list[str]) -> chess.Board:
    board = chess.Board()
    for uci in move_ucis:
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            raise ValueError(f"'{uci}' isn't a valid move")
        if move not in board.legal_moves:
            raise ValueError(f"'{uci}' isn't legal at this point in the sequence")
        board.push(move)
    return board


def get_my_stats_at_position(move_ucis: list[str], source: str | None = None) -> dict:
    """Replay every stored, analyzed game and check whether it passed
    through the exact move sequence `move_ucis` from the start. For games
    that did: record my result (already normalized to my perspective by
    fetchers.py) and, when the position was reached with me to move, which
    move I actually played next.

    No precomputed index — with a few dozen games, replaying all of them
    with python-chess per request is comfortably fast; see the module
    docstring.
    """
    games = get_all_games(source=source)
    n_target = len(move_ucis)

    reached_games = []
    next_move_counts: dict[str, dict] = {}  # uci -> {count, wins, draws, losses, san}

    for game in games:
        if not game["analyzed"] or game["skip_reason"]:
            continue
        try:
            parsed = chess.pgn.read_game(io.StringIO(game["pgn"]))
        except Exception:
            continue
        if parsed is None:
            continue

        board = parsed.board()
        node = parsed
        matched = True
        for uci in move_ucis:
            if not node.variations:
                matched = False
                break
            move = node.variations[0].move
            if move.uci() != uci:
                matched = False
                break
            board.push(move)
            node = node.variations[0]
        if not matched:
            continue

        reached_games.append(game)

        # Whose move is it after move_ucis? If it's mine and the game
        # continued, that next move is one of "my" choices at this position.
        mover_is_me = (board.turn == chess.WHITE) == (game["color"] == "white")
        if mover_is_me and node.variations:
            next_move = node.variations[0].move
            uci = next_move.uci()
            san = board.san(next_move)
            entry = next_move_counts.setdefault(uci, {"uci": uci, "san": san, "count": 0, "wins": 0, "draws": 0, "losses": 0})
            entry["count"] += 1
            if game["result"] == "win":
                entry["wins"] += 1
            elif game["result"] == "draw":
                entry["draws"] += 1
            else:
                entry["losses"] += 1

    wins = sum(1 for g in reached_games if g["result"] == "win")
    draws = sum(1 for g in reached_games if g["result"] == "draw")
    losses = sum(1 for g in reached_games if g["result"] == "loss")

    my_moves = []
    for entry in next_move_counts.values():
        total = entry["count"]
        my_moves.append({
            **entry,
            "win_rate_pct": round(entry["wins"] / total * 100, 1) if total else 0.0,
        })
    my_moves.sort(key=lambda m: m["count"], reverse=True)
    total_next_moves = sum(m["count"] for m in my_moves)
    for m in my_moves:
        m["pct_of_my_choices"] = round(m["count"] / total_next_moves * 100, 1) if total_next_moves else 0.0

    return {
        "games_reached": len(reached_games),
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "win_rate_pct": round(wins / len(reached_games) * 100, 1) if reached_games else None,
        "my_moves": my_moves,
    }


def _community_move_stats(community: dict, white_to_move: bool) -> list[dict]:
    """Normalize Lichess explorer 'moves' entries: add total game count and
    a win_rate_pct from the mover's own perspective (their color, not
    always White) so it's directly comparable to `my_moves` above.
    """
    if not community or not community.get("moves"):
        return []
    grand_total = sum(m["white"] + m["draws"] + m["black"] for m in community["moves"]) or 1

    result = []
    for m in community["moves"]:
        total = m["white"] + m["draws"] + m["black"]
        if total == 0:
            continue
        # The explorer reports white/draws/black counts; win rate here is
        # for whoever is on move at this position (matches my_moves' framing).
        mover_wins = m["white"] if white_to_move else m["black"]
        result.append({
            "uci": m["uci"],
            "san": m["san"],
            "total": total,
            "total_pct": round(total / grand_total * 100, 1),
            "win_rate_pct": round(mover_wins / total * 100, 1),
        })
    return result


def flag_divergence(my_moves: list[dict], community_moves: list[dict]) -> list[dict]:
    """Positions where I play a move rarely (<5% of my own choices here)
    that the community plays often (>=20% of games) at a notably higher
    win rate (>=10 points above my own best move's win rate) — a candidate
    "you might be missing something statistically stronger here" flag.

    Needs at least a few of my own games at this position to say anything;
    returns [] otherwise rather than drawing conclusions from 1-2 games.
    """
    if not my_moves or sum(m["count"] for m in my_moves) < 3:
        return []

    my_pct_by_uci = {m["uci"]: m["pct_of_my_choices"] for m in my_moves}
    my_top_win_rate = max((m["win_rate_pct"] for m in my_moves), default=0.0)

    flags = []
    for cm in community_moves:
        my_pct = my_pct_by_uci.get(cm["uci"], 0.0)
        if my_pct >= DIVERGENCE_MY_MAX_PCT:
            continue
        if cm["total_pct"] < DIVERGENCE_COMMUNITY_MIN_PCT:
            continue
        if cm["win_rate_pct"] - my_top_win_rate < DIVERGENCE_MIN_WIN_RATE_EDGE:
            continue
        flags.append({
            "move_san": cm["san"],
            "move_uci": cm["uci"],
            "community_pct": cm["total_pct"],
            "community_win_rate_pct": cm["win_rate_pct"],
            "my_pct": my_pct,
        })
    return flags


def explore_position(move_ucis: list[str], source: str | None = None) -> dict:
    board = _board_at_moves(move_ucis)
    fen = board.fen()

    community = query_community_explorer(fen)
    community_moves = _community_move_stats(community, white_to_move=board.turn == chess.WHITE) if community else []
    mine = get_my_stats_at_position(move_ucis, source=source)

    legal_moves = [{"uci": m.uci(), "san": board.san(m)} for m in board.legal_moves]

    return {
        "fen": fen,
        "side_to_move": "white" if board.turn == chess.WHITE else "black",
        "legal_moves": legal_moves,
        "community": {
            "available": community is not None,
            "white": community.get("white") if community else None,
            "draws": community.get("draws") if community else None,
            "black": community.get("black") if community else None,
            "opening": community.get("opening") if community else None,
            "moves": community_moves,
        },
        "mine": mine,
        "divergence": flag_divergence(mine["my_moves"], community_moves),
    }

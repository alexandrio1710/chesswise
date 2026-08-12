"""
Advanced features, Section 8 — Clock Management Analysis.

Time-per-move is derived on the fly from clock_seconds_remaining (stored
per move since Stage 3/Section 1) and each game's own TimeControl PGN
header (base+increment) — no schema change needed, the same "it's already
in the PGN we store" situation as Section 7's ratings.
"""

import re

from db import get_connection

_TIME_CONTROL_RE = re.compile(r'\[TimeControl "(\d+)(?:\+(\d+))?"\]')

_TIER_ORDER = ["best", "excellent", "good", "inaccuracy", "mistake", "blunder"]


def _source_clause(source: str | None, alias: str = "g") -> tuple[str, tuple]:
    if source:
        return f" AND {alias}.source = ?", (source,)
    return "", ()


def parse_time_control(pgn_text: str) -> tuple[int, int] | None:
    """(base_seconds, increment_seconds) from a game's TimeControl PGN
    header, or None for correspondence/untimed games (header is "-" or
    missing entirely).
    """
    m = _TIME_CONTROL_RE.search(pgn_text)
    if not m:
        return None
    base = int(m.group(1))
    increment = int(m.group(2)) if m.group(2) else 0
    return base, increment


def annotate_time_spent(moves: list[dict], pgn_text: str) -> list[dict]:
    """Copy of `moves` (ply order, each with color_moved and
    clock_seconds_remaining) with a "time_spent_seconds" key added: how
    long that move took, from the change in that color's own clock
    reading since their last move (previous reading, plus increment,
    minus the reading stamped on this move). Each color's first move is
    measured against the game's starting time control.

    "time_spent_seconds" is None per-move where it can't be computed (no
    TimeControl header, or a missing clock reading). Clamped at 0 — small
    negative values happen occasionally from server-side lag compensation
    and aren't meaningful to show as "negative thinking time".
    """
    result = [dict(m) for m in moves]
    tc = parse_time_control(pgn_text)
    if tc is None:
        for m in result:
            m["time_spent_seconds"] = None
        return result

    base, increment = tc
    prev_remaining = {"white": base, "black": base}
    for m in result:
        color = m["color_moved"]
        remaining = m.get("clock_seconds_remaining")
        if remaining is None:
            m["time_spent_seconds"] = None
            continue
        spent = prev_remaining[color] + increment - remaining
        m["time_spent_seconds"] = max(0, spent)
        prev_remaining[color] = remaining
    return result


def avg_thinking_time_by_tier(source: str | None = None) -> dict:
    """Average seconds spent per move, grouped by that move's own quality
    tier — only the player's own moves (not the opponent's), across every
    analyzed game with usable clock + TimeControl data. This reports
    whatever the numbers actually turn out to be; there's no assumption
    baked in that mistakes happen faster (or slower) than good moves.
    """
    where, params = _source_clause(source)
    conn = get_connection()
    try:
        games = conn.execute(
            f"SELECT id, color, pgn FROM games g WHERE analyzed = 1 AND skip_reason IS NULL {where}",
            params,
        ).fetchall()

        buckets: dict[str, list[int]] = {tier: [] for tier in _TIER_ORDER}
        for g in games:
            move_rows = conn.execute(
                "SELECT color_moved, tier, clock_seconds_remaining FROM game_moves "
                "WHERE game_id = ? ORDER BY ply",
                (g["id"],),
            ).fetchall()
            annotated = annotate_time_spent([dict(r) for r in move_rows], g["pgn"])
            for m in annotated:
                if m["color_moved"] != g["color"]:
                    continue
                if m["tier"] is None or m["time_spent_seconds"] is None:
                    continue
                buckets.setdefault(m["tier"], []).append(m["time_spent_seconds"])
    finally:
        conn.close()

    return {
        tier: {
            "count": len(times),
            "avg_seconds": round(sum(times) / len(times), 1) if times else None,
        }
        for tier, times in buckets.items()
    }


def clock_pressure_games(source: str | None = None, min_diff_seconds: int = 30) -> dict:
    """Games where the final clock reading suggests a clock edge that
    didn't translate into a win, or a disadvantage overcome anyway.
    Compares each side's LAST recorded clock reading in the game (time
    pressure at the end) rather than total time used across the game, so
    a slow start with a fast finish doesn't get confused with the reverse.

    Only flags a difference of at least `min_diff_seconds` — small gaps
    are noise, not a real "advantage".
    """
    where, params = _source_clause(source)
    conn = get_connection()
    try:
        games = conn.execute(
            f"SELECT id, date, opponent, result, color, time_control FROM games g "
            f"WHERE analyzed = 1 AND skip_reason IS NULL {where}",
            params,
        ).fetchall()

        flagged = []
        games_with_clock = 0
        for g in games:
            rows = conn.execute(
                "SELECT color_moved, clock_seconds_remaining FROM game_moves "
                "WHERE game_id = ? ORDER BY ply",
                (g["id"],),
            ).fetchall()
            white_final = next(
                (r["clock_seconds_remaining"] for r in reversed(rows)
                 if r["color_moved"] == "white" and r["clock_seconds_remaining"] is not None),
                None,
            )
            black_final = next(
                (r["clock_seconds_remaining"] for r in reversed(rows)
                 if r["color_moved"] == "black" and r["clock_seconds_remaining"] is not None),
                None,
            )
            if white_final is None or black_final is None:
                continue
            games_with_clock += 1

            player_final = white_final if g["color"] == "white" else black_final
            opponent_final = black_final if g["color"] == "white" else white_final
            diff = player_final - opponent_final

            pattern = None
            if diff >= min_diff_seconds and g["result"] == "loss":
                pattern = "time_advantage_but_lost"
            elif diff <= -min_diff_seconds and g["result"] == "win":
                pattern = "won_despite_time_disadvantage"

            if pattern:
                flagged.append({
                    "game_id": g["id"], "date": g["date"], "opponent": g["opponent"],
                    "result": g["result"], "time_control": g["time_control"],
                    "player_final_clock_seconds": player_final,
                    "opponent_final_clock_seconds": opponent_final,
                    "clock_diff_seconds": diff, "pattern": pattern,
                })
    finally:
        conn.close()

    flagged.sort(key=lambda f: f["date"], reverse=True)
    return {"games_with_clock_data": games_with_clock, "flagged": flagged}

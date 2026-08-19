"""
Stage 6 — Aggregate stats across all analyzed games.

Every function accepts an optional `source` filter ('lichess' | 'chesscom'
| None for both) so the dashboard (Stage 7) can toggle between them without
duplicating query logic.
"""

import json
import math
import re
from datetime import datetime, timedelta

from db import get_connection


def _source_clause(source: str | None, profile_id: int | None = None, table_alias: str = "g") -> tuple[str, tuple]:
    """Combined source + profile (Section 9) filter as a SQL fragment
    ready to inline after a WHERE clause. Kept as one helper (rather than
    two separate ones threaded independently everywhere) since every
    caller already wants both applied together.
    """
    clauses, params = [], []
    if source:
        clauses.append(f"{table_alias}.source = ?")
        params.append(source)
    if profile_id is not None:
        clauses.append(f"{table_alias}.profile_id = ?")
        params.append(profile_id)
    if not clauses:
        return "", ()
    return " AND " + " AND ".join(clauses), tuple(params)


def mistakes_by_phase(source: str | None = None, profile_id: int | None = None) -> dict:
    where, params = _source_clause(source, profile_id)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT phase, COUNT(*) as n
            FROM mistakes m JOIN games g ON m.game_id = g.id
            WHERE 1=1 {where}
            GROUP BY phase
            """,
            params,
        ).fetchall()
        return {row["phase"]: row["n"] for row in rows}
    finally:
        conn.close()


def mistakes_by_severity(source: str | None = None, profile_id: int | None = None) -> dict:
    where, params = _source_clause(source, profile_id)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT severity, COUNT(*) as n
            FROM mistakes m JOIN games g ON m.game_id = g.id
            WHERE 1=1 {where}
            GROUP BY severity
            """,
            params,
        ).fetchall()
        return {row["severity"]: row["n"] for row in rows}
    finally:
        conn.close()


def blunders_by_phase(source: str | None = None, profile_id: int | None = None) -> dict:
    where, params = _source_clause(source, profile_id)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT phase, COUNT(*) as n
            FROM mistakes m JOIN games g ON m.game_id = g.id
            WHERE severity = 'blunder' {where}
            GROUP BY phase
            """,
            params,
        ).fetchall()
        return {row["phase"]: row["n"] for row in rows}
    finally:
        conn.close()


def worst_mistake_phase(source: str | None = None, profile_id: int | None = None) -> str | None:
    """Which game phase has the most puzzle-eligible mistakes (severity
    'mistake' or 'blunder') — used to prioritize the "Practice my mistakes"
    puzzle queue (Stage B) toward the player's biggest actual leak.
    """
    where, params = _source_clause(source, profile_id)
    conn = get_connection()
    try:
        row = conn.execute(
            f"""
            SELECT phase, COUNT(*) as n
            FROM mistakes m JOIN games g ON m.game_id = g.id
            WHERE severity IN ('mistake', 'blunder') {where}
            GROUP BY phase
            ORDER BY n DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        return row["phase"] if row else None
    finally:
        conn.close()


def clock_correlation(source: str | None = None, profile_id: int | None = None) -> dict:
    """Average clock time remaining for blunders vs. everything else, to
    check whether blunders cluster under time pressure. Only considers
    moves where a clock value was actually captured from the PGN.
    """
    where, params = _source_clause(source, profile_id)
    conn = get_connection()
    try:
        blunder_avg = conn.execute(
            f"""
            SELECT AVG(clock_seconds_remaining) as avg_clock, COUNT(*) as n
            FROM mistakes m JOIN games g ON m.game_id = g.id
            WHERE severity = 'blunder' AND clock_seconds_remaining IS NOT NULL {where}
            """,
            params,
        ).fetchone()
        non_blunder_avg = conn.execute(
            f"""
            SELECT AVG(clock_seconds_remaining) as avg_clock, COUNT(*) as n
            FROM mistakes m JOIN games g ON m.game_id = g.id
            WHERE severity != 'blunder' AND clock_seconds_remaining IS NOT NULL {where}
            """,
            params,
        ).fetchone()
        return {
            "avg_clock_seconds_blunders": blunder_avg["avg_clock"],
            "blunder_sample_size": blunder_avg["n"],
            "avg_clock_seconds_non_blunders": non_blunder_avg["avg_clock"],
            "non_blunder_sample_size": non_blunder_avg["n"],
        }
    finally:
        conn.close()


def worst_game(source: str | None = None, profile_id: int | None = None) -> dict | None:
    """The game containing the single largest eval swing (biggest
    individual mistake), i.e. the "worst blunder" game.
    """
    where, params = _source_clause(source, profile_id)
    conn = get_connection()
    try:
        row = conn.execute(
            f"""
            SELECT g.id as game_id, g.source, g.date, g.opponent, g.color,
                   g.result, g.time_control, g.opening_name,
                   m.move_number, m.move_san, m.phase, m.severity, m.eval_drop
            FROM mistakes m JOIN games g ON m.game_id = g.id
            WHERE 1=1 {where}
            ORDER BY m.eval_drop DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def worst_games(source: str | None = None, profile_id: int | None = None, limit: int = 5) -> list[dict]:
    """The `limit` games with the largest single eval swing, one row each,
    for a dashboard table.
    """
    where, params = _source_clause(source, profile_id)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT g.id as game_id, g.source, g.date, g.opponent, g.color,
                   g.result, g.time_control, g.opening_name,
                   MAX(m.eval_drop) as worst_eval_drop
            FROM mistakes m JOIN games g ON m.game_id = g.id
            WHERE 1=1 {where}
            GROUP BY g.id
            ORDER BY worst_eval_drop DESC
            LIMIT ?
            """,
            params + (limit,),
        ).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            worst_move = conn.execute(
                """
                SELECT move_number, move_san, phase, severity, eval_drop
                FROM mistakes
                WHERE game_id = ? AND eval_drop = ?
                LIMIT 1
                """,
                (d["game_id"], d["worst_eval_drop"]),
            ).fetchone()
            d["worst_move"] = dict(worst_move) if worst_move else None
            results.append(d)
        return results
    finally:
        conn.close()


def overall_summary(source: str | None = None, profile_id: int | None = None) -> dict:
    where, params = _source_clause(source, profile_id)
    where_analyzed, params_a = _source_clause(source, profile_id)
    conn = get_connection()
    try:
        games_row = conn.execute(
            f"SELECT COUNT(*) as n FROM games g WHERE g.analyzed = 1 AND g.skip_reason IS NULL {where_analyzed}",
            params_a,
        ).fetchone()
        mistakes_row = conn.execute(
            f"""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN severity = 'blunder' THEN 1 ELSE 0 END) as blunders
            FROM mistakes m JOIN games g ON m.game_id = g.id
            WHERE 1=1 {where}
            """,
            params,
        ).fetchone()

        total_games = games_row["n"]
        total_mistakes = mistakes_row["total"] or 0
        total_blunders = mistakes_row["blunders"] or 0

        return {
            "total_games_analyzed": total_games,
            "total_mistakes": total_mistakes,
            "total_blunders": total_blunders,
            # Explicitly named by denominator so it's never ambiguous which
            # "rate" is meant — this project has three plausible ones
            # (per game, per move, per flagged mistake) and they read very
            # differently, so every stat here spells out its own unit.
            "blunders_pct_of_mistakes": round(total_blunders / total_mistakes * 100, 1) if total_mistakes else 0.0,
            "avg_mistakes_per_game": round(total_mistakes / total_games, 1) if total_games else 0.0,
            "avg_blunders_per_game": round(total_blunders / total_games, 1) if total_games else 0.0,
        }
    finally:
        conn.close()


def top_takeaway(source: str | None = None, profile_id: int | None = None) -> str:
    """Plain-English sentence naming whichever phase blunders cluster in
    most heavily, e.g. '62% of your blunders happen in the endgame.'
    """
    phase_counts = blunders_by_phase(source, profile_id)
    total_blunders = sum(phase_counts.values())

    if total_blunders == 0:
        return "No blunders found in your analyzed games yet — nice!"

    dominant_phase, count = max(phase_counts.items(), key=lambda kv: kv[1])
    pct = round(count / total_blunders * 100)
    return f"{pct}% of your blunders happen in the {dominant_phase}."


def monthly_trend(source: str | None = None, profile_id: int | None = None, n_months: int = 6) -> list[dict]:
    """Games played and mistakes/blunders per game, grouped by the calendar
    month the game was PLAYED (not when it was analyzed) — most recent
    month first. Every rate here is stated per game, not per move or per
    flagged mistake, so it stays comparable across months with different
    game counts.
    """
    where, params = _source_clause(source, profile_id)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT strftime('%Y-%m', g.date) as month,
                   COUNT(DISTINCT g.id) as games,
                   COUNT(m.id) as mistakes,
                   SUM(CASE WHEN m.severity = 'blunder' THEN 1 ELSE 0 END) as blunders
            FROM games g
            LEFT JOIN mistakes m ON m.game_id = g.id
            WHERE g.analyzed = 1 AND g.skip_reason IS NULL {where}
            GROUP BY month
            ORDER BY month DESC
            LIMIT ?
            """,
            params + (n_months,),
        ).fetchall()

        trend = []
        for r in rows:
            games = r["games"]
            blunders = r["blunders"] or 0
            mistakes = r["mistakes"] or 0
            trend.append({
                "month": r["month"],
                "games": games,
                "mistakes": mistakes,
                "blunders": blunders,
                "blunders_per_game": round(blunders / games, 2) if games else 0.0,
                "mistakes_per_game": round(mistakes / games, 2) if games else 0.0,
            })
        return trend
    finally:
        conn.close()


def _is_immediately_preceding_month(month_label: str, candidate_label: str) -> bool:
    """True if `candidate_label` ("YYYY-MM") is the calendar month right
    before `month_label`. monthly_trend skips months with zero analyzed
    games rather than zero-filling them, so trend[1] isn't guaranteed to
    be adjacent to trend[0] — a gap (games in July, then May, with no
    analyzed games in June) would otherwise get mislabeled "last month".
    """
    this_dt = datetime.strptime(month_label, "%Y-%m")
    candidate_dt = datetime.strptime(candidate_label, "%Y-%m")
    preceding = (this_dt.replace(day=1) - timedelta(days=1)).replace(day=1)
    return (candidate_dt.year, candidate_dt.month) == (preceding.year, preceding.month)


def trend_takeaway(source: str | None = None, profile_id: int | None = None) -> str:
    """Plain-English month-over-month comparison, e.g. 'Blunders per game
    are down from 3.5 last month to 2.1 this month.' Needs at least two
    months with analyzed games to say anything.
    """
    trend = monthly_trend(source, profile_id, n_months=2)
    if len(trend) < 2:
        return "Not enough months of analyzed games yet to show a trend."

    this_month, previous_month = trend[0], trend[1]
    this_rate = this_month["blunders_per_game"]
    previous_rate = previous_month["blunders_per_game"]

    period_label = (
        "last month" if _is_immediately_preceding_month(this_month["month"], previous_month["month"])
        else f"in {previous_month['month']}"
    )

    if this_rate < previous_rate:
        direction = "down"
    elif this_rate > previous_rate:
        direction = "up"
    else:
        return f"Blunders per game are unchanged at {this_rate} this month vs. {period_label}."

    return (
        f"Blunders per game are {direction} from {previous_rate} {period_label} "
        f"to {this_rate} this month."
    )


# --- Stage D: opening analysis ---------------------------------------------

def opening_family(opening_name: str) -> str:
    """Collapse a specific opening/variation name down to its top-level
    family, for grouping in the openings summary view (e.g. "Sicilian
    Defense: Accelerated Dragon" -> "Sicilian Defense").

    Lichess names always use a clean "Family: Variation" format, so we
    split on the colon and that's exact.

    Chess.com names have no such delimiter — fetchers.py derives them from
    Chess.com's ECOUrl slug, which trails off into a literal move list
    (e.g. "Modern Defense with 1 d4 2.Bf4 Bg7 3.e3"). Truncating at the
    first digit strips that move list, which is enough to correctly
    de-duplicate short family names ("Italian Game", "Dutch Defense",
    "London System" all collapse cleanly against real data). It does NOT
    reliably strip a named sub-variation with no digit in it (e.g.
    "Sicilian Defense Nyezhmetdinov Rossolimo Attack" stays as one string,
    rather than splitting into "Sicilian Defense") — doing that properly
    would need a real ECO opening database to know where the family name
    ends, which is out of scope here. Good enough for a summary view, not
    guaranteed precise for every Chess.com game.
    """
    if not opening_name:
        return "Unknown"
    if ":" in opening_name:
        return opening_name.split(":", 1)[0].strip()

    digit_match = re.search(r"\d", opening_name)
    truncated = opening_name[:digit_match.start()] if digit_match else opening_name
    truncated = re.sub(r"\.+$", "", truncated).strip()
    truncated = re.sub(r"\s+with$", "", truncated, flags=re.IGNORECASE).strip()
    return truncated or opening_name.strip()


def opening_stats(source: str | None = None, profile_id: int | None = None) -> list[dict]:
    """Per-specific-opening stats (not yet grouped to family): games
    played, win rate, and how many opening-phase mistakes (mistakes table,
    phase='opening') happened in games that used it.
    """
    where, params = _source_clause(source, profile_id)
    conn = get_connection()
    try:
        game_rows = conn.execute(
            f"""
            SELECT g.opening_name,
                   COUNT(*) as games_played,
                   SUM(CASE WHEN g.result = 'win' THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN g.result = 'loss' THEN 1 ELSE 0 END) as losses,
                   SUM(CASE WHEN g.result = 'draw' THEN 1 ELSE 0 END) as draws
            FROM games g
            WHERE g.analyzed = 1 AND g.skip_reason IS NULL AND g.opening_name != '' {where}
            GROUP BY g.opening_name
            """,
            params,
        ).fetchall()

        mistake_rows = conn.execute(
            f"""
            SELECT g.opening_name, COUNT(*) as opening_mistakes
            FROM mistakes m JOIN games g ON m.game_id = g.id
            WHERE m.phase = 'opening' AND g.opening_name != '' {where}
            GROUP BY g.opening_name
            """,
            params,
        ).fetchall()
        # Same opening_name != '' filter as game_rows above. Without it,
        # this query still produces a correct per-opening breakdown (SQL's
        # GROUP BY already keeps '' as its own bucket, so it can't corrupt
        # any real opening's count) — but it wastefully computes
        # mistake_map[''] for a key game_rows never looks up (game_rows
        # already excludes opening_name == ''), and leaves the two
        # queries' WHERE clauses looking like they filter "the same
        # population" when they didn't. Aligning them is a consistency
        # fix, not a behavior change to what's returned today.
        mistake_map = {r["opening_name"]: r["opening_mistakes"] for r in mistake_rows}

        results = []
        for r in game_rows:
            games = r["games_played"]
            wins = r["wins"] or 0
            opening_mistakes = mistake_map.get(r["opening_name"], 0)
            results.append({
                "opening_name": r["opening_name"],
                "family": opening_family(r["opening_name"]),
                "games_played": games,
                "wins": wins,
                "losses": r["losses"] or 0,
                "draws": r["draws"] or 0,
                "win_rate_pct": round(wins / games * 100, 1) if games else 0.0,
                "opening_phase_mistakes": opening_mistakes,
                "opening_mistakes_per_game": round(opening_mistakes / games, 2) if games else 0.0,
            })
        return results
    finally:
        conn.close()


def opening_family_stats(source: str | None = None, profile_id: int | None = None) -> list[dict]:
    """Per-specific-opening stats rolled up to top-level family, for the
    summary view. Same fields as opening_stats() minus opening_name.
    """
    families: dict[str, dict] = {}
    for d in opening_stats(source, profile_id):
        fam = families.setdefault(d["family"], {
            "family": d["family"], "games_played": 0, "wins": 0,
            "losses": 0, "draws": 0, "opening_phase_mistakes": 0,
        })
        fam["games_played"] += d["games_played"]
        fam["wins"] += d["wins"]
        fam["losses"] += d["losses"]
        fam["draws"] += d["draws"]
        fam["opening_phase_mistakes"] += d["opening_phase_mistakes"]

    results = []
    for fam in families.values():
        games = fam["games_played"]
        fam["win_rate_pct"] = round(fam["wins"] / games * 100, 1) if games else 0.0
        fam["opening_mistakes_per_game"] = round(fam["opening_phase_mistakes"] / games, 2) if games else 0.0
        results.append(fam)
    return results


def most_played_openings(source: str | None = None, profile_id: int | None = None, limit: int = 5) -> list[dict]:
    return sorted(opening_family_stats(source, profile_id), key=lambda f: f["games_played"], reverse=True)[:limit]


def best_win_rate_openings(source: str | None = None, profile_id: int | None = None, min_games: int = 2, limit: int = 5) -> list[dict]:
    eligible = [f for f in opening_family_stats(source, profile_id) if f["games_played"] >= min_games]
    return sorted(eligible, key=lambda f: f["win_rate_pct"], reverse=True)[:limit]


def openings_to_review(source: str | None = None, profile_id: int | None = None, min_games: int = 2, limit: int = 5) -> list[dict]:
    """Openings worth reviewing: played often enough to matter AND
    error-prone in the opening phase specifically. Ranked by
    games_played × opening_mistakes_per_game (frequency × error rate) so a
    rarely-played opening with one bad game doesn't outrank an opening
    that quietly costs a little every time you play it.
    """
    eligible = [f for f in opening_family_stats(source, profile_id) if f["games_played"] >= min_games]
    scored = [
        {**f, "impact_score": round(f["games_played"] * f["opening_mistakes_per_game"], 1)}
        for f in eligible
    ]
    return sorted(scored, key=lambda f: f["impact_score"], reverse=True)[:limit]


# --- Game analysis view -----------------------------------------------------

def get_game_detail(game_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_game_moves(game_id: int) -> list[dict]:
    """Full per-move eval trace for one game (populated at analysis time —
    see mistakes.analyze_and_store_game): every move's own quality tier
    (best/excellent/good/inaccuracy/mistake/blunder), plus the flagged
    mistake's severity and puzzle id where one exists (severity is
    redundant with tier for flagged moves — kept separately since it's the
    field the rest of the app, puzzles included, already keys off of).
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT gm.ply, gm.move_number, gm.color_moved, gm.move_san,
                   gm.eval_cp, gm.eval_before_cp, gm.eval_drop, gm.tier,
                   gm.classification, gm.clock_seconds_remaining,
                   m.severity, p.id as puzzle_id
            FROM game_moves gm
            LEFT JOIN mistakes m ON m.game_id = gm.game_id AND m.ply = gm.ply
            LEFT JOIN puzzles p ON p.mistake_id = m.id
            WHERE gm.game_id = ?
            ORDER BY gm.ply
            """,
            (game_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ACPL->accuracy decay constant — this project's own calibration (not
# copied from any external source): chosen so ACPL=50 (a solid, mostly-
# clean game) scores ~80%, and ACPL=200 (a rough game with a couple of
# real blunders) scores ~41%. See compute_game_accuracy() docstring.
ACCURACY_DECAY_K = 0.00446


def compute_game_accuracy(moves: list[dict], color: str) -> float | None:
    """0-100 accuracy score for one player's moves in one game, from their
    average centipawn loss (ACPL) via exponential decay:

        accuracy = 100 * e^(-k * ACPL)

    This treats every centipawn of loss as equally costly regardless of
    how winning/losing the position already was, and doesn't correct for
    the engine's own move-to-move evaluation noise — a real simplification,
    not a claim of precision. It's a standard style of scoring used across
    chess analysis tools generally, not any specific site's exact formula
    (this project picked its own k, see ACCURACY_DECAY_K above).

    `moves` is get_game_moves()'s output; only the given `color`'s own
    moves count (the opponent's moves aren't yours to be accurate about).
    Returns None if that color made no moves with a known eval_drop
    (shouldn't happen for a fully analyzed game, but a partial/corrupt
    trace shouldn't crash the caller).
    """
    drops = [m["eval_drop"] for m in moves if m["color_moved"] == color and m["eval_drop"] is not None]
    if not drops:
        return None
    acpl = sum(max(0.0, d) for d in drops) / len(drops)
    accuracy = 100 * math.exp(-ACCURACY_DECAY_K * acpl)
    return round(max(0.0, min(100.0, accuracy)), 1)


def get_critical_moment(game_id: int, game_pgn: str, player_color: str) -> dict | None:
    """The single move with the largest eval swing IN THE PLAYER'S OWN
    moves for this game (their critical moment to learn from — not the
    opponent's, even if the opponent blundered harder). Includes the
    position right before it and what the engine's actual best move was,
    reusing the stored puzzle if one exists (mistake/blunder severity) or
    running one fresh, cheap top-line query if the critical moment was
    only inaccuracy-severity (which doesn't get a puzzle).

    Returns None if the player made no flagged mistakes this game at all.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT m.id as mistake_id, m.ply, m.move_number, m.move_san,
                   m.phase, m.severity, m.eval_drop, p.id as puzzle_id,
                   p.best_move_san, p.best_move_explanation, p.fen_before, p.top_lines
            FROM mistakes m
            LEFT JOIN puzzles p ON p.mistake_id = m.id
            WHERE m.game_id = ? AND m.color_moved = ?
            ORDER BY m.eval_drop DESC
            LIMIT 1
            """,
            (game_id, player_color),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None
    d = dict(row)

    if d["fen_before"]:
        d["top_lines"] = json.loads(d["top_lines"])
        return d

    # No puzzle (inaccuracy-severity critical moment) — get the position
    # and best move fresh. One Stockfish call, not stored, since this is a
    # one-off detail-view lookup rather than something replayed later.
    import chess
    from puzzles import board_before_ply, get_top_lines

    board = board_before_ply(game_pgn, d["ply"])
    d["fen_before"] = board.fen()
    lines = get_top_lines(d["fen_before"])
    if lines:
        d["best_move_san"] = lines[0]["move_san"]
        d["best_move_explanation"] = None
    d["top_lines"] = lines
    d["puzzle_id"] = None
    return d


# --- Notes (Full Game Review, Section 1) ------------------------------------

def add_note(game_id: int, text: str, ply: int | None = None) -> dict:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO notes (game_id, ply, text, created_at) VALUES (?, ?, ?, datetime('now'))",
            (game_id, ply, text),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def get_notes(game_id: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM notes WHERE game_id = ? ORDER BY created_at", (game_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_note(note_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        conn.commit()
    finally:
        conn.close()


# --- Search and filter (Advanced features, Section 6) -----------------------

def search_games(
    opponent: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    opening: str | None = None,
    result: str | None = None,
    time_control: str | None = None,
    source: str | None = None,
    profile_id: int | None = None,
    color: str | None = None,
    has_blunder: bool | None = None,
    sort_by: str = "date",
    sort_dir: str = "desc",
    limit: int = 200,
) -> list[dict]:
    """Combinable filters across the full game database (e.g. "blitz
    games I lost as black in the last 3 months" = time_control='blitz',
    result='loss', color='black', date_from=<3 months ago>). Only
    analyzed, non-variant-skipped games are searchable — an unanalyzed
    game has no mistake data for `has_blunder` to mean anything, and a
    skipped one was never graded at all.
    """
    where = ["g.analyzed = 1", "g.skip_reason IS NULL"]
    params: list = []

    if opponent:
        where.append("g.opponent LIKE ?")
        params.append(f"%{opponent}%")
    if date_from:
        where.append("g.date >= ?")
        params.append(date_from)
    if date_to:
        where.append("g.date <= ?")
        params.append(date_to)
    if opening:
        where.append("g.opening_name LIKE ?")
        params.append(f"%{opening}%")
    if result:
        where.append("g.result = ?")
        params.append(result)
    if time_control:
        where.append("g.time_control = ?")
        params.append(time_control)
    if source:
        where.append("g.source = ?")
        params.append(source)
    if profile_id is not None:
        where.append("g.profile_id = ?")
        params.append(profile_id)
    if color:
        where.append("g.color = ?")
        params.append(color)
    if has_blunder is not None:
        cmp = "IN" if has_blunder else "NOT IN"
        where.append(f"g.id {cmp} (SELECT DISTINCT game_id FROM mistakes WHERE severity = 'blunder')")

    sort_column = {"date": "g.date", "opponent": "g.opponent", "result": "g.result",
                   "mistakes": "mistake_count", "blunders": "blunder_count"}.get(sort_by, "g.date")
    sort_sql = f"{sort_column} {'ASC' if sort_dir == 'asc' else 'DESC'}"

    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT g.id, g.source, g.date, g.opponent, g.result, g.color,
                   g.time_control, g.opening_name,
                   (SELECT COUNT(*) FROM mistakes m WHERE m.game_id = g.id) as mistake_count,
                   (SELECT COUNT(*) FROM mistakes m WHERE m.game_id = g.id AND m.severity = 'blunder') as blunder_count
            FROM games g
            WHERE {" AND ".join(where)}
            ORDER BY {sort_sql}
            LIMIT ?
            """,
            params + [limit],
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def compute_stats_for_game_ids(game_ids: list[int]) -> dict:
    """Headline stats computed for an arbitrary, caller-chosen subset of
    games — powers the Search page's "stats for this filtered view" panel,
    so a filter like "blitz losses as black" gets its own mini dashboard
    rather than only a list of games (Section 6's "feed into the stats
    dashboard" requirement), without threading a full ad-hoc filter set
    through every existing stats function.
    """
    if not game_ids:
        return {
            "total_games": 0, "total_mistakes": 0, "total_blunders": 0,
            "avg_blunders_per_game": 0.0, "blunders_pct_of_mistakes": 0.0,
            "by_phase": {}, "by_severity": {},
        }

    placeholders = ",".join("?" * len(game_ids))
    conn = get_connection()
    try:
        totals = conn.execute(
            f"SELECT COUNT(*) as total, SUM(CASE WHEN severity = 'blunder' THEN 1 ELSE 0 END) as blunders "
            f"FROM mistakes WHERE game_id IN ({placeholders})",
            game_ids,
        ).fetchone()
        phase_rows = conn.execute(
            f"SELECT phase, COUNT(*) as n FROM mistakes WHERE game_id IN ({placeholders}) GROUP BY phase",
            game_ids,
        ).fetchall()
        severity_rows = conn.execute(
            f"SELECT severity, COUNT(*) as n FROM mistakes WHERE game_id IN ({placeholders}) GROUP BY severity",
            game_ids,
        ).fetchall()
    finally:
        conn.close()

    total_games = len(game_ids)
    total_mistakes = totals["total"] or 0
    total_blunders = totals["blunders"] or 0

    return {
        "total_games": total_games,
        "total_mistakes": total_mistakes,
        "total_blunders": total_blunders,
        "avg_blunders_per_game": round(total_blunders / total_games, 1) if total_games else 0.0,
        "blunders_pct_of_mistakes": round(total_blunders / total_mistakes * 100, 1) if total_mistakes else 0.0,
        "by_phase": {row["phase"]: row["n"] for row in phase_rows},
        "by_severity": {row["severity"]: row["n"] for row in severity_rows},
    }


if __name__ == "__main__":
    import sys

    source = sys.argv[1] if len(sys.argv) > 1 else None
    label = source or "All sources"
    print(f"=== Stats for: {label} ===\n")

    print("Top takeaway:")
    print(f"  {top_takeaway(source)}\n")

    print("Overall summary:")
    for k, v in overall_summary(source).items():
        print(f"  {k}: {v}")

    print("\nMistakes by phase:")
    for phase, n in mistakes_by_phase(source).items():
        print(f"  {phase}: {n}")

    print("\nMistakes by severity:")
    for sev, n in mistakes_by_severity(source).items():
        print(f"  {sev}: {n}")

    print("\nClock correlation:")
    cc = clock_correlation(source)
    print(f"  Blunders: avg {cc['avg_clock_seconds_blunders']:.0f}s remaining (n={cc['blunder_sample_size']})"
          if cc["avg_clock_seconds_blunders"] is not None else "  Blunders: no clock data")
    print(f"  Non-blunders: avg {cc['avg_clock_seconds_non_blunders']:.0f}s remaining (n={cc['non_blunder_sample_size']})"
          if cc["avg_clock_seconds_non_blunders"] is not None else "  Non-blunders: no clock data")

    print("\nWorst single blunder (biggest eval swing):")
    wg = worst_game(source)
    if wg:
        print(f"  {wg['source']} | {wg['date']} | {wg['color']} vs {wg['opponent']} | "
              f"move {wg['move_number']} {wg['move_san']} ({wg['phase']}) "
              f"dropped {wg['eval_drop']:.0f}cp")

    print("\nTop 5 worst games:")
    for g in worst_games(source, limit=5):
        wm = g["worst_move"]
        print(f"  {g['source']} | {g['date']} | vs {g['opponent']} | "
              f"worst: move {wm['move_number']} {wm['move_san']} dropped {wm['eval_drop']:.0f}cp")

    print("\nMonthly trend:")
    print(f"  {trend_takeaway(source)}")
    for m in monthly_trend(source):
        print(f"  {m['month']}: {m['games']} games, "
              f"{m['blunders_per_game']} blunders/game, {m['mistakes_per_game']} mistakes/game")

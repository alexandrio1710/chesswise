"""
Advanced features, Section 7 — Advanced Stats and Insights Dashboard.

Every function here accepts the same optional `source` filter as stats.py.
Depends on player_rating/opponent_rating (migration 6 + backfill_ratings.py)
and the full per-move eval trace (game_moves, from Section 1) — both
already in place before this module was written, per the project's own
rule about extending storage before building the feature that needs it.
"""

from db import get_connection


def _source_clause(source: str | None, profile_id: int | None = None, alias: str = "g") -> tuple[str, tuple]:
    clauses, params = [], []
    if source:
        clauses.append(f"{alias}.source = ?")
        params.append(source)
    if profile_id is not None:
        clauses.append(f"{alias}.profile_id = ?")
        params.append(profile_id)
    if not clauses:
        return "", ()
    return " AND " + " AND ".join(clauses), tuple(params)


def rating_progress(source: str | None = None, profile_id: int | None = None) -> list[dict]:
    """Chronological player_rating per game (skipping games with no
    rating data — a handful of very old or unusual games might lack it),
    for a progress-over-time chart.
    """
    where, params = _source_clause(source, profile_id)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT date, source, player_rating, opponent_rating, result
            FROM games g
            WHERE analyzed = 1 AND skip_reason IS NULL AND player_rating IS NOT NULL {where}
            ORDER BY date ASC
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def win_rate_by_color(source: str | None = None, profile_id: int | None = None) -> dict:
    where, params = _source_clause(source, profile_id)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT color,
                   COUNT(*) as games,
                   SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) as wins
            FROM games g
            WHERE analyzed = 1 AND skip_reason IS NULL {where}
            GROUP BY color
            """,
            params,
        ).fetchall()
        return {
            r["color"]: {
                "games": r["games"], "wins": r["wins"],
                "win_rate_pct": round(r["wins"] / r["games"] * 100, 1) if r["games"] else 0.0,
            }
            for r in rows
        }
    finally:
        conn.close()


def win_rate_by_time_control(source: str | None = None, profile_id: int | None = None) -> dict:
    where, params = _source_clause(source, profile_id)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT time_control,
                   COUNT(*) as games,
                   SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) as wins
            FROM games g
            WHERE analyzed = 1 AND skip_reason IS NULL {where}
            GROUP BY time_control
            """,
            params,
        ).fetchall()
        return {
            r["time_control"]: {
                "games": r["games"], "wins": r["wins"],
                "win_rate_pct": round(r["wins"] / r["games"] * 100, 1) if r["games"] else 0.0,
            }
            for r in rows
        }
    finally:
        conn.close()


_DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def win_rate_by_day_of_week(source: str | None = None, profile_id: int | None = None) -> dict:
    """Keyed by day name. Based on each game's stored UTC date/time — if
    you play mostly around midnight UTC, a game can land on a different
    calendar day than it felt like locally. Stated here rather than
    silently presented as local-time fact.
    """
    where, params = _source_clause(source, profile_id)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT CAST(strftime('%w', date) as INTEGER) as dow,
                   COUNT(*) as games,
                   SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) as wins
            FROM games g
            WHERE analyzed = 1 AND skip_reason IS NULL AND strftime('%w', date) IS NOT NULL {where}
            GROUP BY dow
            """,
            params,
        ).fetchall()
        # The strftime(...) IS NOT NULL clause above excludes any game
        # whose stored `date` isn't a format SQLite's date functions can
        # parse (pre-existing data from before fetchers.py always
        # normalized to ISO) — without it, `dow` is None for such a row
        # and _DAY_NAMES[None] below raises TypeError, 500ing this whole
        # dashboard over one bad game rather than just omitting it.
        return {
            _DAY_NAMES[r["dow"]]: {
                "games": r["games"], "wins": r["wins"],
                "win_rate_pct": round(r["wins"] / r["games"] * 100, 1) if r["games"] else 0.0,
            }
            for r in rows
        }
    finally:
        conn.close()


_TIME_BUCKETS = [
    (0, 6, "Night (00-06 UTC)"), (6, 12, "Morning (06-12 UTC)"),
    (12, 18, "Afternoon (12-18 UTC)"), (18, 24, "Evening (18-24 UTC)"),
]


def win_rate_by_time_of_day(source: str | None = None, profile_id: int | None = None) -> dict:
    """Four 6-hour UTC buckets — same caveat as win_rate_by_day_of_week:
    this is UTC time, not necessarily local time, since that's what's
    actually stored.
    """
    where, params = _source_clause(source, profile_id)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT CAST(strftime('%H', date) as INTEGER) as hour,
                   result
            FROM games g
            WHERE analyzed = 1 AND skip_reason IS NULL AND strftime('%H', date) IS NOT NULL {where}
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    # Same reasoning as win_rate_by_day_of_week's IS NOT NULL clause: a
    # game whose `date` isn't parseable by SQLite's date functions would
    # otherwise make `hour` None here, and `start <= None < end` raises
    # TypeError — excluded at the query level rather than crashing.
    buckets = {label: {"games": 0, "wins": 0} for _, _, label in _TIME_BUCKETS}
    for r in rows:
        for start, end, label in _TIME_BUCKETS:
            if start <= r["hour"] < end:
                buckets[label]["games"] += 1
                if r["result"] == "win":
                    buckets[label]["wins"] += 1
                break

    for b in buckets.values():
        b["win_rate_pct"] = round(b["wins"] / b["games"] * 100, 1) if b["games"] else 0.0
    return buckets


def avg_game_length_wins_vs_losses(source: str | None = None, profile_id: int | None = None) -> dict:
    """Average total ply count (from game_moves) for won vs. lost games —
    draws excluded since they're a third, differently-shaped category
    rather than fitting on the same win/loss axis.
    """
    where, params = _source_clause(source, profile_id)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT g.result, AVG(move_counts.n) as avg_plies, COUNT(*) as games
            FROM games g
            JOIN (SELECT game_id, COUNT(*) as n FROM game_moves GROUP BY game_id) move_counts
                ON move_counts.game_id = g.id
            WHERE g.analyzed = 1 AND g.skip_reason IS NULL AND g.result IN ('win', 'loss') {where}
            GROUP BY g.result
            """,
            params,
        ).fetchall()
        return {
            r["result"]: {"avg_moves": round(r["avg_plies"] / 2, 1), "games": r["games"]}
            for r in rows
        }
    finally:
        conn.close()


_RATING_BANDS = [
    (-100000, -200, "200+ pts lower rated"),
    (-200, -50, "50-200 pts lower rated"),
    (-50, 50, "similarly rated (within 50 pts)"),
    (50, 200, "50-200 pts higher rated"),
    (200, 100000, "200+ pts higher rated"),
]


def performance_vs_rating_band(source: str | None = None, profile_id: int | None = None) -> list[dict]:
    """Win rate bucketed by opponent_rating - player_rating, to answer
    "do I do better or worse than expected against higher/lower rated
    opponents" directly, rather than leaving it to eyeballing a scatter.
    """
    where, params = _source_clause(source, profile_id)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT result, (opponent_rating - player_rating) as diff
            FROM games g
            WHERE analyzed = 1 AND skip_reason IS NULL
              AND player_rating IS NOT NULL AND opponent_rating IS NOT NULL {where}
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    bands = [{"label": label, "games": 0, "wins": 0, "draws": 0, "losses": 0} for _, _, label in _RATING_BANDS]
    for r in rows:
        for i, (lo, hi, _) in enumerate(_RATING_BANDS):
            if lo <= r["diff"] < hi:
                bands[i]["games"] += 1
                bands[i][r["result"] + "s" if r["result"] != "loss" else "losses"] += 1
                break

    for b in bands:
        b["win_rate_pct"] = round(b["wins"] / b["games"] * 100, 1) if b["games"] else None
    return [b for b in bands if b["games"] > 0]


def comeback_rate(source: str | None = None, profile_id: int | None = None, behind_threshold_cp: int = -300) -> dict:
    """Games where the player was significantly behind at some point
    (their own eval, not the opponent's, dropped to `behind_threshold_cp`
    or worse) but still won — a real fighting-spirit stat, computed from
    the actual per-move eval trace rather than inferred from the result
    alone.
    """
    where, params = _source_clause(source, profile_id)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT g.result,
                   MIN(CASE WHEN g.color = 'white' THEN gm.eval_cp ELSE -gm.eval_cp END) as worst_eval_for_player
            FROM games g JOIN game_moves gm ON gm.game_id = g.id
            WHERE g.analyzed = 1 AND g.skip_reason IS NULL {where}
            GROUP BY g.id
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    was_behind = [r for r in rows if r["worst_eval_for_player"] is not None and r["worst_eval_for_player"] <= behind_threshold_cp]
    comebacks = [r for r in was_behind if r["result"] == "win"]

    return {
        "threshold_cp": behind_threshold_cp,
        "games_significantly_behind": len(was_behind),
        "comebacks_won": len(comebacks),
        "comeback_rate_pct": round(len(comebacks) / len(was_behind) * 100, 1) if was_behind else None,
    }


def top_insights(source: str | None = None, profile_id: int | None = None, n: int = 5) -> list[dict]:
    """The most notable findings across every stat above, as plain-English
    sentences — ranked by how far each finding deviates from a 50%/neutral
    baseline, weighted by sample size (a 70% win rate over 3 games isn't
    as notable as 70% over 30). Genuinely data-driven: nothing here is a
    fixed list of "the 5 things we always show" — a filter/profile with
    different data will surface different insights.
    """
    import math

    candidates: list[tuple[float, str]] = []

    color = win_rate_by_color(source, profile_id)
    for c, d in color.items():
        if d["games"] >= 3:
            deviation = abs(d["win_rate_pct"] - 50)
            score = deviation * math.sqrt(d["games"])
            candidates.append((score, f"You win {d['win_rate_pct']}% of games as {c} ({d['games']} games)."))

    tc = win_rate_by_time_control(source, profile_id)
    for t, d in tc.items():
        if d["games"] >= 3:
            deviation = abs(d["win_rate_pct"] - 50)
            score = deviation * math.sqrt(d["games"]) * 0.9  # slightly below color/rating-band framing
            candidates.append((score, f"Your win rate in {t} is {d['win_rate_pct']}% ({d['games']} games)."))

    bands = performance_vs_rating_band(source, profile_id)
    for b in bands:
        if b["games"] >= 3 and b["win_rate_pct"] is not None:
            deviation = abs(b["win_rate_pct"] - 50)
            score = deviation * math.sqrt(b["games"]) * 1.1  # rating-band findings tend to be the most actionable
            candidates.append((score, f"Against opponents {b['label']}, your win rate is {b['win_rate_pct']}% ({b['games']} games)."))

    lengths = avg_game_length_wins_vs_losses(source, profile_id)
    if "win" in lengths and "loss" in lengths:
        diff = lengths["win"]["avg_moves"] - lengths["loss"]["avg_moves"]
        if abs(diff) >= 3:
            score = abs(diff) * 2
            longer = "longer" if diff > 0 else "shorter"
            candidates.append((score, f"Your wins average {lengths['win']['avg_moves']} moves — {longer} than your losses ({lengths['loss']['avg_moves']} moves)."))

    cb = comeback_rate(source, profile_id)
    if cb["games_significantly_behind"] >= 3:
        score = 40 + cb["games_significantly_behind"]  # always fairly notable, more so with more data
        candidates.append((score, f"When significantly behind, you still win {cb['comeback_rate_pct']}% of the time ({cb['comebacks_won']}/{cb['games_significantly_behind']} games)."))

    dow = win_rate_by_day_of_week(source, profile_id)
    for day, d in dow.items():
        if d["games"] >= 3:
            deviation = abs(d["win_rate_pct"] - 50)
            score = deviation * math.sqrt(d["games"]) * 0.7  # weaker signal — day-of-week is a weaker prior than color/rating
            candidates.append((score, f"Your win rate on {day}s is {d['win_rate_pct']}% ({d['games']} games)."))

    candidates.sort(key=lambda c: c[0], reverse=True)
    return [{"text": text, "score": round(score, 1)} for score, text in candidates[:n]]

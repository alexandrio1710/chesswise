"""
Advanced features, Section 10 — Progress, Goals, and Auto-Reports.

Weekly summary, simple goal tracking, and a plain-language narrative
that stitches both together with the same top insight Section 7's
dashboard already surfaces — entirely template-based (no LLM calls),
same "own plain-English sentence" style as stats.top_takeaway() and
insights.top_insights().
"""

from datetime import datetime, timedelta, timezone

import insights
import stats
from db import get_connection

METRICS = ("blunder_rate_overall", "blunder_rate_phase", "accuracy_avg", "win_rate_overall")
COMPARISONS = ("below", "above")


def _filter_clause(source: str | None, profile_id: int | None, alias: str = "g") -> tuple[str, list]:
    clauses, params = [], []
    if source:
        clauses.append(f"{alias}.source = ?")
        params.append(source)
    if profile_id is not None:
        clauses.append(f"{alias}.profile_id = ?")
        params.append(profile_id)
    if not clauses:
        return "", []
    return " AND " + " AND ".join(clauses), params


def average_accuracy(source: str | None = None, profile_id: int | None = None,
                      game_ids: list[int] | None = None) -> float | None:
    """Mean per-game accuracy (stats.compute_game_accuracy — same formula
    shown on every game's own page) across analyzed games matching the
    filter, or across an explicit `game_ids` subset (used by
    weekly_summary() to scope to one week without a second query).
    """
    if game_ids is None:
        where, params = _filter_clause(source, profile_id)
        conn = get_connection()
        try:
            game_ids = [r["id"] for r in conn.execute(
                f"SELECT g.id FROM games g WHERE g.analyzed = 1 AND g.skip_reason IS NULL {where}", params
            ).fetchall()]
        finally:
            conn.close()

    accuracies = []
    for gid in game_ids:
        game = stats.get_game_detail(gid)
        if game is None:
            continue
        moves = stats.get_game_moves(gid)
        acc = stats.compute_game_accuracy(moves, game["color"])
        if acc is not None:
            accuracies.append(acc)
    return round(sum(accuracies) / len(accuracies), 1) if accuracies else None


def overall_win_rate(source: str | None = None, profile_id: int | None = None) -> float | None:
    where, params = _filter_clause(source, profile_id)
    conn = get_connection()
    try:
        row = conn.execute(
            f"""
            SELECT COUNT(*) as games, SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) as wins
            FROM games g WHERE g.analyzed = 1 AND g.skip_reason IS NULL {where}
            """,
            params,
        ).fetchone()
    finally:
        conn.close()
    games = row["games"] or 0
    return round((row["wins"] or 0) / games * 100, 1) if games else None


def _iso_week_label(dt: datetime) -> str:
    return dt.strftime("%Y-%W")  # Monday-start week number within the year


def _week_aggregate(week_label: str, source: str | None, profile_id: int | None) -> dict:
    where, params = _filter_clause(source, profile_id)
    conn = get_connection()
    try:
        game_row = conn.execute(
            f"""
            SELECT COUNT(*) as games, SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) as wins
            FROM games g
            WHERE g.analyzed = 1 AND g.skip_reason IS NULL AND strftime('%Y-%W', g.date) = ? {where}
            """,
            [week_label] + params,
        ).fetchone()
        mistake_row = conn.execute(
            f"""
            SELECT COUNT(*) as total, SUM(CASE WHEN m.severity = 'blunder' THEN 1 ELSE 0 END) as blunders
            FROM mistakes m JOIN games g ON m.game_id = g.id
            WHERE strftime('%Y-%W', g.date) = ? {where}
            """,
            [week_label] + params,
        ).fetchone()
        game_ids = [r["id"] for r in conn.execute(
            f"""
            SELECT g.id FROM games g
            WHERE g.analyzed = 1 AND g.skip_reason IS NULL AND strftime('%Y-%W', g.date) = ? {where}
            """,
            [week_label] + params,
        ).fetchall()]
    finally:
        conn.close()

    games = game_row["games"] or 0
    wins = game_row["wins"] or 0
    mistakes = mistake_row["total"] or 0
    blunders = mistake_row["blunders"] or 0

    return {
        "week": week_label,
        "games": games,
        "win_rate_pct": round(wins / games * 100, 1) if games else None,
        "blunders_per_game": round(blunders / games, 2) if games else None,
        "mistakes_per_game": round(mistakes / games, 2) if games else None,
        "avg_accuracy": average_accuracy(game_ids=game_ids) if games else None,
    }


def weekly_summary(source: str | None = None, profile_id: int | None = None) -> dict:
    """This calendar week (Monday-start) vs. last, same shape for both
    sides so the frontend can render them identically side by side.
    """
    now = datetime.now(timezone.utc)
    this_week = _iso_week_label(now)
    last_week = _iso_week_label(now - timedelta(days=7))
    return {
        "this_week": _week_aggregate(this_week, source, profile_id),
        "last_week": _week_aggregate(last_week, source, profile_id),
    }


# --- Goals -------------------------------------------------------------------

def create_goal(description: str, metric: str, comparison: str, target_value: float,
                 phase: str | None = None, source: str | None = None, profile_id: int | None = None) -> dict:
    if metric not in METRICS:
        raise ValueError(f"Unknown metric '{metric}'")
    if comparison not in COMPARISONS:
        raise ValueError("comparison must be 'below' or 'above'")
    conn = get_connection()
    try:
        cur = conn.execute(
            """
            INSERT INTO goals (profile_id, source, metric, phase, comparison, target_value, description, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (profile_id, source, metric, phase, comparison, target_value, description),
        )
        conn.commit()
        goal_id = cur.lastrowid
    finally:
        conn.close()
    return evaluate_goal(_get_goal_row(goal_id))


def _get_goal_row(goal_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_goal(goal_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
        conn.commit()
    finally:
        conn.close()


def _mark_achieved(goal_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE goals SET achieved_at = datetime('now') WHERE id = ? AND achieved_at IS NULL", (goal_id,)
        )
        conn.commit()
    finally:
        conn.close()


def _current_metric_value(goal: dict) -> float | None:
    source, profile_id = goal["source"], goal["profile_id"]
    if goal["metric"] == "blunder_rate_overall":
        return stats.overall_summary(source, profile_id)["blunders_pct_of_mistakes"]
    if goal["metric"] == "blunder_rate_phase":
        mistake_counts = stats.mistakes_by_phase(source, profile_id)
        blunder_counts = stats.blunders_by_phase(source, profile_id)
        m = mistake_counts.get(goal["phase"], 0)
        b = blunder_counts.get(goal["phase"], 0)
        return round(b / m * 100, 1) if m else None
    if goal["metric"] == "accuracy_avg":
        return average_accuracy(source, profile_id)
    if goal["metric"] == "win_rate_overall":
        return overall_win_rate(source, profile_id)
    return None


def evaluate_goal(goal: dict) -> dict:
    """Adds the live current_value/met/progress_pct to a goal row. Marks
    achieved_at the first time `met` comes back true (see the migration's
    docstring for why that's a one-way "first hit" marker).
    """
    current = _current_metric_value(goal)
    met, progress_pct = None, None

    if current is not None:
        target = goal["target_value"]
        if goal["comparison"] == "below":
            met = current < target
            progress_pct = round(max(0.0, min(100.0, (target / current * 100) if current > 0 else 100.0)), 1)
        else:
            met = current > target
            progress_pct = round(max(0.0, min(100.0, (current / target * 100) if target > 0 else 100.0)), 1)

        if met and not goal["achieved_at"]:
            _mark_achieved(goal["id"])
            goal["achieved_at"] = datetime.now(timezone.utc).isoformat()

    goal["current_value"] = current
    goal["met"] = met
    goal["progress_pct"] = progress_pct
    return goal


def list_goals(profile_id: int | None = None) -> list[dict]:
    """All goals, each evaluated against its own stored filter (a goal
    keeps whatever source/profile it was created under, independent of
    whatever's currently selected in the UI) — except profile_id itself
    is used here to decide which goals to SHOW: this profile's own goals
    plus any created with no profile filter at all.
    """
    conn = get_connection()
    try:
        if profile_id is not None:
            rows = conn.execute(
                "SELECT * FROM goals WHERE profile_id = ? OR profile_id IS NULL ORDER BY id DESC", (profile_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM goals ORDER BY id DESC").fetchall()
        goals = [dict(r) for r in rows]
    finally:
        conn.close()
    return [evaluate_goal(g) for g in goals]


# --- Auto-generated narrative --------------------------------------------------

def generate_narrative(source: str | None = None, profile_id: int | None = None) -> str:
    """A short plain-language paragraph combining this week's numbers
    (vs. last week), the goal furthest from done, and the single most
    notable insight (insights.top_insights) — genuinely template-based,
    not an LLM call, in keeping with every other "plain-English sentence"
    already in this app.
    """
    ws = weekly_summary(source, profile_id)
    this, last = ws["this_week"], ws["last_week"]
    parts = []

    if this["games"] == 0:
        parts.append("No games played yet this week.")
    else:
        plural = "s" if this["games"] != 1 else ""
        win_text = f", {this['win_rate_pct']}% wins" if this["win_rate_pct"] is not None else ""
        parts.append(f"This week you played {this['games']} game{plural}{win_text}.")

        if last["games"]:
            diff = this["games"] - last["games"]
            direction = "up" if diff > 0 else "down" if diff < 0 else "the same as"
            parts.append(f"That's {direction} from {last['games']} last week.")

        if this["blunders_per_game"] is not None:
            trend = ""
            if last["blunders_per_game"] is not None:
                if this["blunders_per_game"] < last["blunders_per_game"]:
                    trend = f" (down from {last['blunders_per_game']} last week)"
                elif this["blunders_per_game"] > last["blunders_per_game"]:
                    trend = f" (up from {last['blunders_per_game']} last week)"
            parts.append(f"Blunder rate: {this['blunders_per_game']}/game{trend}.")

        if this["avg_accuracy"] is not None:
            parts.append(f"Average accuracy: {this['avg_accuracy']}%.")

    goals = [g for g in list_goals(profile_id) if g["current_value"] is not None]
    unmet = [g for g in goals if not g["met"]]
    if unmet:
        g = min(unmet, key=lambda x: 100 - (x["progress_pct"] or 0))
        parts.append(
            f'Goal "{g["description"]}": currently {g["current_value"]} '
            f'(target {g["comparison"]} {g["target_value"]}), {g["progress_pct"]}% of the way there.'
        )
    elif goals:
        g = goals[0]
        parts.append(f'Goal "{g["description"]}" is currently met (at {g["current_value"]}).')

    top = insights.top_insights(source, profile_id, n=1)
    if top:
        parts.append(top[0]["text"])

    return " ".join(parts)

"""
Advanced features, Section 10 — immediate per-game Discord alerts.

Extends the existing weekly digest (digest.py, which summarizes a whole
refresh at once) with per-game alerts: right after a fresh batch of
games is analyzed — from a web refresh, `cli.py refresh`, or digest.py's
own refresh — any single game with notably low accuracy or an
especially large blunder gets its own immediate message, rather than
waiting for the next weekly digest to mention it in aggregate.

Reuses stats.py's existing per-game functions (get_game_detail,
get_game_moves, compute_game_accuracy) unchanged — same accuracy
formula and eval-drop values already shown on that game's own page.
"""

import logging

import requests

import config
from stats import compute_game_accuracy, get_game_detail, get_game_moves

logger = logging.getLogger(__name__)


def check_game_for_alert(game_id: int) -> dict | None:
    """None if this game doesn't cross either alert threshold (the common
    case — most games are unremarkable), otherwise the info needed to
    post about it.
    """
    game = get_game_detail(game_id)
    if game is None or not game["analyzed"] or game["skip_reason"]:
        return None

    moves = get_game_moves(game_id)
    accuracy = compute_game_accuracy(moves, game["color"])
    own_drops = [
        m["eval_drop"] for m in moves
        if m["color_moved"] == game["color"] and m["eval_drop"] is not None
    ]
    worst_drop = max(own_drops, default=0.0)

    reasons = []
    if accuracy is not None and accuracy < config.ALERT_ACCURACY_BELOW:
        reasons.append(f"accuracy was only {accuracy}%")
    if worst_drop >= config.ALERT_BLUNDER_EVAL_DROP_CP:
        reasons.append(f"a {worst_drop:.0f}cp blunder")

    if not reasons:
        return None
    return {"game_id": game_id, "game": game, "accuracy": accuracy, "worst_drop": worst_drop, "reasons": reasons}


def format_alert_message(alert: dict) -> str:
    g = alert["game"]
    date = (g["date"] or "")[:10]
    return (
        f"**Rough one** — vs {g['opponent']} ({g['source']}, {date}): {' and '.join(alert['reasons'])}.\n"
        f"Result: {g['result']} as {g['color']} · <{_game_link(g)}>"
    )


def _game_link(game: dict) -> str:
    return f"{config.BASE_URL}/game?id={game['id']}"


def send_alerts_for_games(game_ids: list[int], webhook_url: str | None = None) -> int:
    """Posts one Discord message per game in `game_ids` that crosses an
    alert threshold. Silently does nothing (returns 0) if no webhook is
    configured — alerts are opt-in, same as digest.py.
    """
    webhook_url = webhook_url or config.DISCORD_WEBHOOK_URL
    if not webhook_url or not game_ids:
        return 0

    sent = 0
    for game_id in game_ids:
        try:
            alert = check_game_for_alert(game_id)
        except Exception as e:
            logger.warning(f"Couldn't evaluate game {game_id} for an alert: {e}")
            continue
        if alert is None:
            continue
        try:
            resp = requests.post(webhook_url, json={"content": format_alert_message(alert)}, timeout=15)
            resp.raise_for_status()
            sent += 1
        except requests.exceptions.RequestException as e:
            logger.warning(f"Failed to post game alert for game {game_id}: {e}")
    return sent

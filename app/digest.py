"""
Stage F — Weekly Discord digest (optional).

Runs an incremental refresh + analysis pass, then posts a short summary to
a Discord webhook. The webhook URL is never hardcoded — set it yourself via
the DISCORD_WEBHOOK_URL environment variable (or .env) or the --webhook-url
flag.

This script only does the work when triggered; scheduling it (cron, Windows
Task Scheduler, etc.) is left to you rather than built here.
"""

import argparse
import logging
import sys

import requests

import config
from alerts import send_alerts_for_games
from batch_analyze import run_batch_analysis
from db import count_games, fetch_and_store, get_connection
from stats import top_takeaway

logger = logging.getLogger(__name__)


def _total_mistakes() -> int:
    conn = get_connection()
    try:
        return conn.execute("SELECT COUNT(*) as n FROM mistakes").fetchone()["n"]
    finally:
        conn.close()


def run_digest(
    lichess_user: str | None = None,
    chesscom_user: str | None = None,
    webhook_url: str | None = None,
) -> None:
    webhook_url = webhook_url or config.DISCORD_WEBHOOK_URL
    if not webhook_url:
        logger.error(
            "No Discord webhook URL configured. Set DISCORD_WEBHOOK_URL in "
            "your environment (or .env), or pass --webhook-url."
        )
        sys.exit(1)

    games_before = sum(count_games().values())
    mistakes_before = _total_mistakes()

    logger.info("Refreshing games...")
    fetch_and_store(lichess_user, chesscom_user, refresh=True)

    logger.info("Analyzing new games...")
    newly_analyzed = run_batch_analysis()

    # Per-game alerts (Section 10) fire first, on the same webhook, so a
    # notably bad game gets flagged on its own before being folded into
    # this run's aggregate summary below.
    alert_count = send_alerts_for_games(newly_analyzed, webhook_url)
    if alert_count:
        logger.info(f"Posted {alert_count} individual game alert(s).")

    new_games = sum(count_games().values()) - games_before
    new_mistakes = _total_mistakes() - mistakes_before
    takeaway = top_takeaway()

    message = (
        "**Chess Mistake Tracker — weekly digest**\n"
        f"New games analyzed: {new_games}\n"
        f"New mistakes found: {new_mistakes}\n"
        f"Current takeaway: {takeaway}"
    )

    logger.info(f"Posting to Discord:\n{message}")

    try:
        resp = requests.post(webhook_url, json={"content": message}, timeout=15)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to post digest to Discord: {e}")
        sys.exit(1)

    logger.info("Done.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(
        description="Refresh games, re-analyze, and post a summary to a Discord webhook."
    )
    parser.add_argument("--lichess-user", default=config.LICHESS_USERNAME,
                         help="Lichess username (or set LICHESS_USERNAME)")
    parser.add_argument("--chesscom-user", default=config.CHESSCOM_USERNAME,
                         help="Chess.com username (or set CHESSCOM_USERNAME)")
    parser.add_argument("--webhook-url", default=None,
                         help="Discord webhook URL (or set DISCORD_WEBHOOK_URL)")
    args = parser.parse_args()

    if not args.lichess_user and not args.chesscom_user:
        logger.error("No usernames given. Pass --lichess-user/--chesscom-user, or set "
                      "LICHESS_USERNAME/CHESSCOM_USERNAME in your environment.")
        sys.exit(1)

    run_digest(args.lichess_user, args.chesscom_user, args.webhook_url)

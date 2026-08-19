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
from db import fetch_and_store, get_connection
from fetchers import _request_with_retry
from stats import top_takeaway

logger = logging.getLogger(__name__)


class DigestError(Exception):
    """Raised on a configuration or delivery failure — a plain exception
    rather than sys.exit(), so run_digest() stays safe to call from
    anywhere (this module's own __main__ block, but also potentially a
    longer-lived caller like a future web-triggered digest) without
    sys.exit()'s SystemExit tearing down the whole calling process. Only
    __main__ below turns this into a process exit code.
    """


def _mistakes_for_games(game_ids: list[int]) -> int:
    if not game_ids:
        return 0
    conn = get_connection()
    try:
        placeholders = ",".join("?" * len(game_ids))
        return conn.execute(
            f"SELECT COUNT(*) as n FROM mistakes WHERE game_id IN ({placeholders})", game_ids
        ).fetchone()["n"]
    finally:
        conn.close()


def run_digest(
    lichess_user: str | None = None,
    chesscom_user: str | None = None,
    webhook_url: str | None = None,
) -> None:
    webhook_url = webhook_url or config.DISCORD_WEBHOOK_URL
    if not webhook_url:
        raise DigestError(
            "No Discord webhook URL configured. Set DISCORD_WEBHOOK_URL in "
            "your environment (or .env), or pass --webhook-url."
        )

    logger.info("Refreshing games...")
    fetch_result = fetch_and_store(lichess_user, chesscom_user, refresh=True)

    logger.info("Analyzing new games...")
    newly_analyzed = run_batch_analysis()

    # Per-game alerts (Section 10) fire first, on the same webhook, so a
    # notably bad game gets flagged on its own before being folded into
    # this run's aggregate summary below.
    alert_count = send_alerts_for_games(newly_analyzed, webhook_url)
    if alert_count:
        logger.info(f"Posted {alert_count} individual game alert(s).")

    # Derived directly from what THIS run did (fetch_and_store's own
    # "inserted" count, and mistakes scoped to exactly the games this run
    # analyzed) rather than a before/after global COUNT(*) diff — this app
    # supports multiple local profiles sharing one DB (profiles.py), and a
    # global diff would have folded in any other profile's games/mistakes
    # that happened to get fetched/analyzed around the same time, not just
    # this run's own.
    new_games = fetch_result["inserted"]
    new_mistakes = _mistakes_for_games(newly_analyzed)
    takeaway = top_takeaway()

    message = (
        "**Chesswise — weekly digest**\n"
        f"New games analyzed: {new_games}\n"
        f"New mistakes found: {new_mistakes}\n"
        f"Current takeaway: {takeaway}"
    )

    logger.info(f"Posting to Discord:\n{message}")

    try:
        resp = _request_with_retry("POST", webhook_url, json={"content": message}, timeout=15)
        resp.raise_for_status()
    except (requests.exceptions.RequestException, ConnectionError) as e:
        # _request_with_retry raises a plain (builtin) ConnectionError, not
        # a requests.exceptions one, once retries are exhausted on a
        # network error — not caught by RequestException alone.
        raise DigestError(f"Failed to post digest to Discord: {e}") from e

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

    try:
        run_digest(args.lichess_user, args.chesscom_user, args.webhook_url)
    except DigestError as e:
        logger.error(str(e))
        sys.exit(1)

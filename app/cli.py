#!/usr/bin/env python
"""
Chess Mistake Tracker — single CLI entry point.

Subcommands:
  fetch    Pull recent games for one or both sites and store them.
  analyze  Run Stockfish analysis on every unanalyzed stored game.
  refresh  fetch (incremental, since last stored game) + analyze in one step.
  puzzles  Generate tactics puzzles from flagged mistakes/blunders.
  digest   refresh + analyze + post a summary to a Discord webhook.
  serve    Start the local dashboard web server.

Run `python cli.py <command> --help` for a command's own options.

Username resolution (fetch/refresh/digest), in priority order:
  1. --lichess-user / --chesscom-user on the command line
  2. Remembered from the last successful fetch/refresh (cli_config.json)
  3. LICHESS_USERNAME / CHESSCOM_USERNAME in the environment or .env
"""

import argparse
import logging
import sys

import config
from cli_state import load_state, save_state

logger = logging.getLogger(__name__)


def _resolve_usernames(args) -> tuple[str | None, str | None]:
    state = load_state()
    lichess_user = args.lichess_user or state.get("lichess_user") or config.LICHESS_USERNAME
    chesscom_user = args.chesscom_user or state.get("chesscom_user") or config.CHESSCOM_USERNAME
    return lichess_user, chesscom_user


def cmd_fetch(args) -> None:
    from db import count_games, fetch_and_store

    lichess_user, chesscom_user = _resolve_usernames(args)
    if not lichess_user and not chesscom_user:
        logger.error("No usernames given. Pass --lichess-user and/or --chesscom-user "
                      "(or set LICHESS_USERNAME/CHESSCOM_USERNAME in .env).")
        sys.exit(1)

    fetch_and_store(lichess_user, chesscom_user, refresh=False, max_games=args.max_games)
    save_state(lichess_user=lichess_user, chesscom_user=chesscom_user)

    print("\nCurrent DB totals by source:")
    for source, n in count_games().items():
        print(f"  {source}: {n} games")


def cmd_analyze(args) -> None:
    from batch_analyze import run_batch_analysis

    run_batch_analysis(depth=args.depth, workers=args.workers)


def cmd_refresh(args) -> None:
    from batch_analyze import run_batch_analysis
    from db import count_games, fetch_and_store

    lichess_user, chesscom_user = _resolve_usernames(args)
    if not lichess_user and not chesscom_user:
        logger.error("No usernames given and none remembered from a previous run. "
                      "Pass --lichess-user and/or --chesscom-user.")
        sys.exit(1)

    fetch_and_store(lichess_user, chesscom_user, refresh=True)
    save_state(lichess_user=lichess_user, chesscom_user=chesscom_user)
    run_batch_analysis(depth=args.depth, workers=args.workers)

    print("\nCurrent DB totals by source:")
    for source, n in count_games().items():
        print(f"  {source}: {n} games")


def cmd_puzzles(args) -> None:
    from puzzles import generate_all_puzzles

    generate_all_puzzles()


def cmd_digest(args) -> None:
    from digest import run_digest

    lichess_user, chesscom_user = _resolve_usernames(args)
    if not lichess_user and not chesscom_user:
        logger.error("No usernames given and none remembered from a previous run. "
                      "Pass --lichess-user and/or --chesscom-user.")
        sys.exit(1)

    run_digest(lichess_user, chesscom_user, args.webhook_url)
    save_state(lichess_user=lichess_user, chesscom_user=chesscom_user)


def cmd_serve(args) -> None:
    import uvicorn

    uvicorn.run("server:app", host=args.host, port=args.port, reload=args.reload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Chess Mistake Tracker — fetch, analyze, and review your games.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_fetch = subparsers.add_parser(
        "fetch", help="Pull recent games for one or both sites and store them (full pull, not incremental)."
    )
    p_fetch.add_argument("--lichess-user", default=None, help="overrides the remembered/configured username")
    p_fetch.add_argument("--chesscom-user", default=None)
    p_fetch.add_argument("--max-games", type=int, default=20, help="max games to pull per site (default: 20)")
    p_fetch.set_defaults(func=cmd_fetch)

    p_analyze = subparsers.add_parser("analyze", help="Run Stockfish analysis on every unanalyzed stored game.")
    p_analyze.add_argument("--depth", type=int, default=config.STOCKFISH_DEPTH,
                            help=f"Stockfish search depth, higher = slower but stronger (default: {config.STOCKFISH_DEPTH})")
    p_analyze.add_argument("--workers", type=int, default=config.ANALYSIS_WORKERS,
                            help=f"parallel worker processes, 1 = sequential (default: {config.ANALYSIS_WORKERS})")
    p_analyze.set_defaults(func=cmd_analyze)

    p_refresh = subparsers.add_parser("refresh", help="fetch (incremental, since last run) + analyze in one step.")
    p_refresh.add_argument("--lichess-user", default=None, help="overrides the remembered/configured username")
    p_refresh.add_argument("--chesscom-user", default=None)
    p_refresh.add_argument("--depth", type=int, default=config.STOCKFISH_DEPTH)
    p_refresh.add_argument("--workers", type=int, default=config.ANALYSIS_WORKERS)
    p_refresh.set_defaults(func=cmd_refresh)

    p_puzzles = subparsers.add_parser("puzzles", help="Generate tactics puzzles from flagged mistakes/blunders.")
    p_puzzles.set_defaults(func=cmd_puzzles)

    p_digest = subparsers.add_parser("digest", help="refresh + analyze + post a summary to a Discord webhook.")
    p_digest.add_argument("--lichess-user", default=None)
    p_digest.add_argument("--chesscom-user", default=None)
    p_digest.add_argument("--webhook-url", default=None, help="overrides DISCORD_WEBHOOK_URL from .env")
    p_digest.set_defaults(func=cmd_digest)

    p_serve = subparsers.add_parser("serve", help="Start the local dashboard web server.")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--reload", action="store_true", help="auto-reload on code changes (development)")
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

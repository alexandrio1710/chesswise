"""
Stage 2 — SQLite storage for fetched games and, later, detected mistakes.

Single-file DB, no external setup. Re-running a fetch+store for the same
username is safe: games are deduped on (source, source_game_id).
"""

import logging
import sqlite3
from datetime import datetime

from migrations import DB_PATH, run_migrations

logger = logging.getLogger(__name__)


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets one writer and multiple readers work concurrently instead of
    # blocking on the whole-file lock SQLite's default journal mode uses —
    # needed for parallel batch analysis (Final Pass 5), where several
    # worker processes commit to the same DB file around the same time.
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    run_migrations()


# Run once at import time so any module using get_connection() directly
# (not just save_games(), which called this explicitly before) still gets
# a migrated schema — mistakes.py and batch_analyze.py rely on this.
init_db()


def save_games(games: list[dict], profile_id: int | None = None) -> dict:
    """Insert normalized games (from fetchers.py) into the DB.

    Duplicates (same source + source_game_id) are silently skipped via
    INSERT OR IGNORE, so re-fetching is always safe to re-run.

    `profile_id` (Section 9) tags every inserted row with which local
    profile these games belong to. None leaves them unassigned — the
    same state every game was in before profiles existed, and still what
    a caller with no profile context (or a fresh install before any
    profile exists) gets by default.

    A skipped game can mean two different things, and callers care which:
    a game you've already fetched before under THIS SAME profile (the
    common, boring case re-fetching guards against), or a game that
    exists in this database under a DIFFERENT profile — which happens
    when two people you track locally have played each other, so
    whichever profile fetched it first "owns" the one stored row (a
    single game can't belong to two profile_id values at once). Found via
    a real case: adding an opponent's account as its own profile reported
    "0 new games" with no explanation even though the games were real —
    `skipped_other_profile` is what makes that legible instead of silent.

    Returns {"inserted": N, "skipped": M, "skipped_other_profile": K}
    (K <= M; the other M - K were already yours).
    """
    init_db()
    conn = get_connection()
    inserted = 0
    skipped_other_profile = 0
    try:
        for g in games:
            existing = conn.execute(
                "SELECT profile_id FROM games WHERE source = ? AND source_game_id = ?",
                (g["source"], g["source_game_id"]),
            ).fetchone()

            cur = conn.execute(
                """
                INSERT OR IGNORE INTO games
                    (source, source_game_id, date, opponent, result, color,
                     time_control, opening_name, pgn, player_rating, opponent_rating, profile_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    g["source"], g["source_game_id"], g["date"], g["opponent"],
                    g["result"], g["color"], g["time_control"],
                    g["opening_name"], g["pgn"],
                    g.get("player_rating"), g.get("opponent_rating"), profile_id,
                ),
            )
            if cur.rowcount:
                inserted += 1
            elif existing is not None and existing["profile_id"] != profile_id:
                skipped_other_profile += 1
        conn.commit()
    finally:
        conn.close()
    return {
        "inserted": inserted,
        "skipped": len(games) - inserted,
        "skipped_other_profile": skipped_other_profile,
    }


def get_all_games(source: str | None = None) -> list[sqlite3.Row]:
    conn = get_connection()
    try:
        if source:
            return conn.execute(
                "SELECT * FROM games WHERE source = ? ORDER BY date DESC", (source,)
            ).fetchall()
        return conn.execute("SELECT * FROM games ORDER BY date DESC").fetchall()
    finally:
        conn.close()


def get_pgn(game_id: int) -> str:
    conn = get_connection()
    try:
        row = conn.execute("SELECT pgn FROM games WHERE id = ?", (game_id,)).fetchone()
        return row["pgn"]
    finally:
        conn.close()


def get_latest_game_date(source: str, profile_id: int | None = None) -> str | None:
    """Most recent stored game's date for a source, used by --refresh (Stage
    C) to only fetch games newer than what's already in the DB.

    Scoped to `profile_id` when given (Section 9) — without this, a brand
    new account being added to a database that already tracks someone
    else would compute "since" from THAT OTHER account's latest game,
    silently filtering out everything from the new account older than
    that unrelated cutoff. Real bug, found via a real account
    ("Evelyn_isbetteratchess", first-ever fetch returning 0 games despite
    2 known real games) that predates Section 9 — this query used to be
    correct back when only one account was ever tracked per database.
    """
    conn = get_connection()
    try:
        if profile_id is not None:
            row = conn.execute(
                "SELECT MAX(date) as latest FROM games WHERE source = ? AND profile_id = ?",
                (source, profile_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT MAX(date) as latest FROM games WHERE source = ?", (source,)
            ).fetchone()
        return row["latest"] if row else None
    finally:
        conn.close()


def count_games() -> dict:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT source, COUNT(*) as n FROM games GROUP BY source"
        ).fetchall()
        return {row["source"]: row["n"] for row in rows}
    finally:
        conn.close()


def _iso_to_epoch(iso_str: str) -> int:
    return int(datetime.fromisoformat(iso_str).timestamp())


def fetch_and_store(
    lichess_user: str | None = None,
    chesscom_user: str | None = None,
    refresh: bool = False,
    max_games: int = 20,
) -> dict:
    """Fetch games for whichever username(s) are given and store them.

    With `refresh=True` (Stage C), only games newer than what's already
    stored for that source are requested in the first place — via Lichess's
    `since` filter and Chess.com's archive-month narrowing — rather than
    re-fetching everything and relying on dedup to discard the repeats.

    Each source's games are tagged with whichever local profile that
    username is linked to (Section 9), auto-creating one on first use so
    fetching still works with zero setup — same as it always has. The two
    usernames are resolved (and saved) separately since they aren't
    guaranteed to belong to the same profile.
    """
    from fetchers import fetch_chesscom_games, fetch_lichess_games
    from profiles import resolve_profile_id

    init_db()
    inserted = 0
    skipped = 0
    skipped_other_profile = 0

    if lichess_user:
        # Resolved before the refresh cutoff is computed — a brand new
        # account's own history is empty, so it always gets a full first
        # fetch, never one scoped by some OTHER already-tracked account's
        # latest game (see get_latest_game_date's docstring).
        profile_id = resolve_profile_id("lichess", lichess_user)
        since_ms = None
        if refresh:
            latest = get_latest_game_date("lichess", profile_id=profile_id)
            if latest:
                since_ms = (_iso_to_epoch(latest) + 1) * 1000
        try:
            lichess_games = fetch_lichess_games(lichess_user, max_games=max_games, since_ms=since_ms)
            suffix = " (since last refresh)" if since_ms else ""
            logger.info(f"Fetched {len(lichess_games)} Lichess games for '{lichess_user}'{suffix}")
            result = save_games(lichess_games, profile_id=profile_id)
            inserted += result["inserted"]
            skipped += result["skipped"]
            skipped_other_profile += result["skipped_other_profile"]
        except Exception as e:
            # Don't let a Lichess outage/error block Chess.com's fetch below —
            # whatever DID come in from the other source still gets stored.
            logger.error(f"Failed to fetch Lichess games for '{lichess_user}': {e}")

    if chesscom_user:
        profile_id = resolve_profile_id("chesscom", chesscom_user)
        since_epoch = None
        if refresh:
            latest = get_latest_game_date("chesscom", profile_id=profile_id)
            if latest:
                since_epoch = _iso_to_epoch(latest) + 1
        try:
            chesscom_games = fetch_chesscom_games(
                chesscom_user, months_back=2, max_games=max_games, since_epoch=since_epoch
            )
            suffix = " (since last refresh)" if since_epoch else ""
            logger.info(f"Fetched {len(chesscom_games)} Chess.com games for '{chesscom_user}'{suffix}")
            result = save_games(chesscom_games, profile_id=profile_id)
            inserted += result["inserted"]
            skipped += result["skipped"]
            skipped_other_profile += result["skipped_other_profile"]
        except Exception as e:
            logger.error(f"Failed to fetch Chess.com games for '{chesscom_user}': {e}")

    logger.info(f"Inserted {inserted} new games, skipped {skipped} duplicates")
    return {"inserted": inserted, "skipped": skipped, "skipped_other_profile": skipped_other_profile}


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    refresh = "--refresh" in sys.argv

    lichess_user = args[0] if len(args) > 0 else None
    chesscom_user = args[1] if len(args) > 1 else None

    if not lichess_user and not chesscom_user:
        print("Usage: python db.py <lichess_username> [chesscom_username] [--refresh]")
        print("       python db.py \"\" <chesscom_username>   (chess.com only)")
        sys.exit(0)

    print(f"DB initialized at {DB_PATH}")
    fetch_and_store(lichess_user, chesscom_user, refresh=refresh)

    print("\nCurrent DB totals by source:")
    for source, n in count_games().items():
        print(f"  {source}: {n} games")

"""
Final pass — Schema versioning and migrations.

A minimal "check current version, apply pending migrations in order"
system — no ORM, no external framework. Each migration is a plain Python
function that mutates a connection in place, and must be safe to run
against a brand-new empty database (existing tables/columns are always
guarded with IF NOT EXISTS / existence checks).

Before any pending migration runs, the whole database file is copied to a
timestamped backup, so a bad migration is always recoverable by restoring
that file — nothing here should ever risk losing already-analyzed games.
"""

import logging
import re
import shutil
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

from config import DB_PATH

logger = logging.getLogger(__name__)


def _migration_001_initial_schema(conn: sqlite3.Connection) -> None:
    """Games, mistakes, and puzzles tables. This collapses everything
    added incrementally across the project's earlier stages (skip_reason,
    analyzed_at, ply, color_moved, the puzzles table) into one shape —
    schema versioning starts tracking from here.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            source_game_id TEXT NOT NULL,
            date TEXT,
            opponent TEXT,
            result TEXT,
            color TEXT,
            time_control TEXT,
            opening_name TEXT,
            pgn TEXT,
            analyzed INTEGER NOT NULL DEFAULT 0,
            skip_reason TEXT,
            analyzed_at TEXT,
            UNIQUE(source, source_game_id)
        );

        CREATE TABLE IF NOT EXISTS mistakes (
            id INTEGER PRIMARY KEY,
            game_id INTEGER REFERENCES games(id),
            ply INTEGER,
            move_number INTEGER,
            move_san TEXT,
            color_moved TEXT,
            phase TEXT,
            severity TEXT,
            eval_before REAL,
            eval_after REAL,
            eval_drop REAL,
            clock_seconds_remaining INTEGER
        );

        CREATE TABLE IF NOT EXISTS puzzles (
            id INTEGER PRIMARY KEY,
            mistake_id INTEGER NOT NULL REFERENCES mistakes(id),
            game_id INTEGER NOT NULL REFERENCES games(id),
            fen_before TEXT NOT NULL,
            side_to_move TEXT NOT NULL,
            played_move_san TEXT NOT NULL,
            best_move_uci TEXT NOT NULL,
            best_move_san TEXT NOT NULL,
            top_lines TEXT NOT NULL,
            phase TEXT NOT NULL,
            severity TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(mistake_id)
        );
    """)

    # Columns added incrementally before schema versioning existed, kept
    # as an ALTER-if-missing check rather than raw SQL above: CREATE TABLE
    # IF NOT EXISTS won't add a column to a table that already exists with
    # an older shape (a DB from before these columns existed).
    column_migrations = {
        "games": [("skip_reason", "TEXT"), ("analyzed_at", "TEXT")],
        "mistakes": [("ply", "INTEGER"), ("color_moved", "TEXT")],
    }
    for table, columns in column_migrations.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for col_name, col_type in columns:
            if col_name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")


def _migration_002_puzzle_explanations(conn: sqlite3.Connection) -> None:
    """Plain-English explanations for the best move and the move actually
    played in each puzzle, so the feedback screen can say more than raw
    engine notation.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(puzzles)")}
    for col_name in ("best_move_explanation", "played_move_explanation"):
        if col_name not in existing:
            conn.execute(f"ALTER TABLE puzzles ADD COLUMN {col_name} TEXT")


def _migration_003_game_moves(conn: sqlite3.Connection) -> None:
    """Full per-move evaluation trace for every analyzed game (not just
    flagged mistakes), so a game can be reviewed move-by-move with an eval
    graph without re-running Stockfish each time it's viewed.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS game_moves (
            id INTEGER PRIMARY KEY,
            game_id INTEGER NOT NULL REFERENCES games(id),
            ply INTEGER NOT NULL,
            move_number INTEGER NOT NULL,
            color_moved TEXT NOT NULL,
            move_san TEXT NOT NULL,
            eval_cp REAL NOT NULL,
            clock_seconds_remaining INTEGER,
            UNIQUE(game_id, ply)
        );
    """)


def _migration_004_move_tiers_and_notes(conn: sqlite3.Connection) -> None:
    """Full Game Review (advanced features, Section 1): every move gets a
    quality tier (best/excellent/good, in addition to the existing
    inaccuracy/mistake/blunder), which needs eval_before_cp and eval_drop
    stored per move — previously only computed for flagged mistakes, not
    every move. Also adds free-text notes on a game or a specific move.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(game_moves)")}
    for col_name, col_type in (("eval_before_cp", "REAL"), ("eval_drop", "REAL"), ("tier", "TEXT")):
        if col_name not in existing:
            conn.execute(f"ALTER TABLE game_moves ADD COLUMN {col_name} {col_type}")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY,
            game_id INTEGER NOT NULL REFERENCES games(id),
            ply INTEGER,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """)


def _migration_005_puzzle_srs(conn: sqlite3.Connection) -> None:
    """Advanced features, Section 4 — per-puzzle attempt history and a
    Leitner-system spaced-repetition schedule, so Puzzle Rush sessions and
    "due for review" queues have real data to work from.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS puzzle_attempts (
            id INTEGER PRIMARY KEY,
            puzzle_id INTEGER NOT NULL REFERENCES puzzles(id),
            correct INTEGER NOT NULL,
            time_taken_ms INTEGER,
            session_type TEXT NOT NULL DEFAULT 'practice',
            attempted_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS puzzle_review_state (
            puzzle_id INTEGER PRIMARY KEY REFERENCES puzzles(id),
            leitner_box INTEGER NOT NULL DEFAULT 1,
            next_review_at TEXT NOT NULL,
            total_attempts INTEGER NOT NULL DEFAULT 0,
            total_correct INTEGER NOT NULL DEFAULT 0,
            last_reviewed_at TEXT
        );
    """)


def _migration_006_ratings(conn: sqlite3.Connection) -> None:
    """Advanced features, Section 7 — rating at time of game, a
    prerequisite for the rating-progress chart and performance-vs-
    opponent-rating-band insight. Both Lichess and Chess.com already
    embed WhiteElo/BlackElo in the PGN text this app already stores, so
    this is a schema change plus a pure-Python backfill (see
    backfill_ratings.py) — no new API calls needed.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(games)")}
    for col_name in ("player_rating", "opponent_rating"):
        if col_name not in existing:
            conn.execute(f"ALTER TABLE games ADD COLUMN {col_name} INTEGER")


_PLAYER_NAME_RE = re.compile(r'\[(White|Black)\s+"([^"]*)"\]')


def _migration_007_profiles(conn: sqlite3.Connection) -> None:
    """Advanced features, Section 9 — Multi-Profile Support. A profile is
    just a name plus the Lichess/Chess.com usernames linked to it (no
    accounts, no login) — this lets more than one person's games share
    one local database without the app assuming there's only one "you".
    A username can only be linked to one profile, which is what routes an
    incoming fetched game to the right profile automatically.

    Existing games predate profiles entirely, so this also backfills:
    detects each stored game's own username (from its PGN's White/Black
    tag matching that game's already-stored `color`) per source, creates
    one profile from whichever usernames come up most often, links them,
    and tags every existing game with that profile — so upgrading doesn't
    silently orphan a user's whole history from their own profile.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS profile_usernames (
            id INTEGER PRIMARY KEY,
            profile_id INTEGER NOT NULL REFERENCES profiles(id),
            source TEXT NOT NULL,
            username TEXT NOT NULL,
            UNIQUE(source, username)
        );
    """)

    existing = {row["name"] for row in conn.execute("PRAGMA table_info(games)")}
    if "profile_id" not in existing:
        conn.execute("ALTER TABLE games ADD COLUMN profile_id INTEGER REFERENCES profiles(id)")

    rows = conn.execute("SELECT id, source, color, pgn FROM games WHERE pgn IS NOT NULL").fetchall()
    if not rows:
        return

    usernames_by_source: dict[str, Counter] = {}
    for row in rows:
        names = dict(_PLAYER_NAME_RE.findall(row["pgn"] or ""))
        mine = names.get("White") if row["color"] == "white" else names.get("Black")
        if mine and mine not in ("Unknown", "?"):
            usernames_by_source.setdefault(row["source"], Counter())[mine] += 1

    detected = {src: counter.most_common(1)[0][0] for src, counter in usernames_by_source.items() if counter}
    if not detected:
        return  # nothing usable to name/link a profile from — leave profile_id NULL

    profile_name = detected.get("lichess") or detected.get("chesscom") or next(iter(detected.values()))
    cur = conn.execute(
        "INSERT INTO profiles (name, created_at) VALUES (?, datetime('now'))", (profile_name,)
    )
    profile_id = cur.lastrowid
    for source, username in detected.items():
        conn.execute(
            "INSERT OR IGNORE INTO profile_usernames (profile_id, source, username) VALUES (?, ?, ?)",
            (profile_id, source, username.lower()),
        )

    conn.execute("UPDATE games SET profile_id = ? WHERE profile_id IS NULL", (profile_id,))


# (version, description, migration_fn). Append new entries here for future
# schema changes — never edit or reorder an already-shipped migration, since
# a DB that already recorded it as applied would silently skip your edit.
def _migration_008_opening_puzzles(conn: sqlite3.Connection) -> None:
    """Opening-based puzzles (user-requested addition): a local cache of
    puzzles pulled from Lichess's public puzzle API for openings the
    player actually plays, kept separate from the existing `puzzles`
    table (see opening_puzzles.py's module docstring for why).
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS opening_puzzles (
            id INTEGER PRIMARY KEY,
            external_id TEXT NOT NULL UNIQUE,
            opening_family TEXT NOT NULL,
            fen TEXT NOT NULL,
            side_to_move TEXT NOT NULL,
            solution_uci TEXT NOT NULL,
            lichess_rating INTEGER,
            themes TEXT,
            game_url TEXT,
            fetched_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            correct INTEGER NOT NULL DEFAULT 0
        );
    """)


def _migration_009_goals(conn: sqlite3.Connection) -> None:
    """Advanced features, Section 10 — simple goal tracking (e.g. "reduce
    endgame blunder rate below 20%") behind the new /progress page.
    `achieved_at` is set the first time a goal is found met and never
    cleared even if the metric later regresses — a "first hit this
    target" marker, not a live "currently passing" flag (evaluate_goal()
    in progress.py reports the live current/met state separately).
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY,
            profile_id INTEGER REFERENCES profiles(id),
            source TEXT,
            metric TEXT NOT NULL,
            phase TEXT,
            comparison TEXT NOT NULL,
            target_value REAL NOT NULL,
            description TEXT NOT NULL,
            created_at TEXT NOT NULL,
            achieved_at TEXT
        );
    """)


MIGRATIONS = [
    (1, "Initial schema: games, mistakes, puzzles tables", _migration_001_initial_schema),
    (2, "Add puzzle move explanations", _migration_002_puzzle_explanations),
    (3, "Add game_moves table for full per-game analysis", _migration_003_game_moves),
    (4, "Add move tiers (game_moves) and free-text notes table", _migration_004_move_tiers_and_notes),
    (5, "Add puzzle_attempts and puzzle_review_state (spaced repetition)", _migration_005_puzzle_srs),
    (6, "Add player_rating/opponent_rating to games", _migration_006_ratings),
    (7, "Add profiles/profile_usernames tables and games.profile_id", _migration_007_profiles),
    (8, "Add opening_puzzles table (Lichess-sourced opening puzzles)", _migration_008_opening_puzzles),
    (9, "Add goals table", _migration_009_goals),
]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _get_schema_version(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "schema_version"):
        return 0
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    return row[0] if row else 0


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))


def backup_database() -> Path | None:
    """Copy the DB file to a timestamped backup. Returns the backup path,
    or None if there's no DB file yet (fresh install — nothing to lose).
    """
    if not DB_PATH.exists():
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = DB_PATH.parent / f"{DB_PATH.stem}_backup_{timestamp}{DB_PATH.suffix}"
    shutil.copy2(DB_PATH, backup_path)
    return backup_path


def run_migrations() -> None:
    """Bring the database up to the latest schema version. Cheap to call
    on every startup: reading the current version is the only work done
    when there's nothing pending.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        current_version = _get_schema_version(conn)  # read-only so far
        pending = [m for m in MIGRATIONS if m[0] > current_version]
        if not pending:
            return
        conn.close()

        backup_path = backup_database()
        if backup_path:
            logger.info(f"Backed up database to {backup_path.name} before applying {len(pending)} migration(s)")

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        for version, description, migration_fn in pending:
            logger.info(f"Applying migration {version}: {description}")
            try:
                migration_fn(conn)
                _set_schema_version(conn, version)
                conn.commit()
            except Exception as e:
                conn.rollback()
                restore_hint = f" Restore it from {backup_path}." if backup_path else ""
                logger.error(f"Migration {version} ({description}) failed: {e}.{restore_hint}")
                raise
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    logger.info(f"Current schema version: {_get_schema_version(conn)}")
    conn.close()
    run_migrations()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    logger.info(f"Schema version after run: {_get_schema_version(conn)}")
    conn.close()

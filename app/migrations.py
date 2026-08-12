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
import shutil
import sqlite3
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


# (version, description, migration_fn). Append new entries here for future
# schema changes — never edit or reorder an already-shipped migration, since
# a DB that already recorded it as applied would silently skip your edit.
MIGRATIONS = [
    (1, "Initial schema: games, mistakes, puzzles tables", _migration_001_initial_schema),
    (2, "Add puzzle move explanations", _migration_002_puzzle_explanations),
    (3, "Add game_moves table for full per-game analysis", _migration_003_game_moves),
    (4, "Add move tiers (game_moves) and free-text notes table", _migration_004_move_tiers_and_notes),
    (5, "Add puzzle_attempts and puzzle_review_state (spaced repetition)", _migration_005_puzzle_srs),
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

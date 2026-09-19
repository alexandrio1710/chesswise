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

import io
import json
import logging
import math
import re
import shutil
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

import chess.pgn

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


def _migration_010_users_and_sessions(conn: sqlite3.Connection) -> None:
    """Web platform, Section 1 — Multi-User Authentication. `users` holds
    one row per Lichess account that has ever logged in (identified by
    Lichess's own immutable account id, not the mutable display username).
    `sessions` backs the login cookie: a random token whose SHA-256 hash is
    stored here (never the raw token — same reasoning as a password hash,
    since this table is the bearer credential for a logged-in user).
    `oauth_states` is a short-lived table for the OAuth2 PKCE handshake
    (state + code_verifier) — needed because the app has no session yet at
    the point it must remember the verifier between the redirect to Lichess
    and the callback coming back.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            lichess_id TEXT NOT NULL UNIQUE,
            username TEXT NOT NULL,
            email TEXT,
            lichess_title TEXT,
            created_at TEXT NOT NULL,
            last_login_at TEXT
        );

        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);

        CREATE TABLE IF NOT EXISTS oauth_states (
            state TEXT PRIMARY KEY,
            code_verifier TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """)


def _migration_011_user_ownership(conn: sqlite3.Connection) -> None:
    """Web platform, Section 1 — attach every profile/game/puzzle to the
    user that owns it. Left NULL for pre-existing local data rather than
    guessed at migration time (which user owns it isn't something a schema
    migration can know) — `auth.claim_unowned_data()` is the deliberate,
    user-triggered way to assign a first-time Lichess login's existing
    local history to their new account.

    `puzzles.user_id` duplicates what's derivable via puzzles.game_id ->
    games.user_id; it's kept as a direct column (backfilled from the
    parent game, and set alongside game_id at puzzle-creation time) purely
    so ownership-scoped puzzle queries don't need a join on every request —
    it must never diverge from the parent game's owner.
    """
    for table in ("profiles", "games", "puzzles"):
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "user_id" not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER REFERENCES users(id)")

    conn.execute("""
        UPDATE puzzles SET user_id = (
            SELECT g.user_id FROM games g WHERE g.id = puzzles.game_id
        ) WHERE user_id IS NULL
    """)

    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_profiles_user_id ON profiles(user_id);
        CREATE INDEX IF NOT EXISTS idx_games_user_id ON games(user_id);
        CREATE INDEX IF NOT EXISTS idx_puzzles_user_id ON puzzles(user_id);
    """)


def _migration_012_puzzle_progress_sm2(conn: sqlite3.Connection) -> None:
    """Web platform, Section 2 — per-user SuperMemo-2 scheduling state, one
    row per (user, puzzle). This is deliberately separate from the existing
    `puzzle_review_state` table (migration 5): that table's Leitner-box
    state is keyed by puzzle_id ALONE, which only ever worked because the
    app had exactly one implicit user — it can't express "user A has seen
    this puzzle 4 times, user B has never seen it" for a puzzle two users
    both have access to. `puzzle_progress` is the multi-user-correct
    replacement surface (see srs_sm2.py); the older table and srs.py are
    left in place for any single-profile/local-only code path still using
    them, rather than ripped out as part of this migration.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS puzzle_progress (
            user_id INTEGER NOT NULL REFERENCES users(id),
            puzzle_id INTEGER NOT NULL REFERENCES puzzles(id),
            repetition_count INTEGER NOT NULL DEFAULT 0,
            easiness_factor REAL NOT NULL DEFAULT 2.5,
            interval_days INTEGER NOT NULL DEFAULT 0,
            next_review_date TEXT NOT NULL,
            last_reviewed_at TEXT,
            PRIMARY KEY (user_id, puzzle_id)
        );
        CREATE INDEX IF NOT EXISTS idx_puzzle_progress_due ON puzzle_progress(user_id, next_review_date);
    """)


def _migration_013_eco_codes(conn: sqlite3.Connection) -> None:
    """Web platform, Section 3 — a local copy of the standard ECO
    (Encyclopedia of Chess Openings) reference data, plus a column on
    `games` to store the exact classification computed from it. `pgn` here
    is the ECO entry's own move sequence in plain SAN, space-separated, no
    move numbers (e.g. "e4 e5 Nf3 Nc6 Bc4") — matching the format of the
    canonical lichess-org/chess-openings dataset this table is populated
    from (see eco_import.py), so classify_game_opening() can do a straight
    string match against it.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS eco_codes (
            id INTEGER PRIMARY KEY,
            eco TEXT NOT NULL,
            name TEXT NOT NULL,
            pgn TEXT NOT NULL UNIQUE,
            ply_count INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_eco_codes_pgn ON eco_codes(pgn);
    """)

    existing = {row["name"] for row in conn.execute("PRAGMA table_info(games)")}
    if "eco" not in existing:
        conn.execute("ALTER TABLE games ADD COLUMN eco TEXT")


def _migration_014_analysis_status(conn: sqlite3.Connection) -> None:
    """Web platform, Section 4 — explicit lifecycle state for Stockfish
    analysis once it runs as a Celery task instead of inline/batch: a task
    can be queued, picked up by a worker, finish, or fail independently of
    the request that queued it, which the old boolean `analyzed` column
    can't represent (it's already meaningful pre-existing state — see
    migration 1 — so it's left alone; `analysis_status` is additive, not a
    replacement). `analysis_task_id` lets /api/analyze/status look up live
    Celery task state instead of only trusting the last-written DB status,
    and `analysis_error` carries a failure message to the UI instead of it
    only living in a worker's logs.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(games)")}
    backfill_needed = "analysis_status" not in existing
    for col_name, col_type in (
        ("analysis_status", "TEXT"),
        ("analysis_task_id", "TEXT"),
        ("analysis_error", "TEXT"),
    ):
        if col_name not in existing:
            conn.execute(f"ALTER TABLE games ADD COLUMN {col_name} {col_type}")

    if backfill_needed:
        conn.execute("UPDATE games SET analysis_status = 'completed' WHERE analyzed = 1")
        conn.execute("UPDATE games SET analysis_status = 'pending' WHERE analyzed = 0")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_games_analysis_status ON games(analysis_status)")


def _migration_015_game_reports(conn: sqlite3.Connection) -> None:
    """Game Report feature — full ten-tier move classification (Brilliant/
    Great/Best/Excellent/Good/Book/Inaccuracy/Mistake/Miss/Blunder) and a
    per-game estimated performance rating, on top of the existing six-tier
    Full Game Review (migration 4).

    `game_moves.classification`/`is_top_choice` are left NULL until
    game_report.compute_enriched_classification() runs for that game (an
    on-demand, opt-in enrichment pass — see that module's docstring for why
    it isn't part of the routine analysis pipeline). `game_moves.phase` is
    new too: the existing `phase` classification only ever got stored on
    flagged mistakes (the `mistakes` table), not on every move, and a
    phase-by-phase accuracy breakdown needs it for every move.

    `game_reports` caches the computed report (accuracy/rating/tier counts/
    summary) per game so repeat views don't re-run the enrichment pass.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(game_moves)")}
    for col_name, col_type in (
        ("classification", "TEXT"),
        ("is_top_choice", "INTEGER"),
        ("phase", "TEXT"),
    ):
        if col_name not in existing:
            conn.execute(f"ALTER TABLE game_moves ADD COLUMN {col_name} {col_type}")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS game_reports (
            game_id INTEGER PRIMARY KEY REFERENCES games(id),
            accuracy_overall REAL,
            accuracy_opening REAL,
            accuracy_middlegame REAL,
            accuracy_endgame REAL,
            estimated_rating INTEGER,
            tier_counts TEXT NOT NULL,
            summary TEXT NOT NULL,
            computed_at TEXT NOT NULL
        );
    """)


def _migration_016_fix_leitner_srs_datetime_format(conn: sqlite3.Connection) -> None:
    """Bug fix — srs.record_attempt originally stored next_review_at via
    Python's datetime.isoformat() ("2026-08-19T06:00:00.123456", local
    time), compared against SQLite's own datetime('now')
    ("2026-08-19 20:00:00", space-separated, UTC) via plain TEXT `<=`.
    Since 'T' (0x54) sorts after ' ' (0x20), every row written by the old
    code compared as "not yet due" regardless of the actual date/time —
    same root cause and fix as auth.py's create_session (see its own
    comment), independently present here since the two modules were
    written separately. See srs.py's record_attempt for the corrected
    version.

    Rewrites existing rows into SQLite's own datetime() text format so
    already-scheduled reviews compare correctly again. This can't (and
    doesn't try to) correct the local-vs-UTC offset those old values were
    originally computed in — only the separator/format that was breaking
    every comparison outright, unconditionally, regardless of timezone.
    """
    conn.execute(
        "UPDATE puzzle_review_state "
        "SET next_review_at = substr(replace(next_review_at, 'T', ' '), 1, 19) "
        "WHERE next_review_at LIKE '%T%'"
    )


def _migration_017_fix_missed_mate_severity_and_tier(conn: sqlite3.Connection) -> None:
    """Bug fix — a move that finds a slower forced mate than the fastest
    one available (still completely winning either way) used to get its
    eval_drop computed as a raw, uncapped difference of mover-perspective
    evals. Since analysis.py represents mate scores as roughly
    +-MATE_SCORE_CP (10000), such a move could carry a "drop" in the
    thousands of centipawns and get flagged as a blunder despite nothing
    practical changing — reported live as "moves that miss mate show up
    as a blunder." mistakes.py now caps eval_before/eval_after to
    stats.ACCURACY_EVAL_CAP_CP before differencing (stats.capped_eval_drop)
    at analysis time; this migration re-derives eval_drop/severity/tier
    for every already-analyzed game's stored rows from the raw evals
    already on disk, without re-running Stockfish.

    Deliberately not imported from mistakes.py/stats.py: db.py runs
    migrations at its own import time, and stats.py itself imports `from
    db import get_connection` near its top (before capped_eval_drop is
    even defined further down the file), so importing back from stats/
    mistakes here — even deferred inside the function — hits stats.py
    mid-initialization and fails ("partially initialized module"),
    confirmed by actually running this migration. A migration should
    encode a fixed, point-in-time transformation anyway, so these are
    intentionally frozen copies of the current thresholds/logic rather
    than a live dependency on code that could change under it later.
    """
    ACCURACY_EVAL_CAP_CP = 1000
    INACCURACY_THRESHOLD_CP = 100
    MISTAKE_THRESHOLD_CP = 200
    BLUNDER_THRESHOLD_CP = 400
    EXCELLENT_THRESHOLD_CP = 25
    BEST_THRESHOLD_CP = 10

    def capped_eval_drop(eval_before_cp, eval_after_cp):
        capped_before = max(-ACCURACY_EVAL_CAP_CP, min(ACCURACY_EVAL_CAP_CP, eval_before_cp))
        capped_after = max(-ACCURACY_EVAL_CAP_CP, min(ACCURACY_EVAL_CAP_CP, eval_after_cp))
        return max(0.0, capped_before - capped_after)

    def classify_severity(eval_drop_cp):
        if eval_drop_cp >= BLUNDER_THRESHOLD_CP:
            return "blunder"
        if eval_drop_cp >= MISTAKE_THRESHOLD_CP:
            return "mistake"
        if eval_drop_cp >= INACCURACY_THRESHOLD_CP:
            return "inaccuracy"
        return None

    def classify_tier(eval_drop_cp):
        eval_drop_cp = max(0.0, eval_drop_cp)
        if eval_drop_cp >= BLUNDER_THRESHOLD_CP:
            return "blunder"
        if eval_drop_cp >= MISTAKE_THRESHOLD_CP:
            return "mistake"
        if eval_drop_cp >= INACCURACY_THRESHOLD_CP:
            return "inaccuracy"
        if eval_drop_cp >= EXCELLENT_THRESHOLD_CP:
            return "good"
        if eval_drop_cp >= BEST_THRESHOLD_CP:
            return "excellent"
        return "best"

    affected_game_ids: set[int] = set()

    mistake_rows = conn.execute(
        "SELECT id, game_id, eval_before, eval_after, eval_drop, severity FROM mistakes"
    ).fetchall()
    to_delete = []
    for row in mistake_rows:
        if row["eval_before"] is None or row["eval_after"] is None:
            continue
        new_drop = capped_eval_drop(row["eval_before"], row["eval_after"])
        new_severity = classify_severity(new_drop)
        if new_severity is None:
            # The mate-swing (or similar) that used to clear the
            # inaccuracy bar no longer does — this mistake, and any
            # puzzle/practice history generated from it, no longer apply.
            to_delete.append(row["id"])
            affected_game_ids.add(row["game_id"])
        elif new_drop != row["eval_drop"] or new_severity != row["severity"]:
            conn.execute(
                "UPDATE mistakes SET eval_drop = ?, severity = ? WHERE id = ?",
                (new_drop, new_severity, row["id"]),
            )
            if new_severity != row["severity"]:
                conn.execute(
                    "UPDATE puzzles SET severity = ? WHERE mistake_id = ?",
                    (new_severity, row["id"]),
                )
            affected_game_ids.add(row["game_id"])

    if to_delete:
        placeholders = ",".join("?" * len(to_delete))
        # Same FK order as analyze_and_store_game()'s re-analysis cleanup
        # (puzzles before mistakes), extended to the SRS/attempt-history
        # tables that also reference puzzles.id and would otherwise violate
        # the foreign key once puzzles are deleted.
        conn.execute(
            f"DELETE FROM puzzle_review_state WHERE puzzle_id IN "
            f"(SELECT id FROM puzzles WHERE mistake_id IN ({placeholders}))",
            to_delete,
        )
        conn.execute(
            f"DELETE FROM puzzle_attempts WHERE puzzle_id IN "
            f"(SELECT id FROM puzzles WHERE mistake_id IN ({placeholders}))",
            to_delete,
        )
        conn.execute(
            f"DELETE FROM puzzle_progress WHERE puzzle_id IN "
            f"(SELECT id FROM puzzles WHERE mistake_id IN ({placeholders}))",
            to_delete,
        )
        conn.execute(f"DELETE FROM puzzles WHERE mistake_id IN ({placeholders})", to_delete)
        conn.execute(f"DELETE FROM mistakes WHERE id IN ({placeholders})", to_delete)

    move_rows = conn.execute(
        "SELECT id, game_id, eval_before_cp, eval_drop, tier FROM game_moves"
    ).fetchall()
    for row in move_rows:
        if row["eval_before_cp"] is None or row["eval_drop"] is None:
            continue
        # The old code stored eval_drop = eval_before_cp - eval_after_cp
        # (plain, uncapped subtraction), so eval_after_cp is exactly
        # recoverable from the two values already on disk.
        eval_after_cp = row["eval_before_cp"] - row["eval_drop"]
        new_drop = capped_eval_drop(row["eval_before_cp"], eval_after_cp)
        new_tier = classify_tier(new_drop)
        if new_drop != row["eval_drop"] or new_tier != row["tier"]:
            conn.execute(
                "UPDATE game_moves SET eval_drop = ?, tier = ? WHERE id = ?",
                (new_drop, new_tier, row["id"]),
            )
            affected_game_ids.add(row["game_id"])

    if affected_game_ids:
        # Bumping analyzed_at invalidates any cached Game Report for these
        # games (generate_game_report's computed_at >= analyzed_at
        # freshness check) so the next view recomputes the enriched
        # classification from the now-corrected game_moves.tier, without
        # this migration needing to re-run Stockfish itself.
        placeholders = ",".join("?" * len(affected_game_ids))
        conn.execute(
            f"UPDATE games SET analyzed_at = datetime('now') WHERE id IN ({placeholders})",
            list(affected_game_ids),
        )


def _migration_018_time_control_adjusted_rating(conn: sqlite3.Connection) -> None:
    """Feature — game_report.estimate_performance_rating() previously fed a
    game's raw ACPL through a single rating curve regardless of time
    control, even though the same player's ACPL runs measurably higher at
    faster time controls purely from time pressure, not weaker play.
    game_report.py now divides ACPL by a per-time-control factor before
    the rating lookup (see ACPL_TIME_CONTROL_DIVISOR), and also reports a
    rough USCF-equivalent figure alongside the existing estimate.

    Recomputes estimated_rating (and the summary sentence that quotes it)
    for every already-cached game_reports row from data already on disk —
    game_moves.eval_drop for the ACPL and games.time_control — without
    re-running Stockfish (compute_enriched_classification, which the
    cached tier_counts/phase accuracy already depend on, isn't touched by
    this change, so there's no need to invalidate those).

    Self-contained rather than importing game_report.py for the same
    reason migration 17 inlines mistakes.py's logic: a migration should
    encode a fixed, point-in-time transformation, not a live dependency on
    application code that could change under it later.
    """
    ACPL_RATING_ANCHORS = [
        (10, 2700), (20, 2400), (35, 2200), (50, 2000), (70, 1800),
        (100, 1600), (140, 1400), (190, 1200), (250, 1000), (350, 800), (500, 600),
    ]
    ACPL_TIME_CONTROL_DIVISOR = {
        "bullet": 1.5, "blitz": 1.25, "rapid": 1.1, "classical": 1.0, "daily": 1.0,
    }
    USCF_RATING_OFFSET = 100

    def estimate_performance_rating(acpl):
        anchors = ACPL_RATING_ANCHORS
        if acpl <= anchors[0][0]:
            return anchors[0][1]
        if acpl >= anchors[-1][0]:
            return anchors[-1][1]
        for (acpl_lo, rating_lo), (acpl_hi, rating_hi) in zip(anchors, anchors[1:]):
            if acpl_lo <= acpl <= acpl_hi:
                frac = (acpl - acpl_lo) / (acpl_hi - acpl_lo)
                return round(rating_lo + frac * (rating_hi - rating_lo))
        return anchors[-1][1]

    def build_summary(accuracy, rating, rating_uscf, tier_counts, phase_accuracy):
        if accuracy is None:
            return "Not enough analyzed moves to summarize this game."
        parts = [f"You played this game at {accuracy}% accuracy"]
        parts[0] += f", roughly the move quality of a {rating}-rated player (about {rating_uscf} USCF)." if rating else "."
        brilliant = tier_counts.get("brilliant", 0)
        if brilliant:
            parts.append(f"You found {brilliant} brilliant move{'s' if brilliant != 1 else ''}.")
        great = tier_counts.get("great", 0)
        if great:
            parts.append(f"{great} great move{'s' if great != 1 else ''} held the position together in a sharp moment.")
        miss = tier_counts.get("miss", 0)
        if miss:
            parts.append(f"You missed {miss} winning tactic{'s' if miss != 1 else ''} — worth reviewing in Puzzles.")
        present = {p: a for p, a in phase_accuracy.items() if a is not None}
        if len(present) > 1:
            weakest = min(present, key=present.get)
            parts.append(f"Your {weakest} was the weakest phase this game ({present[weakest]}% accuracy).")
        return " ".join(parts)

    rows = conn.execute(
        """
        SELECT gr.game_id, gr.estimated_rating, gr.accuracy_overall, gr.accuracy_opening,
               gr.accuracy_middlegame, gr.accuracy_endgame, gr.tier_counts,
               g.time_control, g.color
        FROM game_reports gr JOIN games g ON g.id = gr.game_id
        """
    ).fetchall()

    for row in rows:
        move_rows = conn.execute(
            "SELECT eval_drop FROM game_moves WHERE game_id = ? AND color_moved = ? AND eval_drop IS NOT NULL",
            (row["game_id"], row["color"]),
        ).fetchall()
        drops = [m["eval_drop"] for m in move_rows]
        acpl = sum(drops) / len(drops) if len(drops) >= 2 else None

        if acpl is None:
            new_rating = None
        else:
            divisor = ACPL_TIME_CONTROL_DIVISOR.get(row["time_control"], 1.0)
            new_rating = estimate_performance_rating(acpl / divisor)

        if new_rating == row["estimated_rating"]:
            continue

        new_rating_uscf = max(0, new_rating - USCF_RATING_OFFSET) if new_rating is not None else None
        phase_accuracy = {
            "opening": row["accuracy_opening"],
            "middlegame": row["accuracy_middlegame"],
            "endgame": row["accuracy_endgame"],
        }
        summary = build_summary(
            row["accuracy_overall"], new_rating, new_rating_uscf,
            json.loads(row["tier_counts"]), phase_accuracy,
        )
        conn.execute(
            "UPDATE game_reports SET estimated_rating = ?, summary = ? WHERE game_id = ?",
            (new_rating, summary, row["game_id"]),
        )


def _migration_019_lichess_move_judgment(conn: sqlite3.Connection) -> None:
    """Feature — mistakes.py's severity/tier classification (Inaccuracy/
    Mistake/Blunder, and the finer Best/Excellent/Good bands) used flat
    centipawn thresholds; a flat threshold doesn't know the difference
    between a real swing and one that doesn't change who's winning, the
    same class of problem migration 17 fixed a cruder, narrower version of
    for mate-adjacent swings specifically. mistakes.py now ports Lichess's
    own move-judgment algorithm directly (win-probability-based, see
    https://github.com/lichess-org/lila/blob/master/modules/tree/src/main/Advice.scala) —
    this migration re-derives severity/tier for every already-analyzed
    game's stored rows from the raw evals already on disk, without
    re-running Stockfish.

    Approximation for this backfill only: mistakes.py's real
    classify_severity/classify_tier also special-case a move that
    specifically creates or loses a forced mate (Lichess's own MateAdvice),
    using the exact raw eval right before/after that transition. Only
    eval_before_cp is stored exactly for game_moves; eval_after has to be
    recovered as capped_before - eval_drop, which is exact for the ordinary
    win%-based case (both were already clamped to +-ACCURACY_EVAL_CAP_CP
    before that subtraction produced eval_drop in the first place) but
    loses whether the *raw* after-value was itself mate-scale. Skipping the
    MateAdvice special case for this backfill and using only the ordinary
    win%-based formula still converges on the same practical verdict for
    the common, clear-cut cases (a real winning-to-mated collapse already
    saturates the win% scale hard enough to register as a blunder either
    way) — the only rows this doesn't perfectly reconstruct are already-
    near-decided positions that specifically create or lose a forced mate,
    a narrow edge case. New analyses going forward use the real, exact
    logic in mistakes.py; only this one-time backfill approximates.

    mistakes rows are keyed on eval_before/eval_after (both stored exactly,
    mover's-own-perspective, uncapped) — no approximation needed there.
    """
    WIN_PERCENT_MULTIPLIER = -0.00368208
    WIN_PERCENT_CP_CEILING = 1000
    ACCURACY_EVAL_CAP_CP = 1000
    INACCURACY_WIN_PERCENT_LOSS = 5.0
    MISTAKE_WIN_PERCENT_LOSS = 10.0
    BLUNDER_WIN_PERCENT_LOSS = 15.0
    BEST_WIN_PERCENT_LOSS = 1.0
    EXCELLENT_WIN_PERCENT_LOSS = 2.5

    def win_percent(cp):
        ceiled = max(-WIN_PERCENT_CP_CEILING, min(WIN_PERCENT_CP_CEILING, cp))
        return 100.0 / (1.0 + math.exp(WIN_PERCENT_MULTIPLIER * ceiled))

    def severity_from_win_percent_loss(loss):
        if loss >= BLUNDER_WIN_PERCENT_LOSS:
            return "blunder"
        if loss >= MISTAKE_WIN_PERCENT_LOSS:
            return "mistake"
        if loss >= INACCURACY_WIN_PERCENT_LOSS:
            return "inaccuracy"
        return None

    def tier_from_win_percent_loss(loss):
        loss = max(0.0, loss)
        severity = severity_from_win_percent_loss(loss)
        if severity is not None:
            return severity
        if loss >= EXCELLENT_WIN_PERCENT_LOSS:
            return "good"
        if loss >= BEST_WIN_PERCENT_LOSS:
            return "excellent"
        return "best"

    affected_game_ids = set()

    mistake_rows = conn.execute(
        "SELECT id, game_id, eval_before, eval_after, severity FROM mistakes"
    ).fetchall()
    to_delete = []
    for row in mistake_rows:
        if row["eval_before"] is None or row["eval_after"] is None:
            continue
        loss = win_percent(row["eval_before"]) - win_percent(row["eval_after"])
        new_severity = severity_from_win_percent_loss(loss)
        if new_severity is None:
            to_delete.append(row["id"])
            affected_game_ids.add(row["game_id"])
        elif new_severity != row["severity"]:
            conn.execute("UPDATE mistakes SET severity = ? WHERE id = ?", (new_severity, row["id"]))
            conn.execute("UPDATE puzzles SET severity = ? WHERE mistake_id = ?", (new_severity, row["id"]))
            affected_game_ids.add(row["game_id"])

    if to_delete:
        # Same FK order as migration 17's re-analysis cleanup.
        placeholders = ",".join("?" * len(to_delete))
        conn.execute(
            f"DELETE FROM puzzle_review_state WHERE puzzle_id IN "
            f"(SELECT id FROM puzzles WHERE mistake_id IN ({placeholders}))",
            to_delete,
        )
        conn.execute(
            f"DELETE FROM puzzle_attempts WHERE puzzle_id IN "
            f"(SELECT id FROM puzzles WHERE mistake_id IN ({placeholders}))",
            to_delete,
        )
        conn.execute(
            f"DELETE FROM puzzle_progress WHERE puzzle_id IN "
            f"(SELECT id FROM puzzles WHERE mistake_id IN ({placeholders}))",
            to_delete,
        )
        conn.execute(f"DELETE FROM puzzles WHERE mistake_id IN ({placeholders})", to_delete)
        conn.execute(f"DELETE FROM mistakes WHERE id IN ({placeholders})", to_delete)

    move_rows = conn.execute(
        "SELECT id, game_id, eval_before_cp, eval_drop, tier, classification FROM game_moves"
    ).fetchall()
    reenrichment_needed = set()
    for row in move_rows:
        if row["eval_before_cp"] is None or row["eval_drop"] is None:
            continue
        capped_before = max(-ACCURACY_EVAL_CAP_CP, min(ACCURACY_EVAL_CAP_CP, row["eval_before_cp"]))
        capped_after = capped_before - row["eval_drop"]
        loss = win_percent(capped_before) - win_percent(capped_after)
        new_tier = tier_from_win_percent_loss(loss)
        if new_tier != row["tier"]:
            conn.execute("UPDATE game_moves SET tier = ? WHERE id = ?", (new_tier, row["id"]))
            affected_game_ids.add(row["game_id"])
            if row["classification"] is not None:
                reenrichment_needed.add(row["game_id"])

    if reenrichment_needed:
        # classification (the enriched Brilliant/Great/Book/Miss layer) is
        # derived from tier plus a Stockfish MultiPV=2 pass this migration
        # can't afford to re-run for every affected game at startup — clear
        # the stale enrichment so compute_enriched_classification recomputes
        # it lazily, on-demand, next time that game's Report page is viewed
        # (same "worth it on-demand, not in bulk" tradeoff as elsewhere).
        placeholders = ",".join("?" * len(reenrichment_needed))
        conn.execute(
            f"UPDATE game_moves SET classification = NULL, is_top_choice = NULL "
            f"WHERE game_id IN ({placeholders})",
            list(reenrichment_needed),
        )
        conn.execute(f"DELETE FROM game_reports WHERE game_id IN ({placeholders})", list(reenrichment_needed))

    if affected_game_ids:
        placeholders = ",".join("?" * len(affected_game_ids))
        conn.execute(
            f"UPDATE games SET analyzed_at = datetime('now') WHERE id IN ({placeholders})",
            list(affected_game_ids),
        )


def _migration_020_lichess_game_phase_divider(conn: sqlite3.Connection) -> None:
    """Feature — mistakes.py's opening/middlegame/endgame phase detection
    used a flat "move <= 10 is opening" cutoff and an endgame threshold
    counting ALL remaining pieces including pawns; a piece-down endgame
    with most pawns still on the board never crossed that threshold, so it
    was never tagged "endgame" at all. analysis.py now ports Lichess's own
    Divider algorithm instead — a positional detector (material thinned to
    <=10 majors/minors excluding pawns, back ranks emptied out, or a
    "mixedness" score crossing a threshold for the opening->middlegame
    transition; <=6 majors/minors for middlegame->endgame) computed once
    per game from the actual sequence of positions:
    https://github.com/lichess-org/scalachess/blob/master/core/src/main/scala/Divider.scala

    Recomputes phase for every already-analyzed game's mistakes/game_moves
    rows by replaying each game's stored PGN with python-chess — no
    Stockfish needed, since phase only depends on piece positions, not
    evals.

    Unlike migrations 17-19, this DOES import from analysis.py rather than
    inlining a frozen copy: analysis.py has no dependency on db.py (its
    only imports are io/logging/chess/stockfish/config, and config.py
    itself doesn't touch db.py either), so — unlike stats.py/mistakes.py,
    which import `from db import get_connection` and triggered a
    circular-import failure when tried here (see migration 17's own
    comment) — there is no partially-initialized-module risk. Reusing the
    real implementation avoids a third hand-transcription of a fairly
    intricate positional-scoring algorithm, which would only add another
    place for the two copies to silently drift apart.
    """
    from analysis import _compute_phase_boundaries, _is_endgame_position, _is_midgame_position

    game_rows = conn.execute("SELECT id, pgn FROM games WHERE analyzed = 1 AND pgn IS NOT NULL").fetchall()
    affected_game_ids = set()

    for game_row in game_rows:
        try:
            game = chess.pgn.read_game(io.StringIO(game_row["pgn"]))
        except Exception:
            continue
        if game is None:
            continue

        board = game.board()
        midgame_flags, endgame_flags = [], []
        node = game
        while node.variations:
            next_node = node.variations[0]
            board.push(next_node.move)
            midgame_flags.append(_is_midgame_position(board))
            endgame_flags.append(_is_endgame_position(board))
            node = next_node

        middle_ply, end_ply = _compute_phase_boundaries(midgame_flags, endgame_flags)

        def phase_for(ply: int) -> str:
            if middle_ply is not None and ply < middle_ply:
                return "opening"
            if end_ply is not None and ply >= end_ply:
                return "endgame"
            if middle_ply is None:
                return "opening"
            return "middlegame"

        move_rows = conn.execute(
            "SELECT ply, phase FROM game_moves WHERE game_id = ?", (game_row["id"],)
        ).fetchall()
        for m in move_rows:
            new_phase = phase_for(m["ply"])
            if new_phase != m["phase"]:
                conn.execute(
                    "UPDATE game_moves SET phase = ? WHERE game_id = ? AND ply = ?",
                    (new_phase, game_row["id"], m["ply"]),
                )
                affected_game_ids.add(game_row["id"])

        mistake_rows = conn.execute(
            "SELECT id, ply, phase FROM mistakes WHERE game_id = ?", (game_row["id"],)
        ).fetchall()
        for m in mistake_rows:
            if m["ply"] is None:
                continue
            new_phase = phase_for(m["ply"])
            if new_phase != m["phase"]:
                conn.execute("UPDATE mistakes SET phase = ? WHERE id = ?", (new_phase, m["id"]))
                affected_game_ids.add(game_row["id"])

    if affected_game_ids:
        # Bumping analyzed_at invalidates any cached Game Report for these
        # games (generate_game_report's computed_at >= analyzed_at
        # freshness check), since accuracy_opening/middlegame/endgame are
        # phase-based aggregates that are now stale for them.
        placeholders = ",".join("?" * len(affected_game_ids))
        conn.execute(
            f"UPDATE games SET analyzed_at = datetime('now') WHERE id IN ({placeholders})",
            list(affected_game_ids),
        )


def _migration_021_real_uscf_conversion_formula(conn: sqlite3.Connection) -> None:
    """Fix — game_report._uscf_from_estimated_rating() used this project's
    own guess (a flat "-100", on the assumption US Chess ratings run below
    FIDE-ish ones for the same strength) instead of US Chess's own real,
    published conversion formula — which runs the *opposite* direction
    (US Chess ratings come out HIGHER than the FIDE-ish input, matching
    every rule of thumb US Chess has ever published, pre- or post-2024):
    https://new.uschess.org/civicrm/mailing/view?id=4405

    estimated_rating itself is untouched by this fix (only the USCF figure
    derived from it), and that figure is computed on the fly at read time
    (_report_row_to_dict), never stored — so the only stale data is the
    USCF number already baked into each cached game_reports.summary
    sentence. Recomputes just that sentence for every cached report,
    without bumping games.analyzed_at (unlike migrations 18-20): nothing
    here depends on Stockfish or invalidates the accuracy/tier data those
    migrations were correcting, so forcing a full report recompute would
    only cost time for no benefit.
    """
    USCF_CONVERSION_BREAKPOINT = 2000
    USCF_CONVERSION_LOW = (932, 0.564)
    USCF_CONVERSION_HIGH = (20, 1.02)

    def uscf_from_estimated_rating(estimated_rating):
        if estimated_rating is None:
            return None
        base, slope = (
            USCF_CONVERSION_LOW if estimated_rating <= USCF_CONVERSION_BREAKPOINT else USCF_CONVERSION_HIGH
        )
        return max(0, round(base + slope * estimated_rating))

    def build_summary(accuracy, rating, rating_uscf, tier_counts, phase_accuracy):
        if accuracy is None:
            return "Not enough analyzed moves to summarize this game."
        parts = [f"You played this game at {accuracy}% accuracy"]
        parts[0] += f", roughly the move quality of a {rating}-rated player (about {rating_uscf} USCF)." if rating else "."
        brilliant = tier_counts.get("brilliant", 0)
        if brilliant:
            parts.append(f"You found {brilliant} brilliant move{'s' if brilliant != 1 else ''}.")
        great = tier_counts.get("great", 0)
        if great:
            parts.append(f"{great} great move{'s' if great != 1 else ''} held the position together in a sharp moment.")
        miss = tier_counts.get("miss", 0)
        if miss:
            parts.append(f"You missed {miss} winning tactic{'s' if miss != 1 else ''} — worth reviewing in Puzzles.")
        present = {p: a for p, a in phase_accuracy.items() if a is not None}
        if len(present) > 1:
            weakest = min(present, key=present.get)
            parts.append(f"Your {weakest} was the weakest phase this game ({present[weakest]}% accuracy).")
        return " ".join(parts)

    rows = conn.execute(
        """
        SELECT game_id, estimated_rating, accuracy_overall, accuracy_opening,
               accuracy_middlegame, accuracy_endgame, tier_counts
        FROM game_reports WHERE estimated_rating IS NOT NULL
        """
    ).fetchall()

    for row in rows:
        phase_accuracy = {
            "opening": row["accuracy_opening"],
            "middlegame": row["accuracy_middlegame"],
            "endgame": row["accuracy_endgame"],
        }
        summary = build_summary(
            row["accuracy_overall"], row["estimated_rating"],
            uscf_from_estimated_rating(row["estimated_rating"]),
            json.loads(row["tier_counts"]), phase_accuracy,
        )
        conn.execute(
            "UPDATE game_reports SET summary = ? WHERE game_id = ?",
            (summary, row["game_id"]),
        )


def _migration_022_coaching_report(conn: sqlite3.Connection) -> None:
    """Feature — the coaching-style batch report (bulk PGN ingestion +
    cross-game pattern mining, see coaching_report.py). Three additions:

    - game_moves.multipv_lines: the top-4 engine candidates at that
      position (same JSON-blob shape as puzzles.top_lines), from a
      dedicated MultiPV-4 pass coaching_report.py runs on demand — kept
      off game_moves' normal population path (mistakes.analyze_and_store_game)
      the same way game_report.py's own MultiPV=2 enrichment already is,
      so routine sync speed is unaffected.
    - game_moves.is_drift_candidate: a move whose position had multiple
      engine-close options (per multipv_lines) but wasn't the top choice,
      while still not being bad enough to already be a mistake/blunder —
      the "individually fine, collectively a worse plan" pattern standard
      ACPL can't see. A separate boolean rather than a new `classification`
      value: unlike brilliant/great/book/miss (mutually-exclusive upgrades
      of one tier slot), a move can be both e.g. "good" tier and a drift
      candidate at once.
    - games.repertoire_structure: which of this player's known repertoire
      lines (Dragon, King's Indian, Grünfeld, London, ...) this game
      reached, tagged by coaching_report.repertoire.py from the PGN alone
      (no Stockfish needed) so it's cheap to compute once and reuse.
    - coaching_reports: one row per generated batch report, so a later
      batch can compare its own per-time-control drift rate against the
      profile's previous report (the whole point of tracking this over
      time, not just once).
    """
    existing_moves = {row["name"] for row in conn.execute("PRAGMA table_info(game_moves)")}
    for col_name, col_type in (("multipv_lines", "TEXT"), ("is_drift_candidate", "INTEGER")):
        if col_name not in existing_moves:
            conn.execute(f"ALTER TABLE game_moves ADD COLUMN {col_name} {col_type}")

    existing_games = {row["name"] for row in conn.execute("PRAGMA table_info(games)")}
    if "repertoire_structure" not in existing_games:
        conn.execute("ALTER TABLE games ADD COLUMN repertoire_structure TEXT")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS coaching_reports (
            id INTEGER PRIMARY KEY,
            profile_id INTEGER NOT NULL REFERENCES profiles(id),
            created_at TEXT NOT NULL,
            game_ids TEXT NOT NULL,
            time_control_stats TEXT NOT NULL,
            structure_stats TEXT NOT NULL,
            headline TEXT NOT NULL,
            full_report TEXT NOT NULL
        );
    """)


def _migration_023_drift_puzzles(conn: sqlite3.Connection) -> None:
    """Feature — turns a coaching report's highlighted drift positions
    into puzzles a solver can actually attempt, so the report assigns
    concrete practice rather than just describing a pattern (see
    coaching_report.create_drift_puzzles).

    Deliberately a separate table from the existing `puzzles`, not a
    relaxed version of it: `puzzles.mistake_id` is NOT NULL by design
    (every existing puzzle traces back to a real, objectively-bad flagged
    mistake), and a drift candidate is specifically NOT that — it's a
    close engine decision the played move wasn't wrong to make. Same
    reasoning `opening_puzzles` already documents for being its own table
    rather than shoehorned into `puzzles`.

    Self-contained attempt tracking (own attempts/correct counters)
    rather than wiring into the shared puzzle_attempts/puzzle_review_state/
    puzzle_progress SRS tables — those FK specifically to puzzles.id, and
    extending three heavily-used tables to a second, polymorphic source
    is a bigger, riskier change than this feature's first cut needs. A
    real scope limit, not an oversight: no spaced-repetition scheduling
    for these yet, unlike the main puzzle set.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS drift_puzzles (
            id INTEGER PRIMARY KEY,
            coaching_report_id INTEGER NOT NULL REFERENCES coaching_reports(id),
            game_id INTEGER NOT NULL REFERENCES games(id),
            ply INTEGER NOT NULL,
            fen_before TEXT NOT NULL,
            side_to_move TEXT NOT NULL,
            played_move_san TEXT NOT NULL,
            played_move_explanation TEXT,
            best_move_uci TEXT NOT NULL,
            best_move_san TEXT NOT NULL,
            best_move_explanation TEXT,
            top_lines TEXT NOT NULL,
            created_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            correct INTEGER NOT NULL DEFAULT 0,
            UNIQUE(game_id, ply)
        );
    """)


def _migration_024_self_calibrated_rating(conn: sqlite3.Connection) -> None:
    """Fix — game_report.estimate_performance_rating() previously fed ACPL
    through a fixed anchor table "calibrated by eye against commonly-cited
    ballpark ACPL ranges," confirmed grossly inflated in practice. Replaced
    with a linear fit of each profile's own real rating history
    (games.player_rating) against their own time-control-adjusted ACPL in
    their other analyzed+rated games — see game_report.py's own comment
    above estimate_performance_rating for why a universal table is the
    wrong shape for this in the first place. Below MIN_CALIBRATION_GAMES
    real data points this now returns None (no number) rather than a
    guess, so a profile without enough history yet simply won't show one.

    Self-contained rather than importing game_report.py, same reasoning
    migrations 17/18 already document: a migration should encode a fixed,
    point-in-time transformation, not a live dependency on application
    code that could change under it later.

    Recomputes estimated_rating (and the summary sentence that quotes it)
    for every already-cached game_reports row from data already on disk —
    game_moves.eval_drop for each game's ACPL, games.player_rating/
    time_control/profile_id for the calibration — without re-running
    Stockfish, same as migration 18.
    """
    ACPL_TIME_CONTROL_DIVISOR = {
        "bullet": 1.5, "blitz": 1.25, "rapid": 1.1, "classical": 1.0, "daily": 1.0,
    }
    MIN_CALIBRATION_GAMES = 8
    USCF_CONVERSION_BREAKPOINT = 2000
    USCF_CONVERSION_LOW = (932, 0.564)
    USCF_CONVERSION_HIGH = (20, 1.02)

    def adjusted_acpl(acpl, time_control):
        return acpl / ACPL_TIME_CONTROL_DIVISOR.get(time_control, 1.0)

    def fit_linear(pairs):
        n = len(pairs)
        mean_x = sum(x for x, _ in pairs) / n
        mean_y = sum(y for _, y in pairs) / n
        var_x = sum((x - mean_x) ** 2 for x, _ in pairs)
        if var_x == 0:
            return None
        cov_xy = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
        slope = cov_xy / var_x
        return slope, mean_y - slope * mean_x

    def estimate_rating(acpl, calibration):
        if calibration is None:
            return None
        pairs, mean_y = calibration
        fit = fit_linear(pairs)
        if fit is None or fit[0] > 0:
            return round(mean_y)
        slope, intercept = fit
        return max(0, round(intercept + slope * acpl))

    def uscf_from_estimated_rating(estimated_rating):
        if estimated_rating is None:
            return None
        base, slope = (
            USCF_CONVERSION_LOW if estimated_rating <= USCF_CONVERSION_BREAKPOINT else USCF_CONVERSION_HIGH
        )
        return max(0, round(base + slope * estimated_rating))

    def build_summary(accuracy, rating, rating_uscf, tier_counts, phase_accuracy):
        if accuracy is None:
            return "Not enough analyzed moves to summarize this game."
        parts = [f"You played this game at {accuracy}% accuracy"]
        parts[0] += (
            f", in line with a rating of about {rating} based on your own game history (~{rating_uscf} USCF)."
            if rating else "."
        )
        brilliant = tier_counts.get("brilliant", 0)
        if brilliant:
            parts.append(f"You found {brilliant} brilliant move{'s' if brilliant != 1 else ''}.")
        great = tier_counts.get("great", 0)
        if great:
            parts.append(f"{great} great move{'s' if great != 1 else ''} held the position together in a sharp moment.")
        miss = tier_counts.get("miss", 0)
        if miss:
            parts.append(f"You missed {miss} winning tactic{'s' if miss != 1 else ''} — worth reviewing in Puzzles.")
        present = {p: a for p, a in phase_accuracy.items() if a is not None}
        if len(present) > 1:
            weakest = min(present, key=present.get)
            parts.append(f"Your {weakest} was the weakest phase this game ({present[weakest]}% accuracy).")
        return " ".join(parts)

    # One (adjusted ACPL, real rating) point per rated+analyzed game,
    # grouped by profile — the calibration data, gathered once up front
    # rather than re-queried per report below.
    calibration_rows = conn.execute("""
        SELECT g.profile_id, g.time_control, g.player_rating, AVG(gm.eval_drop) AS acpl,
               COUNT(gm.eval_drop) AS n
        FROM games g
        JOIN game_moves gm ON gm.game_id = g.id AND gm.color_moved = g.color
        WHERE g.analyzed = 1 AND g.player_rating IS NOT NULL AND gm.eval_drop IS NOT NULL
        GROUP BY g.id
        HAVING n >= 2
    """).fetchall()

    pairs_by_profile: dict = {}
    for r in calibration_rows:
        pairs_by_profile.setdefault(r["profile_id"], []).append(
            (adjusted_acpl(r["acpl"], r["time_control"]), r["player_rating"])
        )
    calibration_by_profile = {
        profile_id: (pairs, sum(y for _, y in pairs) / len(pairs))
        for profile_id, pairs in pairs_by_profile.items()
        if len(pairs) >= MIN_CALIBRATION_GAMES
    }

    rows = conn.execute(
        """
        SELECT gr.game_id, gr.estimated_rating, gr.accuracy_overall, gr.accuracy_opening,
               gr.accuracy_middlegame, gr.accuracy_endgame, gr.tier_counts,
               g.time_control, g.color, g.profile_id
        FROM game_reports gr JOIN games g ON g.id = gr.game_id
        """
    ).fetchall()

    for row in rows:
        move_rows = conn.execute(
            "SELECT eval_drop FROM game_moves WHERE game_id = ? AND color_moved = ? AND eval_drop IS NOT NULL",
            (row["game_id"], row["color"]),
        ).fetchall()
        drops = [m["eval_drop"] for m in move_rows]
        acpl = sum(drops) / len(drops) if len(drops) >= 2 else None

        if acpl is None:
            new_rating = None
        else:
            new_rating = estimate_rating(
                adjusted_acpl(acpl, row["time_control"]), calibration_by_profile.get(row["profile_id"]),
            )

        if new_rating == row["estimated_rating"]:
            continue

        new_rating_uscf = uscf_from_estimated_rating(new_rating)
        phase_accuracy = {
            "opening": row["accuracy_opening"],
            "middlegame": row["accuracy_middlegame"],
            "endgame": row["accuracy_endgame"],
        }
        summary = build_summary(
            row["accuracy_overall"], new_rating, new_rating_uscf,
            json.loads(row["tier_counts"]), phase_accuracy,
        )
        conn.execute(
            "UPDATE game_reports SET estimated_rating = ?, summary = ? WHERE game_id = ?",
            (new_rating, summary, row["game_id"]),
        )


def _migration_025_drop_uscf_from_rating_estimate(conn: sqlite3.Connection) -> None:
    """Fix — migration 24 replaced the inflated ACPL-anchor rating guess
    with a fit against each profile's own real ONLINE rating history
    (Lichess/chess.com blitz/bullet/rapid), but kept running that new
    number through US Chess's real FIDE-to-USCF conversion formula in the
    summary sentence anyway. That formula converts a real FIDE (classical,
    over-the-board) rating into a US Chess one — both the SAME kind of
    serious, slow-time-control tournament rating. An online blitz/bullet
    rating isn't on that scale and isn't related to it by any published
    formula, so feeding one through the FIDE-to-USCF formula doesn't
    convert it to anything real; it just adds a misleading +200-400ish
    bump (e.g. 1425 -> "~1736 USCF"), confirmed as still grossly inflated
    immediately after migration 24 shipped. See game_report.py's own
    comment above estimate_performance_rating for the fuller explanation.

    estimated_rating itself is untouched (it was never the problem here —
    the USCF figure derived from it was) and was never stored anyway
    (computed at read time, same note migration 21 already made) — the
    only stale data is the "(~X USCF)" clause already baked into each
    cached game_reports.summary sentence. Rebuilds just that sentence
    from data already in the row, same targeted-recompute shape as
    migrations 21/24.
    """
    def build_summary(accuracy, rating, tier_counts, phase_accuracy):
        if accuracy is None:
            return "Not enough analyzed moves to summarize this game."
        parts = [f"You played this game at {accuracy}% accuracy"]
        parts[0] += f", in line with a rating of about {rating} based on your own game history." if rating else "."
        brilliant = tier_counts.get("brilliant", 0)
        if brilliant:
            parts.append(f"You found {brilliant} brilliant move{'s' if brilliant != 1 else ''}.")
        great = tier_counts.get("great", 0)
        if great:
            parts.append(f"{great} great move{'s' if great != 1 else ''} held the position together in a sharp moment.")
        miss = tier_counts.get("miss", 0)
        if miss:
            parts.append(f"You missed {miss} winning tactic{'s' if miss != 1 else ''} — worth reviewing in Puzzles.")
        present = {p: a for p, a in phase_accuracy.items() if a is not None}
        if len(present) > 1:
            weakest = min(present, key=present.get)
            parts.append(f"Your {weakest} was the weakest phase this game ({present[weakest]}% accuracy).")
        return " ".join(parts)

    rows = conn.execute(
        """
        SELECT game_id, estimated_rating, accuracy_overall, accuracy_opening,
               accuracy_middlegame, accuracy_endgame, tier_counts
        FROM game_reports
        """
    ).fetchall()

    for row in rows:
        phase_accuracy = {
            "opening": row["accuracy_opening"],
            "middlegame": row["accuracy_middlegame"],
            "endgame": row["accuracy_endgame"],
        }
        summary = build_summary(
            row["accuracy_overall"], row["estimated_rating"], json.loads(row["tier_counts"]), phase_accuracy,
        )
        conn.execute(
            "UPDATE game_reports SET summary = ? WHERE game_id = ?",
            (summary, row["game_id"]),
        )


def _migration_026_remove_estimated_rating(conn: sqlite3.Connection) -> None:
    """Fix — removed the per-game "estimated rating" feature entirely
    after two calibration attempts (migrations 24, 25) still produced
    numbers confirmed implausible on real data. The actual check that
    settled it: correlation between a game's own Lichess-formula accuracy%
    and this player's real historical rating, computed from their own
    analyzed games — -0.08 (bullet), 0.09 (blitz), -0.22 (rapid). That's
    statistical noise, not a relationship a per-game estimate could ever
    be calibrated to fit reliably, no matter the scheme. See
    game_report.py's module docstring for the fuller reasoning, and
    stats.py's FIDE Tournament Performance Rating for this app's actual
    rating figure — built from real game results, not move quality.

    Nulls out game_reports.estimated_rating (kept as a column rather than
    dropped — SQLite column drops are less battle-tested than additive
    changes, and nothing reads a NULL column) and rebuilds every cached
    summary without the rating clause, same targeted-recompute shape as
    migrations 21/24/25.
    """
    def build_summary(accuracy, tier_counts, phase_accuracy):
        if accuracy is None:
            return "Not enough analyzed moves to summarize this game."
        parts = [f"You played this game at {accuracy}% accuracy."]
        brilliant = tier_counts.get("brilliant", 0)
        if brilliant:
            parts.append(f"You found {brilliant} brilliant move{'s' if brilliant != 1 else ''}.")
        great = tier_counts.get("great", 0)
        if great:
            parts.append(f"{great} great move{'s' if great != 1 else ''} held the position together in a sharp moment.")
        miss = tier_counts.get("miss", 0)
        if miss:
            parts.append(f"You missed {miss} winning tactic{'s' if miss != 1 else ''} — worth reviewing in Puzzles.")
        present = {p: a for p, a in phase_accuracy.items() if a is not None}
        if len(present) > 1:
            weakest = min(present, key=present.get)
            parts.append(f"Your {weakest} was the weakest phase this game ({present[weakest]}% accuracy).")
        return " ".join(parts)

    rows = conn.execute(
        """
        SELECT game_id, accuracy_overall, accuracy_opening, accuracy_middlegame, accuracy_endgame, tier_counts
        FROM game_reports
        """
    ).fetchall()

    for row in rows:
        phase_accuracy = {
            "opening": row["accuracy_opening"],
            "middlegame": row["accuracy_middlegame"],
            "endgame": row["accuracy_endgame"],
        }
        summary = build_summary(row["accuracy_overall"], json.loads(row["tier_counts"]), phase_accuracy)
        conn.execute(
            "UPDATE game_reports SET estimated_rating = NULL, summary = ? WHERE game_id = ?",
            (summary, row["game_id"]),
        )


def _migration_027_puzzle_themes(conn: sqlite3.Connection) -> None:
    """Feature — tactical-motif themes on puzzles (fork, pin, skewer, mate
    in N, hanging piece, ...), so puzzles can be filtered by theme and hinted
    ("look for a fork") the way chess.com's themed puzzles are.

    Stored as a JSON list of theme keys. NULL means "not computed yet", not
    "no theme" — an empty list means the best move carries no named motif
    (most positional puzzles). Existing rows are backfilled by
    puzzles.backfill_puzzle_themes() (pure python-chess, no engine) on server
    start rather than in this migration, which shouldn't import live
    application code (same reasoning as migrations 17/18).
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(puzzles)")}
    if "themes" not in existing:
        conn.execute("ALTER TABLE puzzles ADD COLUMN themes TEXT")


def _migration_028_game_review_data(conn: sqlite3.Connection) -> None:
    """Feature — chess.com-style Game Review and Insights. Adds what a
    per-move review needs beyond the eval trace game_moves already has:

    - game_moves.best_move_uci / best_pv_uci / best_eval_cp / best_mate_in:
      the engine's best move (and line) in the position BEFORE each move,
      from the mover's perspective — what "Best was Nf3" and the coach's
      explanations are built from. Filled by the review pass
      (game_report.compute_enriched_classification), for both colors.
    - games.reviewed_at: when that pass last completed for the game.
    - games.termination_method: how it ended (checkmate, resignation,
      timeout, abandonment, stalemate, repetition, agreement, ...) —
      parsed from the PGN, no engine needed.
    - games.game_shape: chess.com-style shape (balanced, sharp, wild,
      giveaway, smooth, sudden, intense) derived from the eval curve.
    - games.book_plies: how many plies stayed in known opening theory.
    - game_tactics: one row per tactical opportunity found in a game
      (fork, pin, skewer, discovered attack, mate, free piece) with whether
      the mover found it, plus pieces left hanging — the raw material for
      Insights' found-vs-missed tactic stats, for both players.

    termination/shape/book_plies are NULL until game_meta.ensure_metadata()
    fills them (pure python-chess, no engine) — same "NULL = not computed
    yet" convention as puzzles.themes.
    """
    move_cols = {row["name"] for row in conn.execute("PRAGMA table_info(game_moves)")}
    for name, col_type in (("best_move_uci", "TEXT"), ("best_pv_uci", "TEXT"),
                           ("best_eval_cp", "REAL"), ("best_mate_in", "INTEGER")):
        if name not in move_cols:
            conn.execute(f"ALTER TABLE game_moves ADD COLUMN {name} {col_type}")

    game_cols = {row["name"] for row in conn.execute("PRAGMA table_info(games)")}
    for name, col_type in (("reviewed_at", "TEXT"), ("termination_method", "TEXT"),
                           ("game_shape", "TEXT"), ("book_plies", "INTEGER")):
        if name not in game_cols:
            conn.execute(f"ALTER TABLE games ADD COLUMN {name} {col_type}")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS game_tactics (
            id INTEGER PRIMARY KEY,
            game_id INTEGER NOT NULL REFERENCES games(id),
            ply INTEGER NOT NULL,
            color TEXT NOT NULL,
            motif TEXT NOT NULL,
            outcome TEXT NOT NULL,
            gain REAL,
            best_uci TEXT,
            played_uci TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_game_tactics_game ON game_tactics(game_id);
        CREATE INDEX IF NOT EXISTS idx_game_tactics_motif ON game_tactics(motif, outcome);
    """)


def _migration_029_opponent_countries(conn: sqlite3.Connection) -> None:
    """Feature — Insights geography. A cache of where each opponent says they
    are from, looked up from their public profile (geography.py) since games
    only store a username. `status` records what the lookup found so nothing
    is asked twice: 'ok' (country set), 'none' (no country / account gone) or
    'error' (transient failure, retried later)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS opponent_countries (
            source TEXT NOT NULL,
            username TEXT NOT NULL COLLATE NOCASE,
            country TEXT,
            status TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (source, username)
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
    (10, "Add users, sessions, oauth_states tables (Lichess OAuth)", _migration_010_users_and_sessions),
    (11, "Add user_id ownership to profiles/games/puzzles", _migration_011_user_ownership),
    (12, "Add puzzle_progress table (per-user SM-2 scheduling)", _migration_012_puzzle_progress_sm2),
    (13, "Add eco_codes table and games.eco column", _migration_013_eco_codes),
    (14, "Add analysis_status/analysis_task_id/analysis_error to games", _migration_014_analysis_status),
    (15, "Add game_moves classification/phase columns and game_reports table", _migration_015_game_reports),
    (16, "Fix Leitner SRS next_review_at datetime format", _migration_016_fix_leitner_srs_datetime_format),
    (17, "Fix missed-mate eval_drop/severity/tier miscalculation", _migration_017_fix_missed_mate_severity_and_tier),
    (18, "Recompute estimated_rating with time-control adjustment + USCF figure", _migration_018_time_control_adjusted_rating),
    (19, "Recompute move severity/tier with Lichess's own win%-based judgment", _migration_019_lichess_move_judgment),
    (20, "Recompute game phase with Lichess's own Divider algorithm", _migration_020_lichess_game_phase_divider),
    (21, "Recompute cached USCF figures with US Chess's real 2024 conversion formula", _migration_021_real_uscf_conversion_formula),
    (22, "Add coaching-report schema: game_moves MultiPV/drift columns, games.repertoire_structure, coaching_reports table", _migration_022_coaching_report),
    (23, "Add drift_puzzles table for practicing coaching-report highlights", _migration_023_drift_puzzles),
    (24, "Replace the inflated ACPL-anchor rating estimate with a per-profile self-calibrated fit",
     _migration_024_self_calibrated_rating),
    (25, "Drop the misapplied FIDE-to-USCF conversion from the self-calibrated rating estimate",
     _migration_025_drop_uscf_from_rating_estimate),
    (26, "Remove the per-game estimated-rating feature entirely (no real ACPL-vs-rating correlation)",
     _migration_026_remove_estimated_rating),
    (27, "Add puzzles.themes (tactical-motif tags for themed puzzles and hints)", _migration_027_puzzle_themes),
    (28, "Add game review data: best moves/lines per ply, termination, game shape, tactic events",
     _migration_028_game_review_data),
    (29, "Add opponent_countries (cached opponent country lookups for Insights geography)", _migration_029_opponent_countries),
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
    or None if there's no real DB file yet (fresh install — nothing to
    lose). Checks size, not just existence: run_migrations() opens a
    connection to DB_PATH before calling this, and sqlite3.connect()
    creates a 0-byte file as a side effect on a path that doesn't exist
    yet — without the size check, a fresh install's very first run would
    see DB_PATH "exist" by the time this runs and back up that empty file
    instead of skipping, as the fresh-install case is meant to.

    Runs a WAL checkpoint first — db.py's WAL mode (needed for concurrent
    writers: parallel batch analysis, Celery workers, the background
    refresh thread) means recently committed rows can live only in the
    -wal sidecar file rather than the main .db file; a plain copy of just
    DB_PATH could silently omit them from what's supposed to be the
    recovery point if the migration about to run goes wrong.
    """
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        return None

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

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
            except sqlite3.OperationalError as e:
                # batch_analyze.py spawns one worker process per core on
                # Windows (ProcessPoolExecutor's `spawn` method), and each
                # freshly imports db.py, which runs this same
                # run_migrations() at import time. In the narrow window
                # right after an upgrade — a migration genuinely pending
                # when a parallel batch run starts — more than one worker
                # can see the same column missing and both issue the same
                # ALTER TABLE. The loser isn't a real failure, just this
                # migration having already been applied by a sibling
                # process a moment earlier; treat it as done rather than
                # crashing that worker.
                if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                    conn.rollback()
                    logger.warning(
                        f"Migration {version} ({description}) appears to already be applied "
                        f"(likely a concurrent worker process got there first): {e}. Continuing."
                    )
                    _set_schema_version(conn, version)
                    conn.commit()
                    continue
                conn.rollback()
                restore_hint = f" Restore it from {backup_path}." if backup_path else ""
                logger.error(f"Migration {version} ({description}) failed: {e}.{restore_hint}")
                raise
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

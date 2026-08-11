"""
End-to-end integration test: runs the real pipeline (storage -> Stockfish
analysis -> mistake detection) against a small, fixed fixture PGN and
confirms it produces the expected stored game and mistake.

This is the one test that touches Stockfish and a real (temporary) SQLite
database — conftest.py points DB_PATH at a throwaway file before any app
module is imported, so this never touches real game history.

Requires a working Stockfish install, same as the app itself.
"""

from pathlib import Path

from db import save_games, get_connection
from mistakes import analyze_and_store_game

FIXTURE_PGN = (Path(__file__).parent / "fixtures" / "sample_game.pgn").read_text(encoding="utf-8")


def test_full_pipeline_stores_game_and_flags_the_blunder():
    game = {
        "source": "lichess",
        "source_game_id": "integration-test-fixture-1",
        "date": "2026-01-01T00:00:00+00:00",
        "opponent": "Bob",
        "result": "loss",
        "color": "white",
        "time_control": "blitz",
        "opening_name": "Latvian-ish nonsense",
        "pgn": FIXTURE_PGN,
    }

    result = save_games([game])
    assert result["inserted"] == 1

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM games WHERE source_game_id = ?", ("integration-test-fixture-1",)
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["source"] == "lichess"
    assert row["analyzed"] == 0

    flagged = analyze_and_store_game(row["id"], row["pgn"])
    assert flagged is not None

    # The known blunder: White's 4. Qxf6 trades a queen for a knight.
    blunders = [m for m in flagged if m["severity"] == "blunder"]
    assert any(
        m["move_san"] == "Qxf6" and m["color_moved"] == "white" and m["phase"] == "opening"
        for m in blunders
    ), f"expected Qxf6 to be flagged as a blunder, got: {flagged}"

    conn = get_connection()
    try:
        updated_row = conn.execute("SELECT analyzed FROM games WHERE id = ?", (row["id"],)).fetchone()
        stored_mistakes = conn.execute(
            "SELECT COUNT(*) as n FROM mistakes WHERE game_id = ?", (row["id"],)
        ).fetchone()
    finally:
        conn.close()

    assert updated_row["analyzed"] == 1
    assert stored_mistakes["n"] == len(flagged)


def test_rerunning_save_games_does_not_duplicate():
    game = {
        "source": "lichess",
        "source_game_id": "integration-test-fixture-2",
        "date": "2026-01-01T00:00:00+00:00",
        "opponent": "Bob",
        "result": "loss",
        "color": "white",
        "time_control": "blitz",
        "opening_name": "Latvian-ish nonsense",
        "pgn": FIXTURE_PGN,
    }
    first = save_games([game])
    assert first["inserted"] == 1

    second = save_games([game])
    assert second["inserted"] == 0
    assert second["skipped"] == 1

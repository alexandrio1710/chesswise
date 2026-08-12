"""
Advanced features, Section 7 — one-time backfill of player_rating/
opponent_rating for games stored before migration 6 existed.

Both Lichess and Chess.com already embed WhiteElo/BlackElo directly in
the PGN text this app stores — this is pure regex over data already on
disk, no network calls, no Stockfish. Safe to re-run: only touches rows
where player_rating is still NULL.
"""

import logging
import re

from db import get_connection

logger = logging.getLogger(__name__)

_ELO_RE = re.compile(r'\[(White|Black)Elo\s+"(\d+)"\]')


def extract_ratings(pgn_text: str) -> tuple[int | None, int | None]:
    """(white_elo, black_elo) parsed from PGN headers, or None for either
    that's missing/non-numeric (e.g. an unrated game may omit it, or use
    a placeholder like "?").
    """
    found = dict(_ELO_RE.findall(pgn_text))
    white = int(found["White"]) if "White" in found else None
    black = int(found["Black"]) if "Black" in found else None
    return white, black


def backfill_ratings() -> dict:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, color, pgn FROM games WHERE player_rating IS NULL AND skip_reason IS NULL"
        ).fetchall()

        updated = 0
        skipped = 0
        for row in rows:
            white_elo, black_elo = extract_ratings(row["pgn"])
            if row["color"] == "white":
                player_rating, opponent_rating = white_elo, black_elo
            else:
                player_rating, opponent_rating = black_elo, white_elo

            if player_rating is None and opponent_rating is None:
                skipped += 1
                continue

            conn.execute(
                "UPDATE games SET player_rating = ?, opponent_rating = ? WHERE id = ?",
                (player_rating, opponent_rating, row["id"]),
            )
            updated += 1

        conn.commit()
        return {"updated": updated, "skipped": skipped, "total_checked": len(rows)}
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = backfill_ratings()
    logger.info(f"Backfilled ratings for {result['updated']} games ({result['skipped']} had no rating data at all, {result['total_checked']} checked)")

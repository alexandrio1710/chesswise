"""
Web platform, Section 3 — one-time (or periodic) import of the standard ECO
reference data into the local `eco_codes` table.

Sourced from lichess-org/chess-openings (https://github.com/lichess-org/chess-openings),
the same dataset lichess.org itself uses to classify openings — five TSV
files (a.tsv..e.tsv, one per ECO volume A-E) rather than one, matching the
upstream project's own layout.

Run standalone: `python eco_import.py`. Safe to re-run — rows are keyed by
their move sequence (eco_codes.pgn is UNIQUE) and upserted, so re-running
after upstream publishes corrections just updates the affected rows.
"""

import logging
import re

import requests

from db import get_connection

logger = logging.getLogger(__name__)

_BASE_URL = "https://raw.githubusercontent.com/lichess-org/chess-openings/master/{volume}.tsv"
_VOLUMES = ("a", "b", "c", "d", "e")

# Matches move-number tokens like "1." or "12." so they can be stripped —
# the upstream pgn column is real movetext ("1. e4 c5 2. Nf3"), but
# eco.classify_game_opening() matches against a plain SAN sequence with no
# move numbers (what python-chess hands back from a parsed game).
_MOVE_NUMBER_RE = re.compile(r"\d+\.+")


def _clean_moves(raw_pgn: str) -> str:
    return " ".join(_MOVE_NUMBER_RE.sub("", raw_pgn).split())


def _parse_tsv(text: str) -> list[tuple[str, str, str]]:
    lines = text.strip().splitlines()
    rows = []
    for line in lines[1:]:  # skip header: eco\tname\tpgn
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        eco, name, raw_pgn = parts[0].strip(), parts[1].strip(), parts[2].strip()
        moves = _clean_moves(raw_pgn)
        if eco and name and moves:
            rows.append((eco, name, moves))
    return rows


def import_eco_codes() -> int:
    """Downloads all five ECO volumes and upserts them into `eco_codes`.
    Returns the total number of rows written.
    """
    all_rows: list[tuple[str, str, str]] = []
    for volume in _VOLUMES:
        url = _BASE_URL.format(volume=volume)
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        rows = _parse_tsv(resp.text)
        logger.info(f"Fetched {len(rows)} openings from {volume}.tsv")
        all_rows.extend(rows)

    conn = get_connection()
    try:
        for eco, name, moves in all_rows:
            conn.execute(
                """
                INSERT INTO eco_codes (eco, name, pgn, ply_count)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(pgn) DO UPDATE SET
                    eco = excluded.eco, name = excluded.name, ply_count = excluded.ply_count
                """,
                (eco, name, moves, len(moves.split())),
            )
        conn.commit()
    finally:
        conn.close()

    logger.info(f"Imported {len(all_rows)} ECO openings into eco_codes.")
    return len(all_rows)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import_eco_codes()

    # Classify any already-stored games now that reference data exists —
    # matters on a fresh install where games were fetched before this
    # script ever ran.
    from eco import backfill_missing_eco

    updated = backfill_missing_eco(batch_size=100_000)
    logger.info(f"Backfilled ECO classification for {updated} existing game(s).")

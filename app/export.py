"""
Advanced features, Section 11 — CSV/JSON export of raw stats/games.

Deliberately thin: games export reuses stats.search_games() exactly as
the Search page (Section 6) already does, so "export what I'm looking
at" and "what's on screen" are always the same query — no separate
export-specific filtering logic to keep in sync. CSV conversion is the
only new code here.
"""

import csv
import io

GAME_CSV_FIELDS = [
    "id", "source", "date", "opponent", "result", "color",
    "time_control", "opening_name", "mistake_count", "blunder_count",
]


def games_to_csv(games: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=GAME_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for g in games:
        writer.writerow(g)
    return buf.getvalue()

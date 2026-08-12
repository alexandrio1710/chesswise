"""
Advanced features, Section 9 — Multi-Profile Support.

No accounts, no login: a "profile" is just a name plus the Lichess/
Chess.com usernames that belong to it, so more than one person's data
(or the same person's separate accounts) can share one local database
without the app ever assuming there's only one "you". A username can
only be linked to one profile at a time — that's what routes an
incoming fetched game to the right profile automatically at save time.

Scope note: the profile filter is wired into the pages where "whose
data am I looking at" is the point — Dashboard, Insights, Clock, Search
(see stats.py/insights.py/clock_analysis.py's own `profile_id` params).
Puzzles, the Opening Explorer, the Endgame Trainer, and the Analyze board
deliberately keep working over the full local game pool regardless of
which profile is active, same as before this module existed.
"""

from db import get_connection


def _profile_usernames(conn, profile_id: int) -> list[dict]:
    return [
        dict(r) for r in conn.execute(
            "SELECT source, username FROM profile_usernames WHERE profile_id = ? ORDER BY source",
            (profile_id,),
        ).fetchall()
    ]


def list_profiles() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM profiles ORDER BY id").fetchall()
        profiles = [dict(r) for r in rows]
        for p in profiles:
            p["usernames"] = _profile_usernames(conn, p["id"])
            p["game_count"] = conn.execute(
                "SELECT COUNT(*) as n FROM games WHERE profile_id = ?", (p["id"],)
            ).fetchone()["n"]
        return profiles
    finally:
        conn.close()


def get_profile(profile_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["usernames"] = _profile_usernames(conn, profile_id)
        return d
    finally:
        conn.close()


def create_profile(name: str) -> dict:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO profiles (name, created_at) VALUES (?, datetime('now'))", (name,)
        )
        conn.commit()
        profile_id = cur.lastrowid
    finally:
        conn.close()
    return get_profile(profile_id)


def delete_profile(profile_id: int) -> None:
    """Deletes the profile and its username links. Games that belonged to
    it are NOT deleted — they fall back to profile_id NULL ("unassigned",
    same state as pre-Section-9 games), rather than destroying analyzed
    games and puzzles because a profile got removed.
    """
    conn = get_connection()
    try:
        conn.execute("UPDATE games SET profile_id = NULL WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM profile_usernames WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        conn.commit()
    finally:
        conn.close()


def link_username(profile_id: int, source: str, username: str) -> dict:
    """Claims a username for this profile. A username can only belong to
    one profile — linking it here first releases it from wherever it was
    before, so fixing a mistaken link doesn't need a separate unlink step.
    """
    username = username.lower()
    conn = get_connection()
    try:
        conn.execute("DELETE FROM profile_usernames WHERE source = ? AND username = ?", (source, username))
        conn.execute(
            "INSERT INTO profile_usernames (profile_id, source, username) VALUES (?, ?, ?)",
            (profile_id, source, username),
        )
        conn.commit()
    finally:
        conn.close()
    return get_profile(profile_id)


def unlink_username(profile_id: int, source: str, username: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM profile_usernames WHERE profile_id = ? AND source = ? AND username = ?",
            (profile_id, source, username.lower()),
        )
        conn.commit()
    finally:
        conn.close()


def get_profile_id_for_username(source: str, username: str) -> int | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT profile_id FROM profile_usernames WHERE source = ? AND username = ?",
            (source, username.lower()),
        ).fetchone()
        return row["profile_id"] if row else None
    finally:
        conn.close()


def resolve_profile_id(source: str, username: str) -> int:
    """The profile a fetch for this username should be stored under: the
    profile it's already linked to, or a brand-new profile named after
    the username (auto-claiming it) the first time it's ever fetched —
    so `cli.py fetch <username>` keeps working with zero setup, exactly
    like it did before profiles existed.
    """
    existing = get_profile_id_for_username(source, username)
    if existing is not None:
        return existing
    profile = create_profile(username)
    link_username(profile["id"], source, username)
    return profile["id"]


def default_profile_id() -> int | None:
    """The profile new manually-saved games (Section 5's Analyze board,
    which has no profile switcher of its own) fall under: whichever
    profile was created first, or None on a completely fresh install with
    no profiles yet — save_games() leaves profile_id NULL in that case.
    """
    conn = get_connection()
    try:
        row = conn.execute("SELECT id FROM profiles ORDER BY id LIMIT 1").fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def compare_profiles(profile_a: int, profile_b: int) -> dict:
    """Head-to-head: the same handful of headline numbers for two profiles
    side by side. This is NOT "games profile A played against profile B"
    — multi-profile games aren't matches between the profiles, they're
    just separate players' histories kept in one local database — it's a
    stats comparison, computed the normal profile-scoped way for each.
    """
    conn = get_connection()
    try:
        result = {}
        for key, pid in (("a", profile_a), ("b", profile_b)):
            profile = get_profile(pid)
            if profile is None:
                raise ValueError(f"Profile {pid} not found")

            game_row = conn.execute(
                """
                SELECT COUNT(*) as games,
                       SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END) as losses,
                       SUM(CASE WHEN result = 'draw' THEN 1 ELSE 0 END) as draws
                FROM games WHERE profile_id = ? AND analyzed = 1 AND skip_reason IS NULL
                """,
                (pid,),
            ).fetchone()
            mistake_row = conn.execute(
                """
                SELECT COUNT(*) as total, SUM(CASE WHEN severity = 'blunder' THEN 1 ELSE 0 END) as blunders
                FROM mistakes m JOIN games g ON m.game_id = g.id WHERE g.profile_id = ?
                """,
                (pid,),
            ).fetchone()
            latest_rating_row = conn.execute(
                "SELECT player_rating FROM games WHERE profile_id = ? AND player_rating IS NOT NULL "
                "ORDER BY date DESC LIMIT 1",
                (pid,),
            ).fetchone()

            games = game_row["games"] or 0
            result[key] = {
                "profile": profile,
                "games": games,
                "wins": game_row["wins"] or 0,
                "losses": game_row["losses"] or 0,
                "draws": game_row["draws"] or 0,
                "win_rate_pct": round((game_row["wins"] or 0) / games * 100, 1) if games else None,
                "avg_mistakes_per_game": round((mistake_row["total"] or 0) / games, 1) if games else None,
                "avg_blunders_per_game": round((mistake_row["blunders"] or 0) / games, 1) if games else None,
                "latest_rating": latest_rating_row["player_rating"] if latest_rating_row else None,
            }
        return result
    finally:
        conn.close()

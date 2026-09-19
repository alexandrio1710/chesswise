"""
Where your opponents are from — chess.com Insights' geography view.

Games only store an opponent's username, so a country has to be looked up from
that player's public profile (chess.com's public API, Lichess's user API). That
is one small request per distinct opponent, so it runs as a background job the
person starts on purpose, is throttled, and every answer is cached in
`opponent_countries` — including "no country set" and "account closed", so
nothing is asked twice. A lookup that failed for a transient reason
(`status = 'error'`) is retried after an hour.

Limits worth knowing: only opponents whose profile publishes a country can be
placed (many don't), and a country is whatever the player chose to show, not a
verified location.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from urllib.parse import quote

import countries
import insights
from db import get_connection
from insights import Filters

logger = logging.getLogger(__name__)

LOOKUP_DELAY_SECONDS = 0.3
RETRY_ERRORS_AFTER_SECONDS = 3600
FREQUENT_OPPONENT_MIN_GAMES = 2

job: dict = {"running": False, "total": 0, "done": 0, "failed": 0, "error": None}
_lock = threading.Lock()


def _fetch_country(source: str, username: str) -> tuple[str, str | None]:
    """(status, country code). status: 'ok' (country found), 'none' (looked up,
    no country — or the account is gone), 'error' (couldn't tell; retry later)."""
    from fetchers import USER_AGENT, _request_with_retry

    try:
        if source == "chesscom":
            resp = _request_with_retry("GET", f"https://api.chess.com/pub/player/{quote(username.lower())}",
                                       headers={"User-Agent": USER_AGENT}, timeout=15)
            if resp.status_code == 404:
                return "none", None
            if resp.status_code != 200:
                return "error", None
            code = (resp.json().get("country") or "").rstrip("/").rsplit("/", 1)[-1].upper()
        elif source == "lichess":
            resp = _request_with_retry("GET", f"https://lichess.org/api/user/{quote(username)}",
                                       headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=15)
            if resp.status_code == 404:
                return "none", None
            if resp.status_code != 200:
                return "error", None
            code = ((resp.json().get("profile") or {}).get("country") or "").upper()
        else:
            return "none", None
    except (ConnectionError, ValueError) as e:  # network exhausted its retries, or a non-JSON body
        logger.warning(f"Country lookup failed for {source}:{username}: {e}")
        return "error", None
    return ("ok", code) if code else ("none", None)


def _filter_sql(flt: Filters) -> tuple[str, tuple]:
    return insights._source_clause(flt.source, flt.profile_id, flt=flt)


def pending_opponents(flt: Filters) -> list[tuple[str, str]]:
    """Distinct (source, username) opponents in the filtered games that have
    no cached answer yet (or a stale error)."""
    where, params = _filter_sql(flt)
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - RETRY_ERRORS_AFTER_SECONDS))
    conn = get_connection()
    try:
        rows = conn.execute(
            f"SELECT DISTINCT g.source, g.opponent FROM games g "
            f"LEFT JOIN opponent_countries oc ON oc.source = g.source AND oc.username = g.opponent COLLATE NOCASE "
            f"WHERE g.analyzed = 1 AND g.skip_reason IS NULL AND g.opponent IS NOT NULL AND g.opponent != '' {where} "
            f"AND (oc.username IS NULL OR (oc.status = 'error' AND oc.fetched_at < ?))",
            (*params, cutoff)).fetchall()
    finally:
        conn.close()
    return [(r["source"], r["opponent"]) for r in rows]


def _store(source: str, username: str, status: str, country: str | None) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO opponent_countries (source, username, country, status, fetched_at) VALUES (?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(source, username) DO UPDATE SET country = excluded.country, status = excluded.status, fetched_at = excluded.fetched_at",
            (source, username, country, status))
        conn.commit()
    finally:
        conn.close()


def run_lookup(flt: Filters, delay: float = LOOKUP_DELAY_SECONDS) -> dict:
    """Look up every pending opponent, one at a time. Blocking — the server
    runs it on a background thread (start_lookup)."""
    todo = pending_opponents(flt)
    with _lock:
        job.update(running=True, total=len(todo), done=0, failed=0, error=None)
    try:
        for source, username in todo:
            status, country = _fetch_country(source, username)
            _store(source, username, status, country)
            with _lock:
                job["done"] += 1
                if status == "error":
                    job["failed"] += 1
            time.sleep(delay)
    except Exception as e:  # keep the job state honest rather than dying silently
        logger.exception("Opponent country lookup crashed")
        with _lock:
            job["error"] = str(e)
    finally:
        with _lock:
            job["running"] = False
    return dict(job)


def start_lookup(flt: Filters) -> bool:
    """Start the background lookup. False if one is already running or there
    is nothing left to look up."""
    with _lock:
        if job["running"]:
            return False
        job["running"] = True  # claim the slot before the thread starts
    if not pending_opponents(flt):
        with _lock:
            job["running"] = False
        return False
    threading.Thread(target=run_lookup, args=(flt,), daemon=True).start()
    return True


def report(flt: Filters) -> dict:
    """Results by opponent country plus the most frequent opponents, for the
    filtered games."""
    where, params = _filter_sql(flt)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"SELECT g.source, g.opponent, g.result, g.opponent_rating, oc.country, oc.status FROM games g "
            f"LEFT JOIN opponent_countries oc ON oc.source = g.source AND oc.username = g.opponent COLLATE NOCASE "
            f"WHERE g.analyzed = 1 AND g.skip_reason IS NULL AND g.opponent IS NOT NULL AND g.opponent != '' {where}",
            params).fetchall()
    finally:
        conn.close()

    def record(games: list) -> dict:
        wins = sum(1 for g in games if g["result"] == "win")
        draws = sum(1 for g in games if g["result"] == "draw")
        ratings = [g["opponent_rating"] for g in games if g["opponent_rating"]]
        return {"games": len(games), "wins": wins, "draws": draws, "losses": len(games) - wins - draws,
                "win_rate_pct": round(wins / len(games) * 100, 1) if games else None,
                "avg_opponent_rating": round(sum(ratings) / len(ratings)) if ratings else None}

    by_country: dict[str, list] = defaultdict(list)
    by_opponent: dict[tuple[str, str], list] = defaultdict(list)
    unresolved = set()
    no_country = set()
    for r in rows:
        key = (r["source"], r["opponent"].lower())
        by_opponent[key].append(r)
        if r["status"] == "ok" and r["country"]:
            by_country[r["country"]].append(r)
        elif r["status"] == "none":
            no_country.add(key)
        else:
            unresolved.add(key)

    country_rows = []
    for code, games in by_country.items():
        opponents = {(g["source"], g["opponent"].lower()) for g in games}
        country_rows.append({"code": code, "name": countries.country_name(code), "opponents": len(opponents), **record(games)})
    country_rows.sort(key=lambda c: (-c["games"], c["name"]))

    frequent = []
    for (source, _), games in by_opponent.items():
        if len(games) >= FREQUENT_OPPONENT_MIN_GAMES:
            country = next((g["country"] for g in games if g["status"] == "ok" and g["country"]), None)
            frequent.append({"opponent": games[0]["opponent"], "source": source, "country": country,
                             "country_name": countries.country_name(country) if country else None, **record(games)})
    frequent.sort(key=lambda o: (-o["games"], o["opponent"].lower()))

    with _lock:
        state = dict(job)
    return {
        "countries": country_rows,
        "frequent_opponents": frequent[:15],
        "opponents": len(by_opponent),
        "located": len({k for k in by_opponent if k not in unresolved and k not in no_country}),
        "no_country": len(no_country),
        "unresolved": len(unresolved),
        "job": state,
    }

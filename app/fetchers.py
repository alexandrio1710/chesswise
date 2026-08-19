"""
Stage 1 — Fetch games from Lichess and Chess.com and normalize them into a
common shape so the rest of the pipeline (storage, analysis, dashboard)
never needs to know which site a game came from.

Normalized game dict shape:
    {
        "source": "lichess" | "chesscom",
        "source_game_id": str,
        "date": str (ISO 8601, e.g. "2026-08-10T14:32:00"),
        "opponent": str,
        "result": "win" | "loss" | "draw",
        "color": "white" | "black",
        "time_control": "bullet" | "blitz" | "rapid" | "classical",
        "opening_name": str,
        "pgn": str,
        "player_rating": int | None,
        "opponent_rating": int | None,
    }
"""

import logging
import re
import time
from datetime import datetime, timezone

import requests

from config import API_BACKOFF_BASE_SECONDS, API_INTER_REQUEST_DELAY_SECONDS, API_MAX_RETRIES

logger = logging.getLogger(__name__)

# Chess.com's API docs ask clients to self-identify with a descriptive
# User-Agent so they can contact an app's maintainer if it's misbehaving,
# rather than just blocking it. Lichess doesn't require this but it's good
# practice there too, so the same header is sent to both.
USER_AGENT = "ChessMistakeTracker/1.0 (personal project)"

# Retry/backoff for transient failures (429 rate limits, 5xx, connection
# blips), and the pause between consecutive requests when pulling multiple
# Chess.com archive months in a loop. Tunable via .env — see config.py.
MAX_RETRIES = API_MAX_RETRIES
BACKOFF_BASE_SECONDS = API_BACKOFF_BASE_SECONDS
INTER_REQUEST_DELAY_SECONDS = API_INTER_REQUEST_DELAY_SECONDS

# One shared connection pool for every request this module makes (Lichess,
# Chess.com, and — via alerts.py/digest.py/eco_import.py reusing
# _request_with_retry — Discord and GitHub) rather than a fresh TCP+TLS
# handshake per call, which matters most for the Chess.com per-month
# archive loop that repeatedly hits the same host.
_session = requests.Session()


def _parse_retry_after(value: str | None) -> float | None:
    """Retry-After is allowed by HTTP to be either delta-seconds ("120")
    or an HTTP-date ("Wed, 21 Oct 2026 07:28:00 GMT") — float(value) raises
    ValueError on the date form, which previously crashed the whole fetch
    instead of just falling back to exponential backoff.
    """
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(value)
            return max(0.0, (dt - datetime.now(dt.tzinfo)).total_seconds())
        except (TypeError, ValueError):
            return None


def _request_with_retry(method: str, url: str, **kwargs) -> requests.Response:
    """requests.get/post with retry-with-backoff on 429s, 5xx, and
    transient network errors. Raises the underlying exception (or returns
    the last response) if every retry is exhausted, so callers still see a
    real failure rather than this silently swallowing a persistent outage.
    """
    kwargs.setdefault("timeout", 30)
    last_exception: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = _session.request(method, url, **kwargs)
        except requests.exceptions.RequestException as e:
            last_exception = e
            if attempt == MAX_RETRIES:
                break
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            logger.warning(f"Network error on {url} (attempt {attempt}/{MAX_RETRIES}): {e}. Retrying in {wait:.1f}s...")
            time.sleep(wait)
            continue

        retryable = resp.status_code == 429 or resp.status_code >= 500
        if retryable and attempt < MAX_RETRIES:
            wait = _parse_retry_after(resp.headers.get("Retry-After"))
            if wait is None:
                wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                f"{resp.status_code} on {url} (attempt {attempt}/{MAX_RETRIES}). Retrying in {wait:.1f}s..."
            )
            time.sleep(wait)
            continue

        return resp

    raise ConnectionError(
        f"Failed to reach {url} after {MAX_RETRIES} attempts: {last_exception}"
    ) from last_exception


# ---------------------------------------------------------------------------
# Lichess
# ---------------------------------------------------------------------------

def fetch_lichess_games(username: str, max_games: int = 20, since_ms: int | None = None) -> list[dict]:
    """Fetch the most recent games for `username` from Lichess, normalized.

    Uses the PGN export endpoint with clocks + opening metadata included.
    Returns an empty list (with a printed note) if the username doesn't
    exist on Lichess — treated the same as "zero games" rather than a crash,
    since a typo'd username is the most likely real-world cause.

    `since_ms`: only fetch games played after this Unix timestamp in
    milliseconds (Lichess API's own `since` filter — Stage C's --refresh
    uses this so an incremental run doesn't re-download the full history).
    `max_games` is ignored whenever `since_ms` is given — same "no cap on
    an incremental catch-up" contract fetch_chesscom_games already has for
    its own `since_epoch` path (see that function's docstring). Lichess's
    `max` param is a hard cap independent of `since`, so combining both
    used to mean: more than `max_games` new games since the last refresh
    silently truncated the fetch to the most recent `max_games` of them,
    and — since the next refresh's cutoff (db.get_latest_game_date) then
    advances to the newest of only those returned — permanently skipped
    the older ones in the gap rather than catching them up next time.
    """
    url = f"https://lichess.org/api/games/user/{username}"
    params = {
        "clocks": "true",
        "opening": "true",
        "pgnInJson": "false",
    }
    if since_ms is not None:
        params["since"] = since_ms
    else:
        params["max"] = max_games
    headers = {
        "Accept": "application/x-chess-pgn",
        "User-Agent": USER_AGENT,
    }
    resp = _request_with_retry("GET", url, params=params, headers=headers)
    if resp.status_code == 404:
        logger.info(f"Lichess user '{username}' not found (404) — treating as zero games.")
        return []
    resp.raise_for_status()

    pgn_blobs = _split_pgn_blobs(resp.text)
    games = []
    for pgn in pgn_blobs:
        game = _normalize_lichess_game(pgn, username)
        if game:
            games.append(game)
    return games


def _normalize_lichess_game(pgn: str, username: str) -> dict | None:
    tags = _parse_pgn_tags(pgn)
    if not tags:
        return None

    username_lower = username.lower()
    white = tags.get("White", "")
    black = tags.get("Black", "")

    if white.lower() == username_lower:
        color = "white"
        opponent = black
    elif black.lower() == username_lower:
        color = "black"
        opponent = white
    else:
        # Shouldn't happen for a user's own game feed, but don't crash.
        color = "white"
        opponent = black

    result_tag = tags.get("Result", "*")
    result = _lichess_result_to_outcome(result_tag, color)
    if result is None:
        # Aborted/unterminated game ("*", or anything not one of the three
        # recognized result tags) — nothing meaningful to store or count,
        # and every downstream stat assumes result is always win/loss/draw.
        return None

    site_id = tags.get("Site", "")
    source_game_id = site_id.rstrip("/").split("/")[-1] if site_id else tags.get("GameId", "")

    date_str = tags.get("UTCDate", "")
    time_str = tags.get("UTCTime", "")
    date_iso = _combine_lichess_datetime(date_str, time_str)

    time_control_seconds = tags.get("TimeControl", "")
    time_control = _classify_time_control_from_clock(time_control_seconds)

    opening_name = tags.get("Opening", "")

    white_elo = int(tags["WhiteElo"]) if tags.get("WhiteElo", "").isdigit() else None
    black_elo = int(tags["BlackElo"]) if tags.get("BlackElo", "").isdigit() else None
    player_rating, opponent_rating = (white_elo, black_elo) if color == "white" else (black_elo, white_elo)

    return {
        "source": "lichess",
        "source_game_id": source_game_id,
        "date": date_iso,
        "opponent": opponent,
        "result": result,
        "color": color,
        "time_control": time_control,
        "opening_name": opening_name,
        "pgn": pgn.strip(),
        "player_rating": player_rating,
        "opponent_rating": opponent_rating,
    }


def _lichess_result_to_outcome(result_tag: str, color: str) -> str | None:
    if result_tag == "1/2-1/2":
        return "draw"
    if result_tag == "1-0":
        return "win" if color == "white" else "loss"
    if result_tag == "0-1":
        return "win" if color == "black" else "loss"
    # "*" (aborted/unterminated) or anything else unrecognized — None
    # rather than a "draw" fallback, so the caller can skip storing/
    # counting a game with no real result instead of silently mixing it
    # into draw statistics.
    return None


def _combine_lichess_datetime(date_str: str, time_str: str) -> str:
    """Always returns either a valid ISO 8601 string or "" (never the raw,
    dot-separated PGN tag) — every caller that stores/compares this value
    (db.save_games, db.get_latest_game_date's MAX(date), insights.py's
    strftime()-based day-of-week/time-of-day breakdowns) assumes one of
    those two shapes. Returning the raw "2026.08.10" on a parse failure
    used to violate that: '.' (0x2E) sorts after '-' (0x2D), so a mix of
    ISO and dot-formatted dates for one source made MAX(date) pick the
    wrong "latest" game, and SQLite's strftime() returns NULL for a
    dot-formatted date, which crashed insights.py's day-of-week/time-of-day
    breakdowns outright for any game carrying one.
    """
    if not date_str:
        return ""
    try:
        dt = datetime.strptime(f"{date_str} {time_str}".strip(), "%Y.%m.%d %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        pass
    try:
        # UTCTime missing/malformed but the date itself is fine — still
        # worth a date-only ISO value rather than discarding it entirely.
        dt = datetime.strptime(date_str, "%Y.%m.%d")
        return dt.replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Chess.com
# ---------------------------------------------------------------------------

def fetch_chesscom_games(
    username: str,
    months_back: int = 2,
    max_games: int = 20,
    since_epoch: int | None = None,
) -> list[dict]:
    """Fetch the most recent games for `username` from Chess.com, normalized.

    Chess.com only exposes games grouped by month, so we pull the most
    recent `months_back` months, combine them, sort by end time descending,
    and take the top `max_games`.

    `since_epoch`: Unix timestamp (seconds) — if given, `months_back` /
    `max_games` are ignored and instead every archive month from then to
    now is fetched, filtered down to games newer than `since_epoch`, with
    no cap. Used by Stage C's --refresh for incremental fetches.
    """
    headers = {"User-Agent": USER_AGENT}

    archives_url = f"https://api.chess.com/pub/player/{username}/games/archives"
    resp = _request_with_retry("GET", archives_url, headers=headers)
    if resp.status_code == 404:
        logger.info(f"Chess.com user '{username}' not found (404) — treating as zero games.")
        return []
    resp.raise_for_status()
    archive_urls = resp.json().get("archives", [])

    if not archive_urls:
        return []

    if since_epoch is not None:
        since_month_key = datetime.fromtimestamp(since_epoch, tz=timezone.utc).strftime("%Y/%m")
        recent_archive_urls = [u for u in archive_urls if u.rstrip("/")[-7:] >= since_month_key]
    elif months_back > 0:
        recent_archive_urls = archive_urls[-months_back:]
    else:
        # archive_urls[-0:] is a Python slicing gotcha: -0 == 0, so it
        # would return every archive month ever played instead of none.
        recent_archive_urls = []

    raw_games = []
    for i, archive_url in enumerate(reversed(recent_archive_urls)):  # most recent month first
        if i > 0:
            time.sleep(INTER_REQUEST_DELAY_SECONDS)
        month_resp = _request_with_retry("GET", archive_url, headers=headers)
        month_resp.raise_for_status()
        raw_games.extend(month_resp.json().get("games", []))

    if since_epoch is not None:
        raw_games = [g for g in raw_games if g.get("end_time", 0) > since_epoch]
    raw_games.sort(key=lambda g: g.get("end_time", 0), reverse=True)
    if since_epoch is None:
        raw_games = raw_games[:max_games]

    games = []
    for raw in raw_games:
        game = _normalize_chesscom_game(raw, username)
        if game:
            games.append(game)
    return games


def _normalize_chesscom_game(raw: dict, username: str) -> dict | None:
    pgn = raw.get("pgn", "")
    if not pgn:
        return None

    username_lower = username.lower()
    white_info = raw.get("white", {})
    black_info = raw.get("black", {})
    white_name = white_info.get("username", "")
    black_name = black_info.get("username", "")

    if white_name.lower() == username_lower:
        color = "white"
        opponent = black_name
        my_result = white_info.get("result", "")
    elif black_name.lower() == username_lower:
        color = "black"
        opponent = white_name
        my_result = black_info.get("result", "")
    else:
        color = "white"
        opponent = black_name
        my_result = white_info.get("result", "")

    result = _chesscom_result_to_outcome(my_result)

    end_time = raw.get("end_time")
    date_iso = (
        datetime.fromtimestamp(end_time, tz=timezone.utc).isoformat()
        if end_time
        else ""
    )

    source_game_id = raw.get("uuid") or raw.get("url", "").rstrip("/").split("/")[-1]
    if not source_game_id:
        # No uuid and no usable url — nothing to dedupe on. Storing this
        # under an empty-string id would collide with every other game
        # missing both fields (db.save_games dedupes on (source,
        # source_game_id): INSERT OR IGNORE would silently drop every one
        # after the first as a false "duplicate"), so skip it instead.
        logger.warning("Chess.com game has neither a uuid nor a parseable url — skipping.")
        return None

    time_control = _classify_chesscom_time_control(raw.get("time_class", ""))

    opening_name = _extract_opening_from_pgn(pgn)

    # Normalize Chess.com's PGN clock format ([%clk 0:00:59.9]) to match
    # Lichess's PGN clock annotation style ([%clk 0:00:59]) so downstream
    # clock parsing (Stage 4+) can use one regex regardless of source.
    normalized_pgn = _normalize_chesscom_clock_format(pgn)

    player_rating, opponent_rating = (
        (white_info.get("rating"), black_info.get("rating")) if color == "white"
        else (black_info.get("rating"), white_info.get("rating"))
    )

    return {
        "source": "chesscom",
        "source_game_id": source_game_id,
        "date": date_iso,
        "opponent": opponent,
        "result": result,
        "color": color,
        "time_control": time_control,
        "opening_name": opening_name,
        "pgn": normalized_pgn.strip(),
        "player_rating": player_rating,
        "opponent_rating": opponent_rating,
    }


def _chesscom_result_to_outcome(result_code: str) -> str:
    if result_code == "win":
        return "win"
    if result_code in ("agreed", "repetition", "stalemate", "insufficient",
                       "50move", "timevsinsufficient"):
        return "draw"
    # checkmated, timeout, resigned, abandoned, etc.
    return "loss"


def _classify_chesscom_time_control(time_class: str) -> str:
    mapping = {
        "bullet": "bullet",
        "blitz": "blitz",
        "rapid": "rapid",
        "daily": "classical",
    }
    return mapping.get(time_class, time_class or "unknown")


def _extract_opening_from_pgn(pgn: str) -> str:
    match = re.search(r'\[ECOUrl\s+"([^"]+)"\]', pgn)
    if match:
        slug = match.group(1).rstrip("/").split("/")[-1]
        return slug.replace("-", " ")
    match = re.search(r'\[Opening\s+"([^"]+)"\]', pgn)
    return match.group(1) if match else ""


def _normalize_chesscom_clock_format(pgn: str) -> str:
    def _strip_fraction(m: re.Match) -> str:
        return f"[%clk {m.group(1)}]"

    return re.sub(r"\[%clk (\d+:\d{2}:\d{2})(?:\.\d+)?\]", _strip_fraction, pgn)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _split_pgn_blobs(pgn_text: str) -> list[str]:
    """Split a multi-game PGN export into individual game blobs.

    Normalizes CRLF to LF first: the split pattern requires two literal
    "\n"s directly before "[Event ", which a CRLF-terminated export
    ("\r\n\r\n") wouldn't contain at all — the whole response would
    silently become a single blob, and _parse_pgn_tags' dict-assignment
    tag parsing would then let each game's tags overwrite the previous
    one's, producing one Frankenstein row mixing one game's PGN text with
    a different game's metadata.
    """
    blobs = re.split(r"\n\n(?=\[Event )", pgn_text.replace("\r\n", "\n").strip())
    return [b for b in blobs if b.strip()]


def _parse_pgn_tags(pgn: str) -> dict:
    tags = {}
    for match in re.finditer(r'\[(\w+)\s+"([^"]*)"\]', pgn):
        tags[match.group(1)] = match.group(2)
    return tags


def _classify_time_control_from_clock(time_control_tag: str) -> str:
    """Classify Lichess TimeControl tag (e.g. '180+2') into a bucket using
    estimated game duration, matching Lichess's own speed categories.
    """
    if not time_control_tag or "+" not in time_control_tag:
        return "unknown"
    try:
        base, increment = time_control_tag.split("+")
        base, increment = int(base), int(increment)
    except ValueError:
        return "unknown"

    estimated_seconds = base + 40 * increment
    if estimated_seconds < 180:
        return "bullet"
    if estimated_seconds < 480:
        return "blitz"
    if estimated_seconds < 1500:
        return "rapid"
    return "classical"


if __name__ == "__main__":
    import sys

    lichess_user = sys.argv[1] if len(sys.argv) > 1 else None
    chesscom_user = sys.argv[2] if len(sys.argv) > 2 else None

    if lichess_user:
        print(f"\n=== Lichess games for '{lichess_user}' ===")
        lichess_games = fetch_lichess_games(lichess_user, max_games=5)
        print(f"Fetched {len(lichess_games)} games")
        for g in lichess_games:
            print(f"  {g['date']} | {g['color']} vs {g['opponent']} | "
                  f"{g['result']} | {g['time_control']} | {g['opening_name']} | "
                  f"id={g['source_game_id']}")

    if chesscom_user:
        print(f"\n=== Chess.com games for '{chesscom_user}' ===")
        chesscom_games = fetch_chesscom_games(chesscom_user, months_back=2, max_games=5)
        print(f"Fetched {len(chesscom_games)} games")
        for g in chesscom_games:
            print(f"  {g['date']} | {g['color']} vs {g['opponent']} | "
                  f"{g['result']} | {g['time_control']} | {g['opening_name']} | "
                  f"id={g['source_game_id']}")

    if not lichess_user and not chesscom_user:
        print("Usage: python fetchers.py <lichess_username> [chesscom_username]")
        print("       python fetchers.py \"\" <chesscom_username>   (chess.com only)")

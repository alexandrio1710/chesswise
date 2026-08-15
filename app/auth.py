"""
Web platform, Section 1 — Lichess OAuth2 (Authorization Code + PKCE) and
session management.

PKCE (not a client secret) proves this app is the same party that started
the login, which is what lets it run with zero secrets to leak — Lichess
issues OAuth apps a client_id only, no client_secret, for exactly this
reason (see https://lichess.org/api#tag/OAuth). Sessions are plain random
tokens: the raw token lives only in the user's browser cookie, and only its
SHA-256 hash is ever stored server-side, so a leaked database dump can't be
replayed as a login the way a stored raw token could.

Everything here is synchronous sqlite3 + requests, matching the rest of the
app (db.py, profiles.py, srs.py) rather than introducing an async ORM or an
OAuth client library for a two-endpoint flow.
"""

import base64
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests
from fastapi import Depends, HTTPException, Request

from config import (
    LICHESS_OAUTH_CLIENT_ID,
    LICHESS_OAUTH_REDIRECT_URI,
    LICHESS_OAUTH_SCOPES,
    SESSION_COOKIE_NAME,
    SESSION_TTL_DAYS,
)
from db import get_connection

logger = logging.getLogger(__name__)

LICHESS_AUTHORIZE_URL = "https://lichess.org/oauth"
LICHESS_TOKEN_URL = "https://lichess.org/api/token"
LICHESS_ACCOUNT_URL = "https://lichess.org/api/account"

# How long an oauth_states row is honored — bounds how long a login link
# stays usable, and doubles as the cutoff opportunistic cleanup uses.
_OAUTH_STATE_TTL_MINUTES = 10


# --- PKCE --------------------------------------------------------------------

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _generate_pkce_pair() -> tuple[str, str]:
    """Returns (code_verifier, code_challenge). The verifier is remembered
    server-side (oauth_states) between the redirect to Lichess and the
    callback; only its SHA-256 (the challenge) is ever sent to Lichess up
    front, per RFC 7636's S256 method.
    """
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def build_authorize_url() -> str:
    """Starts the login: generates and stores a (state, code_verifier)
    pair, returns the URL to redirect the browser to. `state` is a CSRF
    guard (the callback only proceeds if it comes back with a state this
    app itself generated) as well as the lookup key for the verifier.
    """
    state = secrets.token_urlsafe(24)
    verifier, challenge = _generate_pkce_pair()

    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM oauth_states WHERE created_at < datetime('now', ?)",
            (f"-{_OAUTH_STATE_TTL_MINUTES} minutes",),
        )
        conn.execute(
            "INSERT INTO oauth_states (state, code_verifier, created_at) VALUES (?, ?, datetime('now'))",
            (state, verifier),
        )
        conn.commit()
    finally:
        conn.close()

    params = {
        "response_type": "code",
        "client_id": LICHESS_OAUTH_CLIENT_ID,
        "redirect_uri": LICHESS_OAUTH_REDIRECT_URI,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
    }
    if LICHESS_OAUTH_SCOPES:
        params["scope"] = LICHESS_OAUTH_SCOPES
    return f"{LICHESS_AUTHORIZE_URL}?{urlencode(params)}"


def _consume_oauth_state(state: str) -> str:
    """Looks up and deletes the stored code_verifier for `state` — one-time
    use, so a captured/replayed callback URL can't be reused after the
    first exchange. Raises if the state is unknown or expired.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT code_verifier FROM oauth_states WHERE state = ? AND created_at >= datetime('now', ?)",
            (state, f"-{_OAUTH_STATE_TTL_MINUTES} minutes"),
        ).fetchone()
        conn.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
        conn.commit()
    finally:
        conn.close()

    if row is None:
        raise HTTPException(status_code=400, detail="OAuth state is invalid or expired. Please log in again.")
    return row["code_verifier"]


def exchange_code_for_token(code: str, state: str) -> str:
    verifier = _consume_oauth_state(state)
    resp = requests.post(
        LICHESS_TOKEN_URL,
        json={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": LICHESS_OAUTH_REDIRECT_URI,
            "client_id": LICHESS_OAUTH_CLIENT_ID,
            "code_verifier": verifier,
        },
        timeout=15,
    )
    if not resp.ok:
        logger.error(f"Lichess token exchange failed: {resp.status_code} {resp.text}")
        raise HTTPException(status_code=502, detail="Lichess rejected the login. Please try again.")
    return resp.json()["access_token"]


def fetch_lichess_account(access_token: str) -> dict:
    resp = requests.get(
        LICHESS_ACCOUNT_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=15,
    )
    if not resp.ok:
        logger.error(f"Lichess account fetch failed: {resp.status_code} {resp.text}")
        raise HTTPException(status_code=502, detail="Could not read your Lichess profile.")
    return resp.json()


# --- Users ---------------------------------------------------------------

def upsert_user(account: dict) -> dict:
    """Creates the user on first login, or updates their (mutable) display
    name/title on every subsequent one. Keyed on Lichess's account `id`,
    which — unlike the username — never changes even if the player renames.
    """
    lichess_id = account["id"]
    username = account["username"]
    email = account.get("email")
    title = account.get("title")

    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO users (lichess_id, username, email, lichess_title, created_at, last_login_at)
            VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(lichess_id) DO UPDATE SET
                username = excluded.username,
                email = COALESCE(excluded.email, users.email),
                lichess_title = excluded.lichess_title,
                last_login_at = datetime('now')
            """,
            (lichess_id, username, email, title),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE lichess_id = ?", (lichess_id,)).fetchone()
    finally:
        conn.close()
    return dict(row)


def claim_unowned_data(user_id: int) -> dict:
    """Assigns every profile/game/puzzle with no owner yet (pre-existing
    local data from before this user's first login) to `user_id`. Not run
    automatically by any migration — deciding "all of this app's existing
    local history belongs to whoever logs in first" is a real decision a
    single-user install's owner should trigger deliberately (e.g. a
    one-time "claim my existing data" button after first login), not
    something applied silently to every fresh multi-user deployment.
    """
    conn = get_connection()
    try:
        profiles_claimed = conn.execute(
            "UPDATE profiles SET user_id = ? WHERE user_id IS NULL", (user_id,)
        ).rowcount
        games_claimed = conn.execute(
            "UPDATE games SET user_id = ? WHERE user_id IS NULL", (user_id,)
        ).rowcount
        puzzles_claimed = conn.execute(
            "UPDATE puzzles SET user_id = ? WHERE user_id IS NULL", (user_id,)
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    return {
        "profiles_claimed": profiles_claimed,
        "games_claimed": games_claimed,
        "puzzles_claimed": puzzles_claimed,
    }


# --- Sessions --------------------------------------------------------------

def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("ascii")).hexdigest()


def create_session(user_id: int) -> tuple[str, datetime]:
    """Returns (raw_token, expires_at). The raw token is what gets set as
    the cookie value; only its hash is persisted.
    """
    raw_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)

    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, datetime('now'), ?)",
            # Stored in SQLite's own datetime() text format ("YYYY-MM-DD
            # HH:MM:SS"), NOT datetime.isoformat() ("...T...+00:00") — the
            # expiry check below compares this column against
            # datetime('now') as plain strings, and isoformat()'s "T"
            # sorts after datetime('now')'s " " (0x54 > 0x20), which made
            # any session expiring on the same calendar day the check runs
            # on compare as still-valid regardless of the actual time.
            (_hash_token(raw_token), user_id, expires_at.strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
    finally:
        conn.close()
    return raw_token, expires_at


def destroy_session(raw_token: str) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(raw_token),))
        conn.commit()
    finally:
        conn.close()


def _user_for_token(raw_token: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT u.* FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND s.expires_at > datetime('now')
            """,
            (_hash_token(raw_token),),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


# --- FastAPI dependencies ---------------------------------------------------

def get_current_user(request: Request) -> dict:
    """Require a logged-in user; raises 401 otherwise. Use as a route
    dependency: `user: dict = Depends(get_current_user)`.
    """
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = _user_for_token(raw_token)
    if user is None:
        raise HTTPException(status_code=401, detail="Session expired or invalid")
    return user


def get_current_user_optional(request: Request) -> dict | None:
    """Same as get_current_user but returns None instead of raising, for
    routes that render differently when logged out rather than blocking
    access entirely (e.g. the dashboard's own landing page).
    """
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        return None
    return _user_for_token(raw_token)


def verify_owns_game(game_id: int, user: dict) -> None:
    """404 (not 403) on a game that doesn't exist OR belongs to someone
    else — both cases should look identical from outside so an attacker
    probing ids can't distinguish "not found" from "not yours".
    """
    conn = get_connection()
    try:
        row = conn.execute("SELECT user_id FROM games WHERE id = ?", (game_id,)).fetchone()
    finally:
        conn.close()
    if row is None or row["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Game not found")


def verify_owns_puzzle(puzzle_id: int, user: dict) -> None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT user_id FROM puzzles WHERE id = ?", (puzzle_id,)).fetchone()
    finally:
        conn.close()
    if row is None or row["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Puzzle not found")


def require_game_owner(game_id: int, user: dict = Depends(get_current_user)) -> dict:
    """Route dependency for `/api/games/{game_id}`-shaped endpoints: pulls
    the path's game_id automatically (FastAPI matches dependency params to
    the route's own path params by name) and 404s unless the logged-in
    user owns it. Returns the user so the route can still use it.
    """
    verify_owns_game(game_id, user)
    return user


def require_puzzle_owner(puzzle_id: int, user: dict = Depends(get_current_user)) -> dict:
    verify_owns_puzzle(puzzle_id, user)
    return user

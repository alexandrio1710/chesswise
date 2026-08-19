"""Unit tests for auth.py: session lifecycle, one-time OAuth state, and the
unowned-or-mine access rule the legacy (pre-OAuth) routes gate on.
"""

import pytest
from fastapi import HTTPException

import auth
from db import get_connection


def _make_user(lichess_id: str, username: str) -> dict:
    return auth.upsert_user({"id": lichess_id, "username": username})


class TestSessions:
    def test_create_and_resolve_session(self):
        user = _make_user("auth-test-1", "sessuser1")
        raw_token, _ = auth.create_session(user["id"])
        resolved = auth._user_for_token(raw_token)
        assert resolved is not None
        assert resolved["id"] == user["id"]

    def test_destroy_session_invalidates_it(self):
        user = _make_user("auth-test-2", "sessuser2")
        raw_token, _ = auth.create_session(user["id"])
        auth.destroy_session(raw_token)
        assert auth._user_for_token(raw_token) is None

    def test_unknown_token_resolves_to_none(self):
        assert auth._user_for_token("not-a-real-token") is None

    def test_expiry_is_stored_as_sqlite_datetime_text_not_isoformat(self):
        # Regression guard for the bug fixed in create_session: expires_at
        # must be SQLite's own "YYYY-MM-DD HH:MM:SS" text, not
        # datetime.isoformat()'s "...T..." — isoformat's "T" (0x54) sorts
        # after datetime('now')'s space (0x20), which made any session
        # expiring later today compare as still-valid regardless of the
        # actual time (see create_session's docstring).
        user = _make_user("auth-test-3", "sessuser3")
        raw_token, _ = auth.create_session(user["id"])
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT expires_at FROM sessions WHERE token_hash = ?", (auth._hash_token(raw_token),)
            ).fetchone()
        finally:
            conn.close()
        assert "T" not in row["expires_at"]


class TestOAuthState:
    def test_state_is_single_use(self):
        url = auth.build_authorize_url()
        state = url.split("state=")[1].split("&")[0]
        verifier = auth._consume_oauth_state(state)
        assert verifier
        with pytest.raises(HTTPException):
            auth._consume_oauth_state(state)

    def test_unknown_state_is_rejected(self):
        with pytest.raises(HTTPException):
            auth._consume_oauth_state("not-a-real-state")


class TestClaimUnownedData:
    def test_claims_only_the_currently_unowned_row(self):
        user = _make_user("auth-test-4", "claimuser")
        conn = get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO games (source, source_game_id, date, result, color, analyzed) "
                "VALUES ('manual', 'claim-test-1', datetime('now'), 'win', 'white', 0)"
            )
            unowned_game_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()

        result = auth.claim_unowned_data(user["id"])
        assert result["games_claimed"] >= 1

        conn = get_connection()
        try:
            row = conn.execute("SELECT user_id FROM games WHERE id = ?", (unowned_game_id,)).fetchone()
        finally:
            conn.close()
        assert row["user_id"] == user["id"]


class TestOwnershipRule:
    """auth._check_owner_or_unowned is the shared rule behind every
    require_*_access dependency (games, notes, goals, profiles, mistakes).
    """

    def test_unowned_row_allows_anyone(self):
        auth._check_owner_or_unowned(None, None, "nope")
        auth._check_owner_or_unowned(None, {"id": 1}, "nope")

    def test_owned_row_rejects_logged_out(self):
        with pytest.raises(HTTPException) as exc_info:
            auth._check_owner_or_unowned(5, None, "nope")
        assert exc_info.value.status_code == 404

    def test_owned_row_rejects_a_different_user(self):
        with pytest.raises(HTTPException) as exc_info:
            auth._check_owner_or_unowned(5, {"id": 6}, "nope")
        assert exc_info.value.status_code == 404

    def test_owned_row_allows_its_owner(self):
        auth._check_owner_or_unowned(5, {"id": 5}, "nope")  # should not raise

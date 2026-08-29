"""Unit tests for opening_explorer.py's Lichess community-explorer client.

explorer.lichess.org started returning a bare 401 ("Authorization
Required") to anonymous requests as of mid-2026 — confirmed live against
the real API, matching community reports of the same change. These tests
cover the fix: an optional personal API token sent as a Bearer header, and
surfacing *why* the community panel is unavailable (needs_auth vs a
generic, possibly-transient failure) so the frontend can tell them apart.
requests.get is monkeypatched throughout — no real network calls.
"""

import requests

import opening_explorer


class _FakeResponse:
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json_data = json_data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.exceptions.HTTPError(f"{self.status_code} error")
            error.response = self
            raise error

    def json(self):
        return self._json_data


class TestQueryCommunityExplorer:
    def setup_method(self):
        opening_explorer._community_cache.clear()

    def test_success_returns_data_and_no_auth_needed(self, monkeypatch):
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append(headers)
            return _FakeResponse(200, {"white": 10, "draws": 2, "black": 8, "moves": []})

        monkeypatch.setattr(opening_explorer.requests, "get", fake_get)
        data, needs_auth = opening_explorer.query_community_explorer("fen-a")

        assert data == {"white": 10, "draws": 2, "black": 8, "moves": []}
        assert needs_auth is False
        assert "Authorization" not in calls[0]

    def test_a_401_is_reported_as_needing_auth_not_a_generic_failure(self, monkeypatch):
        def fake_get(url, params=None, headers=None, timeout=None):
            return _FakeResponse(401)

        monkeypatch.setattr(opening_explorer.requests, "get", fake_get)
        data, needs_auth = opening_explorer.query_community_explorer("fen-b")

        assert data is None
        assert needs_auth is True

    def test_a_401_does_not_retry(self, monkeypatch):
        # No point retrying the same unauthenticated request three times —
        # it isn't a transient failure, and needlessly slows the page down.
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append(1)
            return _FakeResponse(401)

        monkeypatch.setattr(opening_explorer.requests, "get", fake_get)
        opening_explorer.query_community_explorer("fen-c")

        assert len(calls) == 1

    def test_a_non_auth_failure_is_not_reported_as_needing_auth(self, monkeypatch):
        def fake_get(url, params=None, headers=None, timeout=None):
            return _FakeResponse(500)

        monkeypatch.setattr(opening_explorer.requests, "get", fake_get)
        monkeypatch.setattr(opening_explorer.time, "sleep", lambda *_: None)
        data, needs_auth = opening_explorer.query_community_explorer("fen-d")

        assert data is None
        assert needs_auth is False

    def test_a_configured_token_is_sent_as_a_bearer_header(self, monkeypatch):
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append(headers)
            return _FakeResponse(200, {"moves": []})

        monkeypatch.setattr(opening_explorer.requests, "get", fake_get)
        opening_explorer.query_community_explorer("fen-e", lichess_api_token="my-token-123")

        assert calls[0]["Authorization"] == "Bearer my-token-123"

    def test_unauthenticated_and_authenticated_results_are_cached_separately(self, monkeypatch):
        # A 401 cached under a plain FEN key would otherwise never be
        # retried even after the user adds a token later in the same
        # server session.
        responses = iter([_FakeResponse(401), _FakeResponse(200, {"moves": []})])

        def fake_get(url, params=None, headers=None, timeout=None):
            return next(responses)

        monkeypatch.setattr(opening_explorer.requests, "get", fake_get)

        data1, needs_auth1 = opening_explorer.query_community_explorer("fen-f")
        assert data1 is None and needs_auth1 is True

        data2, needs_auth2 = opening_explorer.query_community_explorer("fen-f", lichess_api_token="tok")
        assert data2 == {"moves": []} and needs_auth2 is False

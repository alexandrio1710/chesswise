"""Tests for the retry-with-backoff wrapper around chess API requests."""

from unittest.mock import MagicMock, patch

import pytest
import requests

import fetchers


class TestRetryWithBackoff:
    def test_succeeds_immediately_with_no_errors(self):
        with patch("requests.Session.request", return_value=MagicMock(status_code=200)) as mock_req:
            resp = fetchers._request_with_retry("GET", "http://fake")
        assert resp.status_code == 200
        assert mock_req.call_count == 1

    def test_retries_on_429_then_succeeds(self):
        responses = [MagicMock(status_code=429, headers={}), MagicMock(status_code=200)]
        with patch("requests.Session.request", side_effect=responses), patch("time.sleep") as mock_sleep:
            resp = fetchers._request_with_retry("GET", "http://fake")
        assert resp.status_code == 200
        mock_sleep.assert_called_once()

    def test_respects_retry_after_header_on_429(self):
        responses = [
            MagicMock(status_code=429, headers={"Retry-After": "5"}),
            MagicMock(status_code=200),
        ]
        with patch("requests.Session.request", side_effect=responses), patch("time.sleep") as mock_sleep:
            fetchers._request_with_retry("GET", "http://fake")
        mock_sleep.assert_called_once_with(5.0)

    def test_retries_on_network_error_then_succeeds(self):
        with patch(
            "requests.Session.request",
            side_effect=[requests.exceptions.ConnectionError("boom"), MagicMock(status_code=200)],
        ), patch("time.sleep"):
            resp = fetchers._request_with_retry("GET", "http://fake")
        assert resp.status_code == 200

    def test_gives_up_after_max_retries_on_persistent_network_error(self):
        with patch(
            "requests.Session.request", side_effect=requests.exceptions.ConnectionError("boom")
        ), patch("time.sleep"):
            with pytest.raises(ConnectionError):
                fetchers._request_with_retry("GET", "http://fake")

    def test_gives_up_after_max_retries_on_persistent_429(self):
        with patch(
            "requests.Session.request", return_value=MagicMock(status_code=429, headers={})
        ) as mock_req, patch("time.sleep"):
            resp = fetchers._request_with_retry("GET", "http://fake")
        # Persistent 429s aren't a network exception, so the wrapper
        # returns the last (still-429) response rather than raising —
        # callers decide what a non-2xx status means via raise_for_status().
        assert resp.status_code == 429
        assert mock_req.call_count == fetchers.MAX_RETRIES

    def test_retries_on_502_then_succeeds(self):
        responses = [MagicMock(status_code=502, headers={}), MagicMock(status_code=200)]
        with patch("requests.Session.request", side_effect=responses), patch("time.sleep") as mock_sleep:
            resp = fetchers._request_with_retry("GET", "http://fake")
        assert resp.status_code == 200
        mock_sleep.assert_called_once()

    def test_does_not_retry_on_plain_4xx(self):
        with patch(
            "requests.Session.request", return_value=MagicMock(status_code=404, headers={})
        ) as mock_req, patch("time.sleep") as mock_sleep:
            resp = fetchers._request_with_retry("GET", "http://fake")
        assert resp.status_code == 404
        assert mock_req.call_count == 1
        mock_sleep.assert_not_called()


class TestParseRetryAfter:
    def test_numeric_delta_seconds(self):
        assert fetchers._parse_retry_after("120") == 120.0

    def test_none_or_empty(self):
        assert fetchers._parse_retry_after(None) is None
        assert fetchers._parse_retry_after("") is None

    def test_http_date_form_does_not_crash(self):
        # Retry-After may legally be an HTTP-date instead of delta-seconds
        # (float(value) raises ValueError on this form) — previously
        # uncaught, crashing the whole fetch instead of falling back to
        # exponential backoff.
        future = "Wed, 21 Oct 2099 07:28:00 GMT"
        result = fetchers._parse_retry_after(future)
        assert result is not None
        assert result > 0

    def test_garbage_value_returns_none_rather_than_raising(self):
        assert fetchers._parse_retry_after("not-a-real-value") is None

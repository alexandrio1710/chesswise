"""
Tests for Stage 1's source normalization: a raw Lichess PGN blob and a raw
Chess.com game dict must both produce the same normalized shape, so nothing
downstream needs to know which site a game came from.
"""

import json
from unittest.mock import MagicMock, patch

from fetchers import (
    _combine_lichess_datetime,
    _normalize_chesscom_game,
    _normalize_lichess_game,
    _split_pgn_blobs,
    fetch_lichess_games,
)

from conftest import FIXTURES_DIR

NORMALIZED_KEYS = {
    "source", "source_game_id", "date", "opponent", "result",
    "color", "time_control", "opening_name", "pgn",
    "player_rating", "opponent_rating",
}


def _load_lichess_fixture() -> str:
    return (FIXTURES_DIR / "lichess_sample.pgn").read_text(encoding="utf-8")


def _load_chesscom_fixture() -> dict:
    return json.loads((FIXTURES_DIR / "chesscom_sample.json").read_text(encoding="utf-8"))


class TestLichessNormalization:
    def test_produces_expected_keys(self):
        game = _normalize_lichess_game(_load_lichess_fixture(), "testuser")
        assert set(game.keys()) == NORMALIZED_KEYS

    def test_field_values(self):
        game = _normalize_lichess_game(_load_lichess_fixture(), "testuser")
        assert game["source"] == "lichess"
        assert game["color"] == "white"
        assert game["opponent"] == "opponent1"
        assert game["result"] == "win"
        assert game["opening_name"] == "Italian Game"
        assert game["date"].startswith("2026-01-15")
        assert game["source_game_id"] == "abcd1234"

    def test_username_matching_is_case_insensitive(self):
        game = _normalize_lichess_game(_load_lichess_fixture(), "TESTUSER")
        assert game["color"] == "white"


class TestChesscomNormalization:
    def test_produces_expected_keys(self):
        game = _normalize_chesscom_game(_load_chesscom_fixture(), "testuser")
        assert set(game.keys()) == NORMALIZED_KEYS

    def test_field_values(self):
        game = _normalize_chesscom_game(_load_chesscom_fixture(), "testuser")
        assert game["source"] == "chesscom"
        assert game["color"] == "white"
        assert game["opponent"] == "opponent2"
        assert game["result"] == "win"
        assert game["opening_name"] == "Italian Game"
        assert game["source_game_id"] == "abcd1234-5678-90ef-ghij-klmnopqrstuv"

    def test_clock_annotations_normalized_to_lichess_style(self):
        # Chess.com's fractional-second clock format ([%clk 0:04:58.4])
        # should be stripped to match Lichess's ([%clk 0:04:58]) so
        # downstream clock parsing can use one regex for both sources.
        game = _normalize_chesscom_game(_load_chesscom_fixture(), "testuser")
        assert "[%clk 0:04:58.4]" not in game["pgn"]
        assert "[%clk 0:04:58]" in game["pgn"]


class TestBothSourcesMatch:
    def test_lichess_and_chesscom_produce_identical_shape(self):
        lichess_game = _normalize_lichess_game(_load_lichess_fixture(), "testuser")
        chesscom_game = _normalize_chesscom_game(_load_chesscom_fixture(), "testuser")
        assert set(lichess_game.keys()) == set(chesscom_game.keys())
        for key in NORMALIZED_KEYS:
            assert type(lichess_game[key]) is type(chesscom_game[key])


class TestAbortedGamesAreSkipped:
    def test_lichess_star_result_is_skipped_not_stored_as_draw(self):
        pgn = _load_lichess_fixture().replace('[Result "1-0"]', '[Result "*"]')
        assert _normalize_lichess_game(pgn, "testuser") is None

    def test_chesscom_missing_uuid_and_url_is_skipped(self):
        raw = _load_chesscom_fixture()
        raw.pop("uuid", None)
        raw["url"] = ""
        assert _normalize_chesscom_game(raw, "testuser") is None


class TestCombineLichessDatetime:
    def test_full_date_and_time(self):
        assert _combine_lichess_datetime("2026.01.15", "14:32:00") == "2026-01-15T14:32:00+00:00"

    def test_missing_time_falls_back_to_date_only_iso(self):
        # Must still be a valid ISO string (not the raw "2026.01.15") since
        # every caller assumes date is either ISO or "" — never dot-format —
        # see _combine_lichess_datetime's own docstring for what broke
        # downstream when it wasn't (MAX(date) ordering, insights.py crashes).
        result = _combine_lichess_datetime("2026.01.15", "")
        assert result == "2026-01-15T00:00:00+00:00"

    def test_garbage_time_falls_back_to_date_only_iso(self):
        result = _combine_lichess_datetime("2026.01.15", "not-a-time")
        assert result.startswith("2026-01-15")
        assert "." not in result

    def test_no_date_returns_empty_string(self):
        assert _combine_lichess_datetime("", "") == ""

    def test_garbage_date_returns_empty_string(self):
        assert _combine_lichess_datetime("not-a-date", "") == ""


class TestLichessRefreshHasNoMaxCap:
    """A `max` param is Lichess's hard cap independent of `since` — sending
    both on an incremental refresh silently truncated a large backlog and
    permanently skipped the older games in the gap (the next refresh's
    cutoff advances past only what was actually returned). `max` must be
    omitted whenever `since_ms` is given, mirroring
    fetch_chesscom_games' own "no cap on an incremental catch-up" contract.
    """

    def _mock_response(self):
        return MagicMock(status_code=200, text='[Event "A"]\n[Result "*"]\n\n1. e4', raise_for_status=lambda: None)

    def test_since_ms_given_omits_max_param(self):
        with patch("fetchers._request_with_retry", return_value=self._mock_response()) as mock_req:
            fetch_lichess_games("testuser", max_games=20, since_ms=1234567890000)
        params = mock_req.call_args.kwargs["params"]
        assert "max" not in params
        assert params["since"] == 1234567890000

    def test_no_since_ms_still_sends_max_param(self):
        # A first-ever fetch (no prior stored games, so no cutoff to fetch
        # "since") should stay bounded — this is the normal, non-refresh
        # fetch path, not the one the bug was in.
        with patch("fetchers._request_with_retry", return_value=self._mock_response()) as mock_req:
            fetch_lichess_games("testuser", max_games=20, since_ms=None)
        params = mock_req.call_args.kwargs["params"]
        assert params["max"] == 20
        assert "since" not in params


class TestSplitPgnBlobs:
    def test_splits_on_lf(self):
        text = '[Event "A"]\n[Result "*"]\n\n1. e4\n\n[Event "B"]\n[Result "*"]\n\n1. d4'
        assert len(_split_pgn_blobs(text)) == 2

    def test_splits_on_crlf(self):
        # A CRLF-terminated export has no literal "\n\n" before "[Event ",
        # so the whole response used to collapse into a single blob.
        text = '[Event "A"]\r\n[Result "*"]\r\n\r\n1. e4\r\n\r\n[Event "B"]\r\n[Result "*"]\r\n\r\n1. d4'
        blobs = _split_pgn_blobs(text)
        assert len(blobs) == 2
        assert '[Event "A"]' in blobs[0]
        assert '[Event "B"]' in blobs[1]

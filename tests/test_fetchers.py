"""
Tests for Stage 1's source normalization: a raw Lichess PGN blob and a raw
Chess.com game dict must both produce the same normalized shape, so nothing
downstream needs to know which site a game came from.
"""

import json

from fetchers import _normalize_chesscom_game, _normalize_lichess_game

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

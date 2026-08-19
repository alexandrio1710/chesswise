"""Tests for tablebase.py's error handling and Endgame Trainer move logic.

Network calls to the Lichess tablebase API are mocked (via requests.Session.
request, matching test_fetchers_retry.py's pattern) so these run offline
and deterministically.
"""

from unittest.mock import MagicMock, patch

import pytest

import tablebase


class TestMalformedFenHandling:
    """query_tablebase's docstring promises None for anything not
    tablebase-backed, including a malformed FEN — previously chess.Board(fen)
    raised ValueError before that promise was ever checked.
    """

    def test_is_tablebase_eligible_returns_false_not_raises(self):
        assert tablebase.is_tablebase_eligible("not-a-fen") is False

    def test_query_tablebase_returns_none_not_raises(self):
        assert tablebase.query_tablebase("not-a-fen") is None

    def test_analyze_tablebase_mistake_returns_none_for_malformed_fen(self):
        assert tablebase.analyze_tablebase_mistake("not-a-fen", "e2e4") is None

    def test_trainer_attempt_move_raises_clean_value_error(self):
        with pytest.raises(ValueError):
            tablebase.trainer_attempt_move("not-a-fen", "e2e4")


class TestTrainerAttemptMovePromotion:
    """A K+P endgame position needing a pawn push to the last rank is the
    Endgame Trainer's core use case — trainer_attempt_move must accept a
    bare 4-char UCI move (no promotion letter) for it, same as
    puzzles.check_attempt already does elsewhere in the app.
    """

    def _mock_tablebase_response(self, category="win", moves=None):
        return MagicMock(
            status_code=200,
            json=lambda: {
                "category": category,
                "dtz": 1,
                "dtm": None,
                "checkmate": False,
                "stalemate": False,
                "moves": moves or [],
            },
        )

    # White king e6, white pawn e7, black king a4 — e7e8 requires
    # promotion to be legal at all (confirmed valid/legal via python-chess).
    FEN = "8/4P3/4K3/8/k7/8/8/8 w - - 0 1"

    def test_bare_uci_promotion_move_is_accepted(self):
        tablebase._tablebase_cache.clear()
        with patch("requests.Session.request", return_value=self._mock_tablebase_response()):
            result = tablebase.trainer_attempt_move(self.FEN, "e7e8")
        assert "=Q" in result["played_san"]

    def test_illegal_move_raises_value_error(self):
        tablebase._tablebase_cache.clear()
        with patch("requests.Session.request", return_value=self._mock_tablebase_response()):
            with pytest.raises(ValueError):
                tablebase.trainer_attempt_move(self.FEN, "e7d8")

    def test_malformed_move_uci_raises_value_error(self):
        tablebase._tablebase_cache.clear()
        with patch("requests.Session.request", return_value=self._mock_tablebase_response()):
            with pytest.raises(ValueError):
                tablebase.trainer_attempt_move(self.FEN, "not-a-move")


class TestTablebaseCacheCap:
    def test_cache_clears_itself_once_it_hits_the_size_cap(self):
        tablebase._tablebase_cache.clear()
        try:
            for i in range(tablebase._TABLEBASE_CACHE_MAX_SIZE):
                tablebase._tablebase_cache[f"fake-fen-{i}"] = None
            assert len(tablebase._tablebase_cache) == tablebase._TABLEBASE_CACHE_MAX_SIZE

            with patch("requests.Session.request", return_value=MagicMock(
                status_code=200, json=lambda: {"category": "draw", "moves": []},
            )):
                tablebase.query_tablebase("4k3/8/4K3/8/8/8/8/8 w - - 0 1")

            # The cap check clears the whole dict before inserting the new
            # entry once size >= cap, rather than growing unbounded.
            assert len(tablebase._tablebase_cache) <= tablebase._TABLEBASE_CACHE_MAX_SIZE
        finally:
            tablebase._tablebase_cache.clear()

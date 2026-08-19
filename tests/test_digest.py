"""Tests for digest.py: raises DigestError (not sys.exit) on failure so it
stays safe to call from a long-lived process, and scopes its "new
games"/"new mistakes" counts to what the run itself actually did rather
than a global before/after diff that mixes in other local profiles.
"""

import pytest

import digest
from db import get_connection


class TestRunDigestRaisesInsteadOfExiting:
    def test_missing_webhook_raises_digest_error_not_system_exit(self, monkeypatch):
        monkeypatch.setattr(digest.config, "DISCORD_WEBHOOK_URL", None)
        with pytest.raises(digest.DigestError):
            digest.run_digest(lichess_user="someone", webhook_url=None)


class TestMistakesForGames:
    def test_empty_list_returns_zero_without_querying(self):
        assert digest._mistakes_for_games([]) == 0

    def test_counts_only_mistakes_for_the_given_games(self):
        conn = get_connection()
        try:
            game_a = conn.execute(
                "INSERT INTO games (source, source_game_id, date, result, color, analyzed) "
                "VALUES ('manual', 'digest-test-a', datetime('now'), 'win', 'white', 1)"
            ).lastrowid
            game_b = conn.execute(
                "INSERT INTO games (source, source_game_id, date, result, color, analyzed) "
                "VALUES ('manual', 'digest-test-b', datetime('now'), 'win', 'white', 1)"
            ).lastrowid
            for game_id in (game_a, game_a, game_b):
                conn.execute(
                    "INSERT INTO mistakes (game_id, ply, move_number, move_san, color_moved, phase, "
                    "severity, eval_before, eval_after, eval_drop) "
                    "VALUES (?, 1, 1, 'e4', 'white', 'opening', 'blunder', 0, -500, 500)",
                    (game_id,),
                )
            conn.commit()
        finally:
            conn.close()

        # Only game_a's two mistakes should count — game_b's one mistake
        # (a stand-in for "some other profile's concurrent activity")
        # must not leak into a count scoped to game_a alone.
        assert digest._mistakes_for_games([game_a]) == 2

import itertools

import review
from db import get_connection

_counter = itertools.count(1)

FORK_FEN = "3q1k2/8/8/2N5/8/8/8/4K3 w - - 0 1"


def _pgn(fen: str, moves: str) -> str:
    return f'[Event "t"]\n[SetUp "1"]\n[FEN "{fen}"]\n[Result "*"]\n\n{moves} *\n'


def _row(ply, color, before, after_white, best=None, pv=None, mate=None):
    return {"ply": ply, "color_moved": color, "eval_before_cp": before, "eval_cp": after_white,
            "best_move_uci": best, "best_pv_uci": pv, "best_mate_in": mate}


def _events(pgn, rows, **filters):
    found = review.find_tactic_events(pgn, rows)
    return [e for e in found if all(e[k] == v for k, v in filters.items())]


class TestForkOpportunity:
    PV = "c5e6 f8e8 e6d8 e8d8"

    def test_playing_the_forking_move_is_found(self):
        pgn = _pgn(FORK_FEN, "1. Ne6+ Ke8 2. Nxd8 Kxd8")
        rows = [_row(1, "white", 600, 600, best="c5e6", pv=self.PV)]
        events = _events(pgn, rows, motif="fork")
        assert [(e["ply"], e["color"], e["outcome"], e["gain"]) for e in events] == [(1, "white", "found", 6)]

    def test_playing_something_else_and_dropping_the_advantage_is_missed(self):
        pgn = _pgn(FORK_FEN, "1. Kf2 Ke8")
        rows = [_row(1, "white", 600, 100, best="c5e6", pv=self.PV)]
        events = _events(pgn, rows, motif="fork")
        assert [(e["outcome"], e["played_uci"], e["best_uci"]) for e in events] == [("missed", "e1f2", "c5e6")]

    def test_an_equally_good_alternative_is_neither_found_nor_missed(self):
        pgn = _pgn(FORK_FEN, "1. Kf2 Ke8")
        rows = [_row(1, "white", 600, 590, best="c5e6", pv=self.PV)]
        assert _events(pgn, rows, motif="fork") == []

    def test_a_fork_that_wins_no_material_is_not_an_opportunity(self):
        pgn = _pgn(FORK_FEN, "1. Ne6+ Ke8")
        rows = [_row(1, "white", 600, 600, best="c5e6", pv="c5e6 f8e8")]
        assert _events(pgn, rows, motif="fork") == []


class TestMateOpportunity:
    FEN = "6k1/5ppp/8/8/8/8/8/R3K3 w - - 0 1"

    def test_delivering_the_mate_is_found(self):
        events = _events(_pgn(self.FEN, "1. Ra8#"), [_row(1, "white", 9999, 10000, best="a1a8", mate=1)], motif="mate")
        assert [(e["outcome"], e["gain"]) for e in events] == [("found", 1)]

    def test_letting_the_mate_slip_is_missed(self):
        events = _events(_pgn(self.FEN, "1. Kd2 h6"), [_row(1, "white", 9999, 300, best="a1a8", mate=1)], motif="mate")
        assert [e["outcome"] for e in events] == ["missed"]

    def test_no_mate_available_means_no_event(self):
        assert _events(_pgn(self.FEN, "1. Kd2 h6"), [_row(1, "white", 300, 300, best="a1a8")], motif="mate") == []


class TestFreePieces:
    FEN = "4k3/8/8/3b4/8/8/8/3QK3 w - - 0 1"  # Black's bishop on d5 is undefended

    def test_capturing_the_hanging_piece_is_taken(self):
        events = _events(_pgn(self.FEN, "1. Qxd5 Ke7"), [], motif="free_piece")
        assert [(e["color"], e["outcome"], e["gain"]) for e in events] == [("white", "taken", 3)]

    def test_playing_something_else_is_ignored(self):
        events = _events(_pgn(self.FEN, "1. Kd2 Ke7"), [], motif="free_piece")
        assert [(e["color"], e["outcome"]) for e in events] == [("white", "ignored")]

    def test_no_free_piece_event_when_nothing_is_hanging(self):
        assert _events(_pgn("4k3/8/8/8/8/8/8/3QK3 w - - 0 1", "1. Kd2 Ke7"), [], motif="free_piece") == []


class TestLeftHanging:
    FEN = "4k3/8/2p5/8/8/8/8/3QK3 w - - 0 1"  # ...Qd5 walks into a pawn capture

    def test_a_piece_left_en_prise_and_taken_is_punished(self):
        events = _events(_pgn(self.FEN, "1. Qd5 cxd5"), [], motif="left_hanging")
        assert [(e["color"], e["outcome"], e["gain"]) for e in events] == [("white", "punished", 9)]

    def test_a_piece_left_en_prise_but_not_taken_escaped(self):
        events = _events(_pgn(self.FEN, "1. Qd5 Ke7"), [], motif="left_hanging")
        assert [e["outcome"] for e in events] == [("escaped")]

    def test_a_safe_move_leaves_nothing_hanging(self):
        assert _events(_pgn(self.FEN, "1. Qd2 Ke7"), [], motif="left_hanging") == []


class TestExtractAndNeedsReview:
    def _insert_game(self, reviewed_at=None, analyzed_at="2026-01-01 00:00:00", with_best=True):
        n = next(_counter)
        conn = get_connection()
        try:
            game_id = conn.execute(
                "INSERT INTO games (source, source_game_id, date, result, color, pgn, analyzed, analyzed_at, reviewed_at) "
                "VALUES ('manual', ?, datetime('now'), 'win', 'white', ?, 1, ?, ?)",
                (f"review-{n}", _pgn(FORK_FEN, "1. Ne6+ Ke8 2. Nxd8 Kxd8"), analyzed_at, reviewed_at),
            ).lastrowid
            conn.execute(
                "INSERT INTO game_moves (game_id, ply, move_number, color_moved, move_san, eval_cp, eval_before_cp, "
                "best_move_uci, best_pv_uci) VALUES (?, 1, 1, 'white', 'Ne6+', 600, 600, ?, ?)",
                (game_id, "c5e6" if with_best else None, "c5e6 f8e8 e6d8 e8d8" if with_best else None),
            )
            conn.commit()
            return game_id
        finally:
            conn.close()

    def test_extract_stores_events_and_is_idempotent(self):
        game_id = self._insert_game()
        assert review.extract_tactics(game_id) >= 1
        review.extract_tactics(game_id)
        conn = get_connection()
        try:
            forks = conn.execute("SELECT COUNT(*) n FROM game_tactics WHERE game_id = ? AND motif = 'fork'", (game_id,)).fetchone()["n"]
        finally:
            conn.close()
        assert forks == 1

    def test_unreviewed_game_needs_review(self):
        assert review.needs_review(self._insert_game(reviewed_at=None)) is True

    def test_fully_reviewed_game_does_not(self):
        assert review.needs_review(self._insert_game(reviewed_at="2026-06-01 00:00:00")) is False

    def test_game_reanalyzed_after_its_review_needs_review_again(self):
        game_id = self._insert_game(reviewed_at="2026-01-01 00:00:00", analyzed_at="2026-06-01 00:00:00")
        assert review.needs_review(game_id) is True

    def test_missing_best_move_data_needs_review(self):
        game_id = self._insert_game(reviewed_at="2026-06-01 00:00:00", with_best=False)
        assert review.needs_review(game_id) is True

    def test_backfill_targets_only_games_that_need_it(self):
        pending = self._insert_game(reviewed_at=None)
        done = self._insert_game(reviewed_at="2026-06-01 00:00:00")
        todo = review.games_needing_review()
        assert pending in todo and done not in todo


class TestGamesWithoutMoves:
    def test_a_game_that_ended_before_any_move_has_nothing_to_review(self):
        import review
        from db import get_connection
        conn = get_connection()
        try:
            gid = conn.execute(
                "INSERT INTO games (source, source_game_id, date, result, color, analyzed, analyzed_at) "
                "VALUES ('manual', 'no-moves-1', '2026-01-01T00:00:00+00:00', 'win', 'white', 1, '2026-01-01 00:00:00')").lastrowid
            conn.commit()
        finally:
            conn.close()
        assert review.needs_review(gid) is False
        assert gid not in review.games_needing_review()


class TestReviewCli:
    def test_review_command_backfills_and_shuts_the_pool_down(self, monkeypatch, capsys):
        import argparse

        import cli
        import engine_pool
        import review

        calls = []
        monkeypatch.setattr(review, "games_needing_review", lambda game_ids=None: [1, 2])
        monkeypatch.setattr(review, "backfill_reviews", lambda game_ids=None, progress=None: (progress(2, 2, 0), {"reviewed": 2, "failed": 0})[1])
        monkeypatch.setattr(engine_pool, "shutdown", lambda: calls.append("shutdown"))
        cli.cmd_review(argparse.Namespace())
        out = capsys.readouterr().out
        assert "2 game(s) need review data." in out and "Done: 2 reviewed, 0 failed." in out
        assert calls == ["shutdown"]

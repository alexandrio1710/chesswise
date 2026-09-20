"""Play a position out against Stockfish: strength levels, validation, outcomes."""

import chess
import pytest
from fastapi.testclient import TestClient

import play
import server

client = TestClient(server.app)


class TestReply:
    def test_returns_a_legal_move_and_the_new_position(self):
        r = play.reply(chess.STARTING_FEN, "beginner")
        board = chess.Board(chess.STARTING_FEN)
        move = chess.Move.from_uci(r["uci"])
        assert move in board.legal_moves and r["san"] == board.san(move)
        board.push(move)
        assert r["fen"] == board.fen() and r["outcome"] is None

    def test_delivers_mate_in_one_at_every_level(self):
        fen = "6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1"  # Ra8#
        for level in play.LEVELS:
            assert play.reply(fen, level)["outcome"] == {"kind": "checkmate", "winner": "white"}, level

    @pytest.mark.parametrize("fen, level, message", [
        ("not a fen", "1600", "valid position"), (chess.STARTING_FEN, "godlike", "Unknown level"),
        ("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1", "1600", "already over"),        # stalemate
        ("k7/8/8/8/8/8/8/8 w - - 0 1", "1600", "legal"),                     # no white king
    ])
    def test_bad_input_is_a_value_error(self, fen, level, message):
        with pytest.raises(ValueError, match=message):
            play.reply(fen, level)

    def test_strength_options_are_clamped_to_what_the_engine_supports(self):
        # 1350 is above UCI_Elo's minimum on current Stockfish, but a lower bound must never crash a reply
        assert play.LEVELS["1350"]["options"]["UCI_Elo"] == 1350
        assert play.reply(chess.STARTING_FEN, "1350")["uci"]


class TestOutcome:
    def test_outcomes(self):
        assert play.outcome_of(chess.Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")) == {"kind": "stalemate", "winner": None}
        assert play.outcome_of(chess.Board("R5k1/5ppp/8/8/8/8/8/6K1 b - - 0 1")) == {"kind": "checkmate", "winner": "white"}
        assert play.outcome_of(chess.Board("8/8/8/4k3/8/8/8/4K3 w - - 0 1"))["kind"] == "insufficient material"
        assert play.outcome_of(chess.Board()) is None


class TestEndpoints:
    def test_levels(self):
        body = client.get("/api/play/levels").json()
        assert body["default"] == "1600" and [l["key"] for l in body["levels"]][0] == "beginner"

    def test_reply_and_errors(self):
        ok = client.post("/api/play/reply", json={"fen": chess.STARTING_FEN, "level": "beginner"})
        assert ok.status_code == 200 and ok.json()["uci"]
        bad = client.post("/api/play/reply", json={"fen": "nope"})
        assert bad.status_code == 400 and "valid position" in bad.json()["detail"]

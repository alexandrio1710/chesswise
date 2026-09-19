"""Game import: PGNs pasted or uploaded must produce the same rows a sync does,
and must never store a game that's already there a second time."""

import itertools

import pytest

import coaching_report
import db
import fetchers
import manual_analysis
from db import get_connection
from fetchers import _classify_time_control_from_clock, chesscom_link_id, normalize_pgn_game

_counter = itertools.count(1)

MOVES = "1. e4 {[%clk 0:04:58.9]} e5 {[%clk 0:04:57.1]} 2. Nf3 {[%clk 0:04:55]} Nc6 3. Bb5 a6 1-0"


def _chesscom_pgn(link_id: str = "174390743510", white="alice", black="bob") -> str:
    return (
        '[Event "Live Chess"]\n[Site "Chess.com"]\n[Date "2026.09.12"]\n[White "%s"]\n[Black "%s"]\n[Result "1-0"]\n'
        '[UTCDate "2026.09.12"]\n[UTCTime "22:33:04"]\n[WhiteElo "1500"]\n[BlackElo "1480"]\n[TimeControl "300"]\n'
        '[ECOUrl "https://www.chess.com/openings/Ruy-Lopez-Opening"]\n[Link "https://www.chess.com/game/live/%s"]\n\n%s\n'
        % (white, black, link_id, MOVES))


def _lichess_pgn(site_id: str = "AbCdEf12", white="alice", black="bob") -> str:
    return (
        '[Event "Rated Blitz game"]\n[Site "https://lichess.org/%s"]\n[Date "2026.09.12"]\n[White "%s"]\n[Black "%s"]\n[Result "0-1"]\n'
        '[UTCDate "2026.09.12"]\n[UTCTime "10:00:00"]\n[WhiteElo "1600"]\n[BlackElo "1650"]\n[TimeControl "180+2"]\n'
        '[Opening "Italian Game"]\n\n1. e4 e5 2. Nf3 Nc6 0-1\n' % (site_id, white, black))


_USERNAMES: dict[int, str] = {}


def _user(pid: int) -> str:
    return _USERNAMES[pid]


def _profile(username: str, source: str = "chesscom") -> int:
    """A profile linked to a fresh, unique username (the shared test DB forbids reusing one)."""
    conn = get_connection()
    try:
        pid = conn.execute("INSERT INTO profiles (name, created_at) VALUES (?, datetime('now'))", (f"imp-{next(_counter)}",)).lastrowid
        conn.execute("INSERT INTO profile_usernames (profile_id, source, username) VALUES (?, ?, ?)", (pid, source, f"{username}{pid}"))
        conn.commit()
        _USERNAMES[pid] = f"{username}{pid}"
        return pid
    finally:
        conn.close()


def _count(where: str, params: tuple = ()) -> int:
    conn = get_connection()
    try:
        return conn.execute(f"SELECT COUNT(*) n FROM games WHERE {where}", params).fetchone()["n"]
    finally:
        conn.close()


class TestTimeControlClassifier:
    @pytest.mark.parametrize("tag, bucket", [
        ("60", "bullet"), ("300", "blitz"), ("600", "rapid"), ("1800", "classical"), ("180+2", "blitz"), ("600+0", "rapid"),
        ("60+1", "bullet"), ("1/86400", "daily"), ("40/7200:3600", "classical"), ("-", "unknown"), ("", "unknown"), ("abc", "unknown"),
    ])
    def test_every_pgn_shape(self, tag, bucket):
        assert _classify_time_control_from_clock(tag) == bucket


class TestNormalizePgnGame:
    def test_chesscom_pgn_gets_the_same_fields_a_sync_would(self):
        g = normalize_pgn_game(_chesscom_pgn(), "white")
        assert (g["source"], g["source_game_id"]) == ("chesscom", "174390743510")
        assert g["date"] == "2026-09-12T22:33:04+00:00"  # full timestamp, not just a date
        assert g["opening_name"] == "Ruy Lopez Opening"
        assert g["time_control"] == "blitz" and g["result"] == "win" and g["opponent"] == "bob"
        assert (g["player_rating"], g["opponent_rating"]) == (1500, 1480)
        assert "[%clk 0:04:58]" in g["pgn"] and "58.9" not in g["pgn"]  # clocks in the one format the clock analysis reads

    def test_lichess_pgn_keeps_its_real_id(self):
        g = normalize_pgn_game(_lichess_pgn(), "black")
        assert (g["source"], g["source_game_id"], g["result"], g["opening_name"]) == ("lichess", "AbCdEf12", "win", "Italian Game")
        assert g["opponent"] == "alice" and g["time_control"] == "blitz"

    def test_other_pgns_get_the_default_source_and_a_content_hash(self):
        pgn = '[White "a"]\n[Black "b"]\n[Result "1/2-1/2"]\n\n1. d4 d5 1/2-1/2\n'
        g = normalize_pgn_game(pgn, "white", default_source="bulk_upload")
        assert g["source"] == "bulk_upload" and len(g["source_game_id"]) == 16 and g["result"] == "draw"
        assert normalize_pgn_game(pgn, "white", default_source="bulk_upload")["source_game_id"] == g["source_game_id"]

    def test_unfinished_and_tagless_games(self):
        assert normalize_pgn_game(_chesscom_pgn().replace('"1-0"', '"*"'), "white")["result"] is None
        assert normalize_pgn_game("1. e4 e5", "white") is None

    def test_link_id(self):
        assert chesscom_link_id(_chesscom_pgn("42")) == "42" and chesscom_link_id(_lichess_pgn()) is None
        assert chesscom_link_id(_chesscom_pgn().replace("/live/", "/daily/")) == "174390743510"


class TestNoDuplicates:
    def test_importing_a_synced_chesscom_game_is_a_duplicate(self):
        pid = _profile("alice")
        link = f"9{next(_counter):011d}"
        synced = normalize_pgn_game(_chesscom_pgn(link), "white")
        synced["source_game_id"] = "aaaa-bbbb-uuid-%s" % link  # the API keys Chess.com games by uuid
        assert db.save_games([synced], profile_id=pid)["inserted"] == 1

        result = coaching_report.bulk_import_pgn(_chesscom_pgn(link, white=_user(pid)), pid)
        assert (result["inserted"], result["skipped"]) == (0, 1)
        assert _count("pgn LIKE ?", (f'%/game/live/{link}"%',)) == 1

    def test_syncing_a_game_that_was_imported_is_a_duplicate_too(self):
        pid = _profile("alice")
        link = f"8{next(_counter):011d}"
        assert coaching_report.bulk_import_pgn(_chesscom_pgn(link, white=_user(pid)), pid)["inserted"] == 1
        synced = normalize_pgn_game(_chesscom_pgn(link), "white")
        synced["source_game_id"] = "some-api-uuid-%s" % link
        assert db.save_games([synced], profile_id=pid) == {"inserted": 0, "skipped": 1, "skipped_other_profile": 0}

    def test_a_lichess_game_already_synced_is_not_stored_again(self):
        pid = _profile("alice", "lichess")
        site = f"L{next(_counter):07d}"
        db.save_games([normalize_pgn_game(_lichess_pgn(site, white=_user(pid)), "white")], profile_id=pid)
        result = coaching_report.bulk_import_pgn(_lichess_pgn(site, white=_user(pid)), pid)
        assert (result["inserted"], result["skipped"]) == (0, 1)
        assert _count("source_game_id = ?", (site,)) == 1

    def test_a_duplicate_under_another_profile_is_reported_as_such(self):
        owner, other = _profile("alice"), _profile("carol")
        link = f"7{next(_counter):011d}"
        coaching_report.bulk_import_pgn(_chesscom_pgn(link, white=_user(owner)), owner)
        game = normalize_pgn_game(_chesscom_pgn(link), "white")
        assert db.save_games([game], profile_id=other)["skipped_other_profile"] == 1


class TestManualPaste:
    @pytest.fixture(autouse=True)
    def _no_engine(self, monkeypatch):
        import mistakes
        self.analyzed = []
        monkeypatch.setattr(mistakes, "analyze_and_store_game", lambda gid, pgn, *a, **k: self.analyzed.append(gid))

    def test_pasting_an_already_synced_game_returns_the_existing_row(self):
        pid = _profile("alice")
        link = f"6{next(_counter):011d}"
        synced = normalize_pgn_game(_chesscom_pgn(link), "white")
        synced["source_game_id"] = "uuid-%s" % link
        db.save_games([synced], profile_id=pid)
        existing = db.find_game_id(synced)
        assert manual_analysis.save_manual_game(_chesscom_pgn(link), "white", profile_id=pid) == existing
        assert self.analyzed == []  # not re-analyzed, not duplicated

    def test_a_new_site_game_is_saved_with_its_real_source_and_metadata(self):
        pid = _profile("alice")
        link = f"5{next(_counter):011d}"
        gid = manual_analysis.save_manual_game(_chesscom_pgn(link), "white", profile_id=pid)
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM games WHERE id = ?", (gid,)).fetchone()
        finally:
            conn.close()
        assert (row["source"], row["time_control"], row["opening_name"]) == ("chesscom", "blitz", "Ruy Lopez Opening")
        assert row["date"] == "2026-09-12T22:33:04+00:00" and self.analyzed == [gid]

    def test_bare_movetext_and_stray_whitespace_are_accepted(self):
        pid = _profile("x")
        gid = manual_analysis.save_manual_game("\n\n   1. e4 e5 2. Nf3 Nc6 3. Bb5 a6", "white", "Rival", profile_id=pid)
        conn = get_connection()
        try:
            row = conn.execute("SELECT source, opponent, result, pgn FROM games WHERE id = ?", (gid,)).fetchone()
        finally:
            conn.close()
        assert (row["source"], row["opponent"], row["result"]) == ("manual", "Rival", "draw")
        assert row["pgn"].startswith("[Event") and "1. e4 e5" in row["pgn"]

    def test_pasting_the_same_otb_game_twice_makes_two_rows(self):
        pid = _profile("x")
        pgn = '[White "me"]\n[Black "you"]\n[Result "1-0"]\n\n1. d4 d5 1-0'
        assert manual_analysis.save_manual_game(pgn, "white", profile_id=pid) != manual_analysis.save_manual_game(pgn, "white", profile_id=pid)

    def test_multi_game_paste_saves_the_first_and_counts_the_rest(self):
        text = _lichess_pgn("Aaaaaaaa") + "\n\n" + _lichess_pgn("Bbbbbbbb")
        first, n = manual_analysis.first_game_of(text)
        assert n == 2 and "Aaaaaaaa" in first and "Bbbbbbbb" not in first
        assert manual_analysis.first_game_of("   " + _lichess_pgn())[1] == 1


class TestFirstImportSize:
    def test_a_new_profiles_first_refresh_pulls_real_history(self, monkeypatch):
        calls = {}
        monkeypatch.setattr(fetchers, "fetch_lichess_games", lambda user, max_games=20, since_ms=None: calls.update(li=(max_games, since_ms)) or [])
        monkeypatch.setattr(fetchers, "fetch_chesscom_games", lambda user, months_back=2, max_games=20, since_epoch=None:
                            calls.update(cc=(months_back, max_games, since_epoch)) or [])
        user = f"brandnew{next(_counter)}"
        db.fetch_and_store(user, user + "cc", refresh=True)
        assert calls["li"] == (100, None) and calls["cc"] == (3, 100, None)

    def test_an_explicit_fetch_keeps_its_own_limit(self, monkeypatch):
        calls = {}
        monkeypatch.setattr(fetchers, "fetch_lichess_games", lambda user, max_games=20, since_ms=None: calls.update(li=max_games) or [])
        db.fetch_and_store(f"explicit{next(_counter)}", None, refresh=False, max_games=7)
        assert calls["li"] == 7

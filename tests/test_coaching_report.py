"""Unit tests for coaching_report.py's pure-logic pieces: bulk-import
normalization/dedup, the close-decision/drift-candidate rules, and the
aggregation/narrative helpers. run_multipv_pass/generate_report need a
real Stockfish engine and are covered by live verification instead (same
split as test_integration.py's role for the rest of the analysis
pipeline), not mocked here.
"""

import itertools

import coaching_report
from db import get_connection

_id_counter = itertools.count(1)


def _pgn(white="Alice", black="Bob", result="1-0", time_control="600+0", moves="1. e4 e5 *"):
    return (
        f'[Event "Test"]\n[White "{white}"]\n[Black "{black}"]\n'
        f'[Result "{result}"]\n[TimeControl "{time_control}"]\n[UTCDate "2026.01.15"]\n\n{moves}\n'
    )


def _line(eval_cp=None, mate_in=None, uci="e2e4", san="e4"):
    return {"move_uci": uci, "move_san": san, "eval_cp": eval_cp, "mate_in": mate_in}


class TestNormalizeBulkGame:
    def test_matches_white_username_against_known_usernames(self):
        game = coaching_report._normalize_bulk_game(_pgn(white="alice"), {"alice"}, None)
        assert game["color"] == "white"
        assert game["opponent"] == "Bob"
        assert game["result"] == "win"

    def test_matches_black_username_case_insensitively(self):
        game = coaching_report._normalize_bulk_game(_pgn(white="Zoe", black="Alice"), {"alice"}, None)
        assert game["color"] == "black"
        assert game["result"] == "loss"  # black lost 1-0

    def test_falls_back_to_default_color_when_no_username_matches(self):
        game = coaching_report._normalize_bulk_game(_pgn(), set(), "white")
        assert game["color"] == "white"

    def test_unattributable_game_returns_none(self):
        assert coaching_report._normalize_bulk_game(_pgn(), set(), None) is None

    def test_unterminated_result_returns_none(self):
        game = coaching_report._normalize_bulk_game(_pgn(result="*"), {"alice"}, None)
        assert game is None

    def test_source_hash_is_content_only_not_timestamp_salted(self):
        # Unlike manual_analysis.save_manual_game's per-paste hash, the
        # same PGN text must hash identically every time — that's the
        # whole dedup mechanism for re-uploading an overlapping export.
        pgn = _pgn(white="alice")
        first = coaching_report._normalize_bulk_game(pgn, {"alice"}, None)
        second = coaching_report._normalize_bulk_game(pgn, {"alice"}, None)
        assert first["source_game_id"] == second["source_game_id"]

    def test_source_is_bulk_upload(self):
        game = coaching_report._normalize_bulk_game(_pgn(white="alice"), {"alice"}, None)
        assert game["source"] == "bulk_upload"


class TestBulkImportPgn:
    def test_reimporting_the_same_export_dedupes(self):
        profile_id, username = _new_profile_with_username()
        pgn_text = _pgn(white=username) + "\n" + _pgn(white=username, black="Carol", moves="1. d4 d5 *")

        first = coaching_report.bulk_import_pgn(pgn_text, profile_id)
        second = coaching_report.bulk_import_pgn(pgn_text, profile_id)

        assert first["inserted"] == 2
        assert second["inserted"] == 0
        assert second["skipped"] == 2

    def test_unattributable_games_are_counted_but_not_saved(self):
        profile_id, _ = _new_profile_with_username()
        result = coaching_report.bulk_import_pgn(_pgn(white="someone_else", black="someone_else_2"), profile_id)
        assert result["parsed"] == 1
        assert result["unattributable"] == 1
        assert result["inserted"] == 0


def _new_profile_with_username() -> tuple[int, str]:
    username = f"test-user-{next(_id_counter)}"
    conn = get_connection()
    try:
        profile_id = conn.execute(
            "INSERT INTO profiles (name, created_at) VALUES (?, datetime('now'))",
            (username,),
        ).lastrowid
        conn.execute(
            "INSERT INTO profile_usernames (profile_id, source, username) VALUES (?, 'lichess', ?)",
            (profile_id, username),
        )
        conn.commit()
        return profile_id, username
    finally:
        conn.close()


class TestIsCloseDecision:
    def test_fewer_than_two_lines_is_never_close(self):
        assert coaching_report.is_close_decision([_line(eval_cp=50)]) is False

    def test_two_lines_within_threshold_is_close(self):
        lines = [_line(eval_cp=100), _line(eval_cp=90)]
        assert coaching_report.is_close_decision(lines, threshold=20) is True

    def test_two_lines_far_apart_is_not_close(self):
        lines = [_line(eval_cp=100), _line(eval_cp=20)]
        assert coaching_report.is_close_decision(lines, threshold=20) is False

    def test_mate_lines_are_treated_as_a_large_finite_value(self):
        lines = [_line(mate_in=2), _line(mate_in=3)]
        assert coaching_report.is_close_decision(lines, threshold=20) is True


class TestIsDriftCandidate:
    _CLOSE_LINES = [_line(eval_cp=100, uci="e2e4"), _line(eval_cp=90, uci="d2d4")]

    def test_playing_the_top_choice_is_never_a_drift_candidate(self):
        assert coaching_report.is_drift_candidate(self._CLOSE_LINES, "e2e4", eval_drop=10, tier="good") is False

    def test_playing_a_close_alternative_with_low_eval_drop_is_a_drift_candidate(self):
        assert coaching_report.is_drift_candidate(self._CLOSE_LINES, "d2d4", eval_drop=10, tier="good") is True

    def test_an_already_flagged_mistake_is_not_also_a_drift_candidate(self):
        assert coaching_report.is_drift_candidate(self._CLOSE_LINES, "d2d4", eval_drop=10, tier="mistake") is False

    def test_eval_drop_above_the_ceiling_is_not_a_drift_candidate(self):
        assert coaching_report.is_drift_candidate(self._CLOSE_LINES, "d2d4", eval_drop=999, tier="good") is False

    def test_a_position_with_no_close_alternative_is_not_a_drift_candidate(self):
        lines = [_line(eval_cp=500, uci="e2e4"), _line(eval_cp=50, uci="d2d4")]
        assert coaching_report.is_drift_candidate(lines, "d2d4", eval_drop=10, tier="good") is False

    def test_no_lines_at_all_is_not_a_drift_candidate(self):
        assert coaching_report.is_drift_candidate([], "e2e4", eval_drop=10, tier="good") is False


class TestHeadline:
    def test_too_few_games_gives_the_not_enough_data_message(self):
        stats = {"rapid": {"games": 1, "acpl": 20, "win_rate_pct": 100.0, "drift_rate": 5.0}}
        assert "not enough games" in coaching_report._headline(stats)

    def test_acpl_and_win_rate_pointing_the_same_way_notes_the_drift_leader(self):
        stats = {
            "rapid": {"games": 5, "acpl": 20, "win_rate_pct": 70.0, "drift_rate": 10.0},
            "blitz": {"games": 5, "acpl": 40, "win_rate_pct": 50.0, "drift_rate": 5.0},
        }
        headline = coaching_report._headline(stats)
        assert "rapid" in headline and "10.0" in headline

    def test_mismatched_acpl_and_win_rate_leaders_is_the_headline_finding(self):
        stats = {
            "rapid": {"games": 5, "acpl": 20, "win_rate_pct": 40.0, "drift_rate": 12.0},
            "bullet": {"games": 5, "acpl": 60, "win_rate_pct": 70.0, "drift_rate": 3.0},
        }
        headline = coaching_report._headline(stats)
        assert "rapid" in headline and "bullet" in headline
        assert "different directions" in headline


class TestTrendNote:
    def test_no_previous_report_returns_none(self):
        assert coaching_report._trend_note({"rapid": {"drift_rate": 5.0}}, None) is None

    def test_meaningful_improvement_is_reported(self):
        current = {"rapid": {"drift_rate": 5.0}}
        previous = {"rapid": {"drift_rate": 15.0}}
        note = coaching_report._trend_note(current, previous)
        assert "down" in note and "rapid" in note

    def test_tiny_change_is_treated_as_no_meaningful_change(self):
        current = {"rapid": {"drift_rate": 10.2}}
        previous = {"rapid": {"drift_rate": 10.0}}
        assert coaching_report._trend_note(current, previous) == "No meaningful change in drift-candidate rate vs. the last batch."


class TestWhatToWorkOn:
    def test_deduplicates_checklist_items_across_structures(self):
        matches = {
            "dragon_opposite_castling": {"checklist": ["A", "B"]},
            "kid_mar_del_plata": {"checklist": ["B", "C"]},
        }
        assert coaching_report._what_to_work_on(matches) == ["A", "B", "C"]

    def test_no_structures_gives_an_empty_list(self):
        assert coaching_report._what_to_work_on({}) == []


class TestAggregateByTimeControl:
    def test_win_rate_and_games_are_tallied_per_time_control(self):
        games = [
            {"id": 1, "time_control": "rapid", "result": "win", "color": "white"},
            {"id": 2, "time_control": "rapid", "result": "loss", "color": "white"},
        ]
        stats = coaching_report._aggregate_by_time_control(games, drift_by_game={1: [], 2: []})
        assert stats["rapid"]["games"] == 2
        assert stats["rapid"]["win_rate_pct"] == 50.0

    def test_drift_rate_is_per_own_move_not_per_game(self):
        games = [{"id": 1, "time_control": "rapid", "result": "win", "color": "white"}]
        drift_by_game = {1: [
            {"is_drift_candidate": True}, {"is_drift_candidate": False}, {"is_drift_candidate": False}, {"is_drift_candidate": False},
        ]}
        stats = coaching_report._aggregate_by_time_control(games, drift_by_game)
        assert stats["rapid"]["drift_rate"] == 25.0

    def test_missing_time_control_buckets_as_unknown(self):
        games = [{"id": 1, "time_control": None, "result": "draw", "color": "white"}]
        stats = coaching_report._aggregate_by_time_control(games, drift_by_game={1: []})
        assert "unknown" in stats


class TestHighlightedPositions:
    def test_picks_at_most_one_per_game_before_padding(self):
        drift_by_game = {
            1: [{"game_id": 1, "ply": 5, "is_drift_candidate": True, "eval_before_cp": 0},
                {"game_id": 1, "ply": 9, "is_drift_candidate": True, "eval_before_cp": 0}],
            2: [{"game_id": 2, "ply": 3, "is_drift_candidate": True, "eval_before_cp": 0}],
        }
        picked = coaching_report._highlighted_positions(drift_by_game, limit=2)
        assert {p["game_id"] for p in picked} == {1, 2}

    def test_excludes_already_decided_positions(self):
        drift_by_game = {1: [{"game_id": 1, "ply": 5, "is_drift_candidate": True, "eval_before_cp": 900}]}
        picked = coaching_report._highlighted_positions(drift_by_game)
        assert picked == []

    def test_ignores_non_drift_candidates(self):
        drift_by_game = {1: [{"game_id": 1, "ply": 5, "is_drift_candidate": False, "eval_before_cp": 0}]}
        assert coaching_report._highlighted_positions(drift_by_game) == []


def _insert_game(profile_id: int) -> int:
    conn = get_connection()
    try:
        game_id = conn.execute(
            "INSERT INTO games (source, source_game_id, date, result, color, analyzed, profile_id) "
            "VALUES ('manual', ?, datetime('now'), 'win', 'white', 1, ?)",
            (f"drift-puzzle-test-{next(_id_counter)}", profile_id),
        ).lastrowid
        conn.commit()
        return game_id
    finally:
        conn.close()


def _insert_coaching_report(profile_id: int) -> int:
    conn = get_connection()
    try:
        report_id = conn.execute(
            "INSERT INTO coaching_reports "
            "(profile_id, created_at, game_ids, time_control_stats, structure_stats, headline, full_report) "
            "VALUES (?, datetime('now'), '[]', '{}', '{}', 'test', '{}')",
            (profile_id,),
        ).lastrowid
        conn.commit()
        return report_id
    finally:
        conn.close()


def _highlighted_position(game_id: int, ply: int = 5) -> dict:
    return {
        "game_id": game_id, "ply": ply,
        "fen_before": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "played_move_san": "d4", "played_move_explanation": "The pawn develops the position.",
        "best_move_san": "e4", "best_move_explanation": "The pawn develops the position.",
        "close_alternatives": [
            {"move_uci": "e2e4", "move_san": "e4", "eval_cp": 30, "mate_in": None},
            {"move_uci": "d2d4", "move_san": "d4", "eval_cp": 25, "mate_in": None},
        ],
    }


class TestCreateDriftPuzzles:
    def test_creates_one_puzzle_per_highlighted_position(self):
        profile_id, _ = _new_profile_with_username()
        report_id = _insert_coaching_report(profile_id)
        game_id = _insert_game(profile_id)

        ids = coaching_report.create_drift_puzzles(report_id, [_highlighted_position(game_id)])

        assert len(ids) == 1
        puzzle = coaching_report.get_drift_puzzle(ids[0])
        assert puzzle["game_id"] == game_id
        assert puzzle["best_move_uci"] == "e2e4"
        assert puzzle["top_lines"][0]["move_san"] == "e4"

    def test_re_highlighting_the_same_position_returns_the_same_id_not_a_duplicate(self):
        profile_id, _ = _new_profile_with_username()
        report_id = _insert_coaching_report(profile_id)
        game_id = _insert_game(profile_id)
        position = _highlighted_position(game_id)

        first_ids = coaching_report.create_drift_puzzles(report_id, [position])
        second_report_id = _insert_coaching_report(profile_id)
        second_ids = coaching_report.create_drift_puzzles(second_report_id, [position])

        assert first_ids == second_ids

    def test_ids_align_positionally_even_when_some_already_exist(self):
        # Regression check: create_drift_puzzles must return one id per
        # input position in order, not silently drop entries that
        # INSERT OR IGNORE skipped as already-existing.
        profile_id, _ = _new_profile_with_username()
        report_id = _insert_coaching_report(profile_id)
        game_a, game_b = _insert_game(profile_id), _insert_game(profile_id)
        pos_a, pos_b = _highlighted_position(game_a, ply=3), _highlighted_position(game_b, ply=7)

        coaching_report.create_drift_puzzles(report_id, [pos_a])  # pos_a already exists now
        ids = coaching_report.create_drift_puzzles(report_id, [pos_a, pos_b])

        assert len(ids) == 2
        assert coaching_report.get_drift_puzzle(ids[0])["game_id"] == game_a
        assert coaching_report.get_drift_puzzle(ids[1])["game_id"] == game_b


class TestRecordDriftPuzzleAttempt:
    def test_correct_attempt_increments_both_counters(self):
        profile_id, _ = _new_profile_with_username()
        report_id = _insert_coaching_report(profile_id)
        game_id = _insert_game(profile_id)
        puzzle_id = coaching_report.create_drift_puzzles(report_id, [_highlighted_position(game_id)])[0]

        result = coaching_report.record_drift_puzzle_attempt(puzzle_id, "e2", "e4")

        assert result["correct"] is True
        puzzle = coaching_report.get_drift_puzzle(puzzle_id)
        assert puzzle["attempts"] == 1
        assert puzzle["correct"] == 1

    def test_incorrect_attempt_increments_only_attempts(self):
        profile_id, _ = _new_profile_with_username()
        report_id = _insert_coaching_report(profile_id)
        game_id = _insert_game(profile_id)
        puzzle_id = coaching_report.create_drift_puzzles(report_id, [_highlighted_position(game_id)])[0]

        result = coaching_report.record_drift_puzzle_attempt(puzzle_id, "g1", "f3")

        assert result["correct"] is False
        puzzle = coaching_report.get_drift_puzzle(puzzle_id)
        assert puzzle["attempts"] == 1
        assert puzzle["correct"] == 0

    def test_missing_puzzle_raises_value_error(self):
        import pytest
        with pytest.raises(ValueError):
            coaching_report.record_drift_puzzle_attempt(999999, "e2", "e4")

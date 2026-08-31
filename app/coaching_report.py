"""
Coaching report — bulk PGN ingestion + cross-game pattern mining.

Built for a specific diagnosed problem, not as a generic "run Stockfish on
everything" tool: this player's accuracy is better in rapid than blitz/
bullet, but their rating is worse in rapid relative to their own bullet/
blitz — the opposite of what standard blunder-detection would predict.
The working theory is "quiet strategic drift": sequences of individually
fine moves (low centipawn loss) that are collectively a worse plan than an
available alternative, invisible to single-best-move analysis because it
only flags moves that are objectively bad, not moves that were a fine but
non-optimal choice among several close options.

Two pieces, matching this project's existing division between routine
analysis and on-demand enrichment (see game_report.py's own docstring for
the same reasoning):

1. bulk_import_pgn() — ingest a multi-game PGN export, reusing
   fetchers._split_pgn_blobs and db.save_games rather than a parallel
   import path.
2. generate_report() — a dedicated MultiPV-N pass (own-color moves only)
   over an already-analyzed batch, classifying "drift candidates" (close
   engine decision + not the top choice + not already a mistake/blunder),
   tagging repertoire structures (repertoire.py), aggregating by time
   control, and writing a narrative report — kept out of routine
   analysis/sync so it only costs engine time when actually requested.

None of this tries to decide whether a drift candidate really was the
wrong plan — that needs real chess judgment an engine's win-percentage
number can't supply on its own. Every flagged position is framed as
"worth reviewing," and the repertoire checklists as the specific things to
weigh, not a verdict (see this project's own explicit non-goal: don't
overclaim what analysis can decide).
"""

import io
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone

import chess
import chess.engine
import chess.pgn

import stats
from analysis import get_engine
from config import (
    COACHING_REPORT_DEPTH,
    COACHING_REPORT_MULTIPV,
    DRIFT_CLOSE_DECISION_THRESHOLD_CP,
    DRIFT_MAX_EVAL_DROP_CP,
    STOCKFISH_MAX_SECONDS_PER_POSITION,
)
from db import get_connection, save_games
from eco import classify_game_opening, moves_from_pgn
from fetchers import _classify_time_control_from_clock, _parse_pgn_tags, _split_pgn_blobs
from profiles import _profile_usernames
from repertoire import detect_structure

logger = logging.getLogger(__name__)

BAD_TIERS = {"inaccuracy", "mistake", "blunder"}

# Highlighted positions are picked from roughly-undecided games — a drift
# candidate in an already-blown-out position is far less instructive than
# one in a game that was actually still up for grabs. Same lesson as this
# project's puzzle generator: a quality filter needs checking against real
# data, not just theory (see CHANGELOG v30) — expect to revisit this
# alongside the other drift thresholds during the validation pass.
HIGHLIGHT_MAX_EVAL_BEFORE_CP = 300
MAX_HIGHLIGHTED_POSITIONS = 5


# --- Bulk import -------------------------------------------------------

def _normalize_bulk_game(pgn: str, known_usernames: set[str], default_color: str | None) -> dict | None:
    """One game blob -> db.save_games' normalized shape, or None if it
    can't be parsed/attributed at all. Mirrors manual_analysis.
    save_manual_game's per-field extraction, but the source_game_id is a
    pure content hash (no timestamp) — re-uploading the same export
    should dedupe exactly like a routine sync re-fetch does, unlike a
    single deliberate paste.
    """
    import hashlib

    tags = _parse_pgn_tags(pgn)
    if not tags:
        return None

    white = tags.get("White", "")
    black = tags.get("Black", "")
    white_lower, black_lower = white.lower(), black.lower()

    if white_lower in known_usernames:
        color, opponent = "white", black
    elif black_lower in known_usernames:
        color, opponent = "black", white
    elif default_color in ("white", "black"):
        color = default_color
        opponent = black if color == "white" else white
    else:
        return None  # can't tell which side was this player — skip rather than guess

    result_tag = tags.get("Result", "*")
    result = {"1-0": "win" if color == "white" else "loss",
              "0-1": "loss" if color == "white" else "win",
              "1/2-1/2": "draw"}.get(result_tag)
    if result is None:
        return None  # unterminated/unknown result — nothing meaningful to store

    date_str = tags.get("UTCDate") or tags.get("Date", "")
    date_iso = date_str.replace(".", "-") if date_str and date_str != "????.??.??" else None

    time_control = _classify_time_control_from_clock(tags.get("TimeControl", ""))
    white_elo = int(tags["WhiteElo"]) if tags.get("WhiteElo", "").isdigit() else None
    black_elo = int(tags["BlackElo"]) if tags.get("BlackElo", "").isdigit() else None
    player_rating, opponent_rating = (white_elo, black_elo) if color == "white" else (black_elo, white_elo)

    source_game_id = hashlib.sha256(pgn.encode()).hexdigest()[:16]

    return {
        "source": "bulk_upload", "source_game_id": source_game_id, "date": date_iso,
        "opponent": opponent, "result": result, "color": color, "time_control": time_control,
        "opening_name": tags.get("Opening", ""), "pgn": pgn.strip(),
        "player_rating": player_rating, "opponent_rating": opponent_rating,
    }


def bulk_import_pgn(pgn_text: str, profile_id: int, default_color: str | None = None) -> dict:
    """Split a multi-game PGN blob into individual games and store them
    through the same dedup path every other import already uses
    (db.save_games) — see that function's own docstring for exactly what
    "duplicate" means here. `default_color` is a fallback for games where
    neither White nor Black matches one of this profile's known linked
    usernames (profiles.profile_usernames) — e.g. a raw OTB PGN with no
    recognizable account name at all; a game that still can't be
    attributed even then is skipped, not guessed.

    Returns {"parsed": total blobs found, "attributed": normalized and
    handed to save_games, "unattributable": couldn't tell which side was
    the player, **save_games' own inserted/skipped/skipped_other_profile}.
    """
    conn = get_connection()
    try:
        known_usernames = {u["username"].lower() for u in _profile_usernames(conn, profile_id)}
    finally:
        conn.close()

    blobs = _split_pgn_blobs(pgn_text)
    normalized = []
    for blob in blobs:
        game = _normalize_bulk_game(blob, known_usernames, default_color)
        if game:
            normalized.append(game)

    result = {"parsed": len(blobs), "attributed": len(normalized), "unattributable": len(blobs) - len(normalized)}
    if normalized:
        result.update(save_games(normalized, profile_id=profile_id))
    else:
        result.update({"inserted": 0, "skipped": 0, "skipped_other_profile": 0})
    return result


# --- MultiPV pass / drift-candidate detection ---------------------------

def _multipv_lines(engine: chess.engine.SimpleEngine, board: chess.Board, depth: int, n: int) -> list[dict]:
    """Top-n engine lines at `board`, mover's-own-perspective — same shape
    as puzzles.get_top_lines, but takes an already-open engine so a whole
    game's own-color moves share one Stockfish subprocess instead of
    spinning one up per position."""
    limit = chess.engine.Limit(depth=depth, time=STOCKFISH_MAX_SECONDS_PER_POSITION)
    infos = engine.analyse(board, limit, multipv=n)
    lines = []
    for info in infos:
        move = info["pv"][0]
        score = info["score"].pov(board.turn)
        mate_in = score.mate()
        lines.append({
            "move_uci": move.uci(), "move_san": board.san(move),
            "eval_cp": score.score() if mate_in is None else None,
            "mate_in": mate_in,
        })
    return lines


def _line_cp(line: dict) -> float:
    if line["mate_in"] is not None:
        return 5000.0 if line["mate_in"] > 0 else -5000.0
    return line["eval_cp"]


def is_close_decision(lines: list[dict], threshold: int = DRIFT_CLOSE_DECISION_THRESHOLD_CP) -> bool:
    """True if at least 2 of the top lines are within `threshold`
    centipawns of the best one — the position where plan quality, not
    calculation, is what actually decides the choice."""
    if len(lines) < 2:
        return False
    best = _line_cp(lines[0])
    return sum(1 for line in lines if abs(_line_cp(line) - best) <= threshold) >= 2


def is_drift_candidate(lines: list[dict], played_uci: str, eval_drop: float | None, tier: str | None) -> bool:
    """A drift candidate is additive to existing mistake detection, never
    a replacement for it: the played move must NOT already be flagged as
    an inaccuracy/mistake/blunder (that's a real, objectively-bad move,
    not a "fine but maybe not the best plan" one), and its own eval_drop
    must stay under DRIFT_MAX_EVAL_DROP_CP — the whole point is surfacing
    moves ACPL already calls "fine."
    """
    if not lines or not is_close_decision(lines):
        return False
    if lines[0]["move_uci"] == played_uci:
        return False  # played the top choice — nothing to flag
    if tier in BAD_TIERS:
        return False
    if eval_drop is None or eval_drop > DRIFT_MAX_EVAL_DROP_CP:
        return False
    return True


def run_multipv_pass(game_id: int, pgn_text: str, own_color: str,
                      depth: int = COACHING_REPORT_DEPTH, n: int = COACHING_REPORT_MULTIPV) -> list[dict]:
    """Runs the MultiPV-n pass over one game's own-color moves only —
    opponent moves don't matter for diagnosing THIS player's judgment,
    and skipping them roughly halves the engine work for a batch this
    size. Persists multipv_lines/is_drift_candidate onto game_moves (so
    re-viewing a report never re-runs this), and returns the per-ply
    results for the caller's own aggregation.

    Skips a game whose own-color moves already have multipv_lines stored
    (from an earlier report run) rather than redoing expensive engine
    work — generate_report() relies on this to make re-running a report
    over a growing batch cheap.
    """
    conn = get_connection()
    try:
        move_rows = conn.execute(
            "SELECT ply, color_moved, eval_before_cp, eval_drop, tier, multipv_lines FROM game_moves "
            "WHERE game_id = ? ORDER BY ply",
            (game_id,),
        ).fetchall()
    finally:
        conn.close()

    # Book-theory ply choices aren't the "quiet strategic drift" pattern
    # this tool exists to find — which reasonable opening move to play is
    # a different kind of decision than a mid-plan judgment call, and
    # early-game engine lines are close together constantly (confirmed
    # live: an untuned first pass flagged the game's very first move,
    # d4-vs-e4, as a "drift candidate"). Reuses the exact book-cutoff
    # eco.py/game_report.py already compute elsewhere, rather than a new
    # heuristic — no engine call needed for this part.
    book_ply_cutoff = 0
    eco_match = classify_game_opening(moves_from_pgn(pgn_text))
    if eco_match:
        book_ply_cutoff = eco_match["ply_count"]

    move_rows_by_ply = {r["ply"]: r for r in move_rows}
    own_ply_rows = [r for r in move_rows if r["color_moved"] == own_color and r["ply"] > book_ply_cutoff]
    if own_ply_rows and all(r["multipv_lines"] is not None for r in own_ply_rows):
        return _drift_results_from_rows(game_id, move_rows, own_color)

    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return []

    engine = get_engine()
    updates = []
    results = []
    try:
        board = game.board()
        node = game
        ply = 0
        while node.variations:
            next_node = node.variations[0]
            move = next_node.move
            color_moved = "white" if board.turn == chess.WHITE else "black"
            ply += 1
            row = move_rows_by_ply.get(ply)

            if color_moved != own_color or row is None or ply <= book_ply_cutoff:
                board.push(move)
                node = next_node
                continue

            lines = _multipv_lines(engine, board, depth, n)
            drift = is_drift_candidate(lines, move.uci(), row["eval_drop"], row["tier"])
            updates.append((json.dumps(lines), int(drift), game_id, ply))
            results.append({
                "game_id": game_id, "ply": ply, "is_drift_candidate": drift, "lines": lines,
                "eval_before_cp": row["eval_before_cp"], "eval_drop": row["eval_drop"], "tier": row["tier"],
            })
            board.push(move)
            node = next_node
    finally:
        engine.quit()

    if updates:
        conn = get_connection()
        try:
            conn.executemany(
                "UPDATE game_moves SET multipv_lines = ?, is_drift_candidate = ? "
                "WHERE game_id = ? AND ply = ?",
                updates,
            )
            conn.commit()
        finally:
            conn.close()
    return results


def _drift_results_from_rows(game_id: int, move_rows, own_color: str) -> list[dict]:
    """Rebuilds run_multipv_pass()'s result shape from already-persisted
    game_moves rows — used when a game's MultiPV pass ran on an earlier
    report and doesn't need redoing. FEN/move-SAN detail for whichever
    positions end up highlighted is fetched separately, on demand, by
    _position_detail — replaying every game's PGN here just to build a
    result list most of which is never displayed would be wasted work.
    """
    results = []
    for r in move_rows:
        if r["color_moved"] != own_color or r["multipv_lines"] is None:
            continue
        results.append({
            "game_id": game_id, "ply": r["ply"], "is_drift_candidate": bool(r["is_drift_candidate"]),
            "lines": json.loads(r["multipv_lines"]),
            "eval_before_cp": r["eval_before_cp"], "eval_drop": r["eval_drop"], "tier": r["tier"],
        })
    return results


# --- Structure tagging ---------------------------------------------------

def tag_repertoire_structure(game_id: int, pgn_text: str, opening_name: str | None) -> dict | None:
    """Tags games.repertoire_structure from the PGN alone (no engine
    needed — see repertoire.py), persisting so a re-run report doesn't
    redo it. Returns the same {"structure", "checklist"} repertoire.
    detect_structure gives, or None.
    """
    match = detect_structure(pgn_text, opening_name)
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE games SET repertoire_structure = ? WHERE id = ?",
            (match["structure"] if match else None, game_id),
        )
        conn.commit()
    finally:
        conn.close()
    return match


# --- Aggregation -----------------------------------------------------------

def _aggregate_by_time_control(games: list[dict], drift_by_game: dict[int, list[dict]]) -> dict:
    """{"rapid": {"games": N, "acpl": float|None, "drift_rate": float|None,
    "win_rate_pct": float}, ...} — drift_rate is drift candidates per own
    move (not per game), so it's comparable across batches with different
    average game lengths. win_rate is tracked here (not reused from
    insights.win_rate_by_time_control, which is scoped to a profile's
    entire history, not this specific batch) because the whole point of
    this report is the accuracy-vs-result relationship per time control —
    good rapid ACPL with worse rapid results than bullet/blitz is exactly
    the pattern this tool exists to track over time.
    """
    by_tc: dict[str, dict] = defaultdict(lambda: {"games": 0, "wins": 0, "acpls": [], "drift": 0, "own_moves": 0})
    for g in games:
        tc = g["time_control"] or "unknown"
        bucket = by_tc[tc]
        bucket["games"] += 1
        if g["result"] == "win":
            bucket["wins"] += 1

        moves = stats.get_game_moves(g["id"])
        acpl = stats.compute_game_acpl(moves, g["color"])
        if acpl is not None:
            bucket["acpls"].append(acpl)

        own_results = drift_by_game.get(g["id"], [])
        bucket["own_moves"] += len(own_results)
        bucket["drift"] += sum(1 for r in own_results if r["is_drift_candidate"])

    return {
        tc: {
            "games": b["games"],
            "win_rate_pct": round(100 * b["wins"] / b["games"], 1) if b["games"] else 0.0,
            "acpl": round(sum(b["acpls"]) / len(b["acpls"]), 1) if b["acpls"] else None,
            "drift_rate": round(100 * b["drift"] / b["own_moves"], 1) if b["own_moves"] else None,
        }
        for tc, b in by_tc.items()
    }


def _aggregate_by_structure(games: list[dict], drift_by_game: dict[int, list[dict]]) -> dict:
    by_structure: dict[str, dict] = defaultdict(lambda: {"games": 0, "acpls": [], "drift": 0, "own_moves": 0})
    for g in games:
        structure = g["repertoire_structure"]
        if not structure:
            continue
        bucket = by_structure[structure]
        bucket["games"] += 1
        moves = stats.get_game_moves(g["id"])
        acpl = stats.compute_game_acpl(moves, g["color"])
        if acpl is not None:
            bucket["acpls"].append(acpl)
        own_results = drift_by_game.get(g["id"], [])
        bucket["own_moves"] += len(own_results)
        bucket["drift"] += sum(1 for r in own_results if r["is_drift_candidate"])

    return {
        structure: {
            "games": b["games"],
            "acpl": round(sum(b["acpls"]) / len(b["acpls"]), 1) if b["acpls"] else None,
            "drift_rate": round(100 * b["drift"] / b["own_moves"], 1) if b["own_moves"] else None,
        }
        for structure, b in by_structure.items()
    }


def _highlighted_positions(drift_by_game: dict[int, list[dict]],
                            limit: int = MAX_HIGHLIGHTED_POSITIONS) -> list[dict]:
    """Picks up to `limit` drift candidates to show in full, preferring
    roughly-undecided positions (see HIGHLIGHT_MAX_EVAL_BEFORE_CP) and one
    per game where possible, so the highlights aren't all from a single
    long game."""
    candidates = [
        r for results in drift_by_game.values() for r in results
        if r["is_drift_candidate"]
        and (r["eval_before_cp"] is None or abs(r["eval_before_cp"]) <= HIGHLIGHT_MAX_EVAL_BEFORE_CP)
    ]

    picked: list[dict] = []
    used_games: set[int] = set()
    for c in candidates:
        if len(picked) >= limit:
            break
        if c["game_id"] in used_games:
            continue
        picked.append(c)
        used_games.add(c["game_id"])
    # Pad with leftovers (possibly more than one per game) if there weren't
    # enough distinct games to fill the quota on the first pass.
    for c in candidates:
        if len(picked) >= limit:
            break
        if c not in picked:
            picked.append(c)
    return picked[:limit]


def _position_detail(game_id: int, ply: int, lines: list[dict]) -> dict:
    """FEN + plain-English description for one chosen highlighted
    position — fetched on demand for just the handful of positions that
    end up highlighted, not for every drift candidate (see
    _drift_results_from_rows)."""
    from puzzles import board_before_ply, describe_move

    conn = get_connection()
    try:
        game_row = conn.execute("SELECT pgn FROM games WHERE id = ?", (game_id,)).fetchone()
        move_row = conn.execute(
            "SELECT move_san FROM game_moves WHERE game_id = ? AND ply = ?", (game_id, ply),
        ).fetchone()
    finally:
        conn.close()

    board = board_before_ply(game_row["pgn"], ply)
    played_san = move_row["move_san"]
    try:
        played_explanation = describe_move(board, board.parse_san(played_san))
    except ValueError:
        played_explanation = ""

    best = lines[0]
    best_explanation = describe_move(board, chess.Move.from_uci(best["move_uci"]))

    return {
        "game_id": game_id, "ply": ply, "fen_before": board.fen(),
        "played_move_san": played_san, "played_move_explanation": played_explanation,
        "best_move_san": best["move_san"], "best_move_explanation": best_explanation,
        "close_alternatives": lines,
    }


# --- Narrative -------------------------------------------------------------

def _headline(time_control_stats: dict) -> str:
    """A conservative, data-only headline — states what the numbers show
    (an ACPL/win-rate mismatch, if this batch actually has one) rather
    than asserting a diagnosis the analysis isn't equipped to make.
    """
    eligible = {tc: s for tc, s in time_control_stats.items() if s["games"] >= 3 and s["acpl"] is not None}
    if len(eligible) < 2:
        total_games = sum(s["games"] for s in time_control_stats.values())
        return f"Analyzed {total_games} game(s) across {len(time_control_stats)} time control(s) — not enough games per time control yet for a reliable cross-time-control comparison."

    best_acpl_tc = min(eligible, key=lambda tc: eligible[tc]["acpl"])
    best_win_rate_tc = max(eligible, key=lambda tc: eligible[tc]["win_rate_pct"])

    if best_acpl_tc != best_win_rate_tc:
        acpl_tc_stats = eligible[best_acpl_tc]
        return (
            f"This batch's cleanest move quality was in {best_acpl_tc} (ACPL {acpl_tc_stats['acpl']}), "
            f"but the best results came from {best_win_rate_tc} ({eligible[best_win_rate_tc]['win_rate_pct']}% "
            f"win rate vs. {acpl_tc_stats['win_rate_pct']}% in {best_acpl_tc}) — accuracy and results are "
            f"pointing in different directions, worth a closer look at the drift-candidate rate below."
        )

    highest_drift_tc = max(eligible, key=lambda tc: eligible[tc]["drift_rate"] or 0)
    return (
        f"Accuracy and results tracked together across time controls in this batch — the highest "
        f"drift-candidate rate was in {highest_drift_tc} ({eligible[highest_drift_tc]['drift_rate']}% of own moves)."
    )


def _what_to_work_on(structure_matches: dict[str, dict]) -> list[str]:
    """Deduplicated checklist items pulled from every repertoire structure
    that actually showed up in this batch — concrete, tied to a named
    structure, not generic "study more" advice.
    """
    seen: list[str] = []
    for structure, match in structure_matches.items():
        for item in match["checklist"]:
            if item not in seen:
                seen.append(item)
    return seen


def _previous_report(profile_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT time_control_stats FROM coaching_reports WHERE profile_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (profile_id,),
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row["time_control_stats"]) if row else None


def _trend_note(current: dict, previous: dict | None) -> str | None:
    if previous is None:
        return None
    notes = []
    for tc in current:
        if tc not in previous or current[tc]["drift_rate"] is None or previous[tc]["drift_rate"] is None:
            continue
        delta = current[tc]["drift_rate"] - previous[tc]["drift_rate"]
        if abs(delta) < 1:
            continue
        direction = "down" if delta < 0 else "up"
        notes.append(f"{tc} drift-candidate rate is {direction} {abs(round(delta, 1))} points vs. the last batch")
    if not notes:
        return "No meaningful change in drift-candidate rate vs. the last batch."
    return "Since the last batch: " + "; ".join(notes) + "."


# --- Orchestration -----------------------------------------------------------

def _resolve_games(profile_id: int, game_ids: list[int] | None, since: str | None) -> list[dict]:
    conn = get_connection()
    try:
        if game_ids:
            placeholders = ",".join("?" * len(game_ids))
            rows = conn.execute(
                f"SELECT id, color, result, time_control, pgn, opening_name, repertoire_structure "
                f"FROM games WHERE id IN ({placeholders}) AND profile_id = ? AND analyzed = 1",
                (*game_ids, profile_id),
            ).fetchall()
        else:
            clause = "AND date >= ?" if since else ""
            params = (profile_id, since) if since else (profile_id,)
            rows = conn.execute(
                f"SELECT id, color, result, time_control, pgn, opening_name, repertoire_structure "
                f"FROM games WHERE profile_id = ? AND analyzed = 1 AND skip_reason IS NULL {clause} "
                f"ORDER BY date ASC",
                params,
            ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def generate_report(profile_id: int, game_ids: list[int] | None = None, since: str | None = None) -> dict:
    """Runs the full coaching-report pipeline over a batch of a profile's
    already-analyzed games: MultiPV pass + drift detection (own moves
    only), repertoire structure tagging, time-control/structure
    aggregation, a handful of highlighted positions, and a narrative —
    then persists the result so a later batch can compare against it.

    `game_ids` selects an explicit batch; otherwise every analyzed game
    for this profile since `since` is used (defaulting to the profile's
    last report's timestamp, or its entire history if it has no prior
    report). Raises ValueError if that resolves to zero games.
    """
    if game_ids is None and since is None:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT created_at FROM coaching_reports WHERE profile_id = ? ORDER BY created_at DESC LIMIT 1",
                (profile_id,),
            ).fetchone()
        finally:
            conn.close()
        since = row["created_at"] if row else None

    games = _resolve_games(profile_id, game_ids, since)
    if not games:
        raise ValueError("No analyzed games found for this batch.")

    logger.info(f"Generating coaching report for profile_id={profile_id}: {len(games)} game(s)")
    drift_by_game: dict[int, list[dict]] = {}
    structure_matches: dict[str, dict] = {}
    for i, g in enumerate(games, start=1):
        drift_by_game[g["id"]] = run_multipv_pass(g["id"], g["pgn"], g["color"])
        if g["repertoire_structure"] is None:
            match = tag_repertoire_structure(g["id"], g["pgn"], g["opening_name"])
            g["repertoire_structure"] = match["structure"] if match else None
        else:
            # Already tagged on an earlier run — no engine needed to
            # recover the checklist, so re-derive it directly rather than
            # re-running tag_repertoire_structure's write.
            match = detect_structure(g["pgn"], g["opening_name"])
        if match:
            structure_matches[g["repertoire_structure"]] = match
        if i % 10 == 0 or i == len(games):
            logger.info(f"{i}/{len(games)} game(s) processed")

    time_control_stats = _aggregate_by_time_control(games, drift_by_game)
    structure_stats = _aggregate_by_structure(games, drift_by_game)
    highlighted = _highlighted_positions(drift_by_game)
    highlighted_detail = [
        {**_position_detail(h["game_id"], h["ply"], h["lines"]), "eval_before_cp": h["eval_before_cp"]}
        for h in highlighted
    ]

    report = {
        "profile_id": profile_id,
        "game_count": len(games),
        "headline": _headline(time_control_stats),
        "time_control_stats": time_control_stats,
        "structure_stats": structure_stats,
        "highlighted_positions": highlighted_detail,
        "what_to_work_on": _what_to_work_on(structure_matches),
        "trend_note": _trend_note(time_control_stats, _previous_report(profile_id)),
    }
    report_id, created_at = _persist_report(profile_id, [g["id"] for g in games], report)
    report["id"] = report_id
    report["created_at"] = created_at
    return report


def _persist_report(profile_id: int, game_ids: list[int], report: dict) -> tuple[int, str]:
    conn = get_connection()
    try:
        created_at = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO coaching_reports "
            "(profile_id, created_at, game_ids, time_control_stats, structure_stats, headline, full_report) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                profile_id, created_at, json.dumps(game_ids), json.dumps(report["time_control_stats"]),
                json.dumps(report["structure_stats"]), report["headline"], json.dumps(report),
            ),
        )
        conn.commit()
        return cur.lastrowid, created_at
    finally:
        conn.close()


def get_report(report_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT id, created_at, full_report FROM coaching_reports WHERE id = ?", (report_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {**json.loads(row["full_report"]), "id": row["id"], "created_at": row["created_at"]}


def list_reports(profile_id: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, created_at, headline, game_ids FROM coaching_reports "
            "WHERE profile_id = ? ORDER BY created_at DESC",
            (profile_id,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"id": r["id"], "created_at": r["created_at"], "headline": r["headline"], "game_count": len(json.loads(r["game_ids"]))}
        for r in rows
    ]

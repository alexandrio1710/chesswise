"""
Game review orchestration: the per-game review pass (engine best moves for
every position, see game_report.compute_enriched_classification), the tactic
events Insights counts, and the bulk "review every game" job.

Tactic events are found by pairing what the ENGINE says was best in a
position with what the board says that move is (tactics.move_motifs): a fork
is only an "opportunity" if the engine's best move was that fork AND playing
it out wins material, so a coincidental double-attack isn't counted. Whether
the mover "found" it is then a matter of what they actually played.

Event kinds (game_tactics.motif / outcome):
  fork, pin, skewer, discovered_attack   found | missed
  mate                                   found | missed   (a forced mate was on)
  free_piece                             taken | ignored  (opponent left a piece
                                                          hanging that could be captured)
  left_hanging                           punished | escaped (mover left their own
                                                          piece en prise)
"""

from __future__ import annotations

import io
import logging
import threading
import time

import chess
import chess.pgn

import game_meta
import game_report
from db import get_connection
from mistakes import MATE_SCORE_DETECTION_THRESHOLD_CP
from stats import win_percent
from tactics import hanging_pieces, move_motifs, pv_material_swing, VALUE

logger = logging.getLogger(__name__)

# Motifs that count as a tactical "opportunity" when they're the engine's
# best move and the line wins material.
OPPORTUNITY_MOTIFS = ("fork", "pin", "skewer", "discovered_attack")
# Material (pawns) a line must win for the motif to count as a real chance.
MIN_OPPORTUNITY_SWING = 2
# A played move within this many win-percentage points of the best one is an
# equally good alternative — neither "found" nor "missed" the tactic.
EQUIVALENT_WIN_PERCENT = 3.0


def _most_valuable(board: chess.Board, squares: list[chess.Square]) -> int:
    return max(VALUE[board.piece_type_at(sq)] for sq in squares)


def _captured_value(board_before: chess.Board, move: chess.Move) -> int:
    """Material (pawn units) a move captures; 0 for a quiet move."""
    if board_before.is_en_passant(move):
        return VALUE[chess.PAWN]
    piece = board_before.piece_at(move.to_square)
    return VALUE[piece.piece_type] if piece else 0


def find_tactic_events(pgn_text: str, rows: list[dict]) -> list[dict]:
    """Every tactic event in one game.

    `rows` are the game's per-ply review rows (ply, eval_before_cp — mover's
    perspective —, eval_cp — White's perspective, after the move —,
    best_move_uci, best_pv_uci, best_mate_in). Plies without review data
    still contribute the engine-free events (free pieces, left-hanging).
    """
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return []
    moves = list(game.mainline_moves())
    boards = []
    board = game.board()
    for move in moves:
        boards.append(board.copy())
        board.push(move)
    by_ply = {r["ply"]: r for r in rows}

    events: list[dict] = []
    for ply, move in enumerate(moves, start=1):
        board_before = boards[ply - 1]
        mover = board_before.turn
        color = "white" if mover == chess.WHITE else "black"
        row = by_ply.get(ply)
        after = board_before.copy()
        after.push(move)

        def add(motif: str, outcome: str, gain: float | None, best_uci: str | None = None) -> None:
            events.append({"ply": ply, "color": color, "motif": motif, "outcome": outcome, "gain": gain,
                           "best_uci": best_uci, "played_uci": move.uci()})

        if row is not None and row.get("best_move_uci"):
            before_eval = row["eval_before_cp"]
            after_eval = row["eval_cp"] if mover == chess.WHITE else -row["eval_cp"]
            loss = win_percent(before_eval) - win_percent(after_eval)
            best_uci = row["best_move_uci"]
            try:
                best_move = chess.Move.from_uci(best_uci)
            except ValueError:
                best_move = None
            if best_move is not None and best_move in board_before.legal_moves:
                mate_in = row.get("best_mate_in")
                if mate_in is not None and mate_in > 0:
                    add("mate", "found" if after_eval >= MATE_SCORE_DETECTION_THRESHOLD_CP else "missed", mate_in, best_uci)
                else:
                    best_motifs = move_motifs(board_before, best_move) & set(OPPORTUNITY_MOTIFS)
                    if best_motifs:
                        pv = (row.get("best_pv_uci") or best_uci).split()
                        swing = pv_material_swing(board_before.fen(), pv, mover)
                        if swing >= MIN_OPPORTUNITY_SWING:
                            played_motifs = best_motifs if move == best_move else move_motifs(board_before, move)
                            for motif in sorted(best_motifs):
                                if move == best_move or (motif in played_motifs and loss < EQUIVALENT_WIN_PERCENT):
                                    add(motif, "found", swing, best_uci)
                                elif loss >= EQUIVALENT_WIN_PERCENT:
                                    add(motif, "missed", swing, best_uci)

        # The opponent left something en prise and this side could take it. A
        # piece that only looks loose because the opponent just captured
        # something worth as much (a recapture in a trade) isn't a free piece.
        if not board_before.is_check():
            prev_gain = _captured_value(boards[ply - 2], moves[ply - 2]) if ply >= 2 else 0
            free = [sq for sq in hanging_pieces(board_before, not mover) if VALUE[board_before.piece_type_at(sq)] > prev_gain]
            capturable = [sq for sq in free if any(m.to_square == sq and board_before.is_capture(m)
                                                   for m in board_before.legal_moves)]
            if capturable:
                taken = board_before.is_capture(move) and move.to_square in capturable
                add("free_piece", "taken" if taken else "ignored", _most_valuable(board_before, capturable))

        # This side's own move left a piece en prise — net of whatever the move
        # itself just captured: taking a rook and being recaptured is a trade,
        # not a piece left hanging.
        gained = _captured_value(board_before, move)
        mine = [sq for sq in hanging_pieces(after, mover) if VALUE[after.piece_type_at(sq)] > gained]
        capturable = [sq for sq in mine if any(m.to_square == sq and after.is_capture(m) for m in after.legal_moves)]
        if capturable:
            reply = moves[ply] if ply < len(moves) else None
            punished = reply is not None and after.is_capture(reply) and reply.to_square in capturable
            add("left_hanging", "punished" if punished else "escaped", _most_valuable(after, capturable))

    return events


def extract_tactics(game_id: int) -> int:
    """(Re)compute and store one game's tactic events. Returns the count."""
    conn = get_connection()
    try:
        game = conn.execute("SELECT pgn FROM games WHERE id = ?", (game_id,)).fetchone()
        rows = [dict(r) for r in conn.execute(
            "SELECT ply, color_moved, eval_before_cp, eval_cp, best_move_uci, best_pv_uci, best_mate_in "
            "FROM game_moves WHERE game_id = ? ORDER BY ply", (game_id,)).fetchall()]
    finally:
        conn.close()
    if game is None or not rows:
        return 0
    events = find_tactic_events(game["pgn"], rows)

    conn = get_connection()
    try:
        conn.execute("DELETE FROM game_tactics WHERE game_id = ?", (game_id,))
        conn.executemany(
            "INSERT INTO game_tactics (game_id, ply, color, motif, outcome, gain, best_uci, played_uci) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(game_id, e["ply"], e["color"], e["motif"], e["outcome"], e["gain"], e["best_uci"], e["played_uci"])
             for e in events],
        )
        conn.commit()
    finally:
        conn.close()
    return len(events)


def needs_review(game_id: int) -> bool:
    """True if the game has no completed review pass, or was re-analyzed
    since (its stored best moves would describe the old analysis)."""
    conn = get_connection()
    try:
        game = conn.execute("SELECT analyzed, analyzed_at, reviewed_at FROM games WHERE id = ?", (game_id,)).fetchone()
        if game is None or not game["analyzed"]:
            return False
        if not conn.execute("SELECT 1 FROM game_moves WHERE game_id = ? LIMIT 1", (game_id,)).fetchone():
            return False  # a game that ended before a move was played has nothing to review
        if game["reviewed_at"] is None:
            return True
        if game["analyzed_at"] and game["analyzed_at"] > game["reviewed_at"]:
            return True
        missing = conn.execute(
            "SELECT 1 FROM game_moves WHERE game_id = ? AND best_move_uci IS NULL LIMIT 1", (game_id,)).fetchone()
        return missing is not None
    finally:
        conn.close()


def finalize(game_id: int) -> None:
    """The engine-free steps after the review pass: tactic events and the
    per-game metadata Insights groups by."""
    extract_tactics(game_id)
    game_meta.ensure_metadata([game_id])


def ensure_reviewed(game_id: int) -> None:
    """Run the full review for one game if it needs it (engine pass +
    accuracy/tier report + tactic events + metadata). Blocking — call it from
    a background thread on a request path."""
    if needs_review(game_id):
        game_report.generate_game_report(game_id, force=True)
        finalize(game_id)


# --- bulk job ---------------------------------------------------------------

_bulk_lock = threading.Lock()
bulk_status: dict = {"running": False, "total": 0, "done": 0, "failed": 0, "error": None, "started_at": None}


def games_needing_review(game_ids: list[int] | None = None) -> list[int]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id FROM games WHERE analyzed = 1 AND skip_reason IS NULL "
            "AND EXISTS (SELECT 1 FROM game_moves gm WHERE gm.game_id = games.id) AND ("
            "reviewed_at IS NULL OR analyzed_at > reviewed_at OR EXISTS ("
            "SELECT 1 FROM game_moves gm WHERE gm.game_id = games.id AND gm.best_move_uci IS NULL)) "
            "ORDER BY date DESC"
        ).fetchall()
    finally:
        conn.close()
    ids = [r["id"] for r in rows]
    if game_ids is None:
        return ids
    wanted = set(game_ids)
    return [i for i in ids if i in wanted]


def backfill_reviews(game_ids: list[int] | None = None, progress=None) -> dict:
    """Review every game that still needs it, most recent first. Games are
    processed one at a time (each game's positions already fan out across the
    whole engine pool), so progress is per game and a crash loses at most the
    game in flight. Returns {"reviewed", "failed"}."""
    todo = games_needing_review(game_ids)
    reviewed = failed = 0
    for i, game_id in enumerate(todo, start=1):
        try:
            game_report.generate_game_report(game_id, force=True)
            finalize(game_id)
            reviewed += 1
        except Exception as e:
            failed += 1
            logger.warning(f"Review failed for game {game_id}: {e}")
        if progress:
            progress(i, len(todo), failed)
    return {"reviewed": reviewed, "failed": failed}


def start_bulk_review() -> bool:
    """Kick off backfill_reviews() in a background thread. False if one is
    already running."""
    with _bulk_lock:
        if bulk_status["running"]:
            return False
        todo = games_needing_review()
        bulk_status.update({"running": True, "total": len(todo), "done": 0, "failed": 0, "error": None,
                            "started_at": time.time()})

    def progress(done: int, total: int, failed: int) -> None:
        bulk_status.update({"done": done, "total": total, "failed": failed})

    def run() -> None:
        try:
            backfill_reviews(progress=progress)
        except Exception as e:
            logger.exception("Bulk review failed")
            bulk_status["error"] = str(e)
        finally:
            bulk_status["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return True


# --- the review payload the game page renders -------------------------------

KEY_MOMENT_CLASSES = ("brilliant", "great", "miss", "mistake", "blunder")
PHASES = ("opening", "middlegame", "endgame")
_payload_cache: dict[tuple, dict] = {}


def _mover_pov(eval_cp: float | None, color: str) -> float | None:
    if eval_cp is None:
        return None
    return eval_cp if color == "white" else -eval_cp


def _class_of(row: dict) -> str | None:
    return row.get("classification") or row.get("tier")


def _mistake_causes(game_id: int, you: str, pgn_text: str, rows: list[dict]) -> dict[int, dict]:
    """{ply: {key, label, habit}} for this player's mistakes in the game — why
    each happened (a piece left hanging, a tactic walked into or missed, time
    trouble, ...). The same classification the Skills page aggregates."""
    import insights_report
    import skills

    events = skills._load_tactics([game_id])
    g = {"id": game_id, "color": you, "base_seconds": insights_report.base_seconds(pgn_text)}
    out = {}
    for row in rows:
        if row["color_moved"] != you or (row.get("classification") or row.get("tier")) not in skills.BAD_CLASSES:
            continue
        key = skills.classify_mistake(g, row, events)
        out[row["ply"]] = {"key": key, "label": skills.CAUSES[key]["label"], "habit": skills.CAUSES[key]["habit"]}
    return out


def game_review_payload(game_id: int) -> dict:
    """Everything the review page needs, for BOTH players: accuracy and
    move-class counts per side, phase grades, every move with its
    classification, the engine's best move/line, and a coach explanation,
    plus the key moments and a summary. Caches the (moderately expensive)
    explanation pass per review — see _payload_cache."""
    import explain
    import stats
    from clock_analysis import annotate_time_spent

    game = stats.get_game_detail(game_id)
    if game is None:
        raise ValueError(f"Game {game_id} not found")
    cache_key = (game_id, game.get("reviewed_at"), game.get("analyzed_at"))
    cached = _payload_cache.get(cache_key)
    if cached is not None:
        return cached

    conn = get_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            """
            SELECT gm.ply, gm.move_number, gm.color_moved, gm.move_san, gm.eval_cp, gm.eval_before_cp, gm.eval_drop,
                   gm.tier, gm.classification, gm.phase, gm.clock_seconds_remaining, gm.best_move_uci,
                   gm.best_pv_uci, gm.best_eval_cp, gm.best_mate_in, p.id AS puzzle_id
            FROM game_moves gm
            LEFT JOIN mistakes m ON m.game_id = gm.game_id AND m.ply = gm.ply
            LEFT JOIN puzzles p ON p.mistake_id = m.id
            WHERE gm.game_id = ? ORDER BY gm.ply
            """, (game_id,)).fetchall()]
    finally:
        conn.close()

    pgn = chess.pgn.read_game(io.StringIO(game["pgn"]))
    headers = pgn.headers if pgn else {}
    rows = annotate_time_spent(rows, game["pgn"])
    by_ply = {r["ply"]: r for r in rows}

    board = pgn.board() if pgn else chess.Board()
    start_fen = board.fen()
    moves_out: list[dict] = []
    book_plies = game.get("book_plies") or 0
    for ply, node in enumerate(pgn.mainline() if pgn else [], start=1):
        move = node.move
        row = by_ply.get(ply)
        fen_before = board.fen()
        san = board.san(move)
        board_before = board.copy()
        board.push(move)
        if row is None:
            continue
        color = row["color_moved"]
        classification = _class_of(row) or "good"
        nxt = by_ply.get(ply + 1)
        best_pv = row["best_pv_uci"].split() if row.get("best_pv_uci") else []
        explanation = explain.explain_move(
            board_before, move, classification=classification,
            eval_before_cp=row["eval_before_cp"], eval_after_cp=_mover_pov(row["eval_cp"], color),
            best_uci=row.get("best_move_uci"), best_pv=best_pv, best_mate_in=row.get("best_mate_in"),
            reply_uci=nxt.get("best_move_uci") if nxt else None,
            reply_pv=nxt["best_pv_uci"].split() if nxt and nxt.get("best_pv_uci") else None,
            reply_mate_in=nxt.get("best_mate_in") if nxt else None,
            opening_name=game.get("opening_name"),
        )
        moves_out.append({
            "ply": ply, "move_number": row["move_number"], "color": color, "san": san, "uci": move.uci(),
            "fen_before": fen_before, "fen_after": board.fen(),
            "classification": classification, "tier": row["tier"], "phase": row["phase"],
            "eval_cp": row["eval_cp"], "eval_before_cp": row["eval_before_cp"], "eval_drop": row["eval_drop"],
            "best": ({
                "uci": explanation["best_uci"], "san": explanation["best_san"], "pv_uci": best_pv,
                "cp": row.get("best_eval_cp"), "mate": row.get("best_mate_in"), "effect": explanation["best_effect"],
            } if explanation["best_uci"] else None),
            "explanation": {k: explanation[k] for k in ("headline", "text", "played_is_best", "hint_kind", "motifs")},
            "clock_seconds_remaining": row["clock_seconds_remaining"], "time_spent_seconds": row.get("time_spent_seconds"),
            "puzzle_id": row["puzzle_id"], "is_book": ply <= book_plies,
        })

    causes = _mistake_causes(game_id, game["color"], game["pgn"], rows)
    for m in moves_out:
        if m["ply"] in causes:
            m["cause"] = causes[m["ply"]]

    accuracy = {c: stats.compute_game_accuracy(rows, c) for c in ("white", "black")}
    phase_accuracy = {
        c: {ph: stats.compute_game_accuracy([r for r in rows if r["phase"] == ph], c) for ph in PHASES}
        for c in ("white", "black")
    }
    phase_grades = {c: {ph: explain.grade_from_accuracy(a) for ph, a in phase_accuracy[c].items()} for c in phase_accuracy}
    class_counts = {"white": {}, "black": {}}
    for m in moves_out:
        counts = class_counts[m["color"]]
        counts[m["classification"]] = counts.get(m["classification"], 0) + 1

    you = game["color"]
    key_moments = [m["ply"] for m in moves_out if m["color"] == you and m["classification"] in KEY_MOMENT_CLASSES]

    worst = None
    for m in moves_out:
        if m["color"] != you or m["classification"] not in ("mistake", "blunder", "miss"):
            continue
        loss = win_percent(m["eval_before_cp"]) - win_percent(_mover_pov(m["eval_cp"], you))
        if worst is None or loss > worst[0]:
            worst = (loss, m)
    turning_point = None
    if worst:
        m = worst[1]
        turning_point = {"ply": m["ply"], "move_number": m["move_number"], "san": m["san"],
                         "label": explain.CLASS_LABEL.get(m["classification"], m["classification"])}
    scored = {ph: a for ph, a in phase_accuracy[you].items() if a is not None}
    weakest = min(scored, key=scored.get) if len(scored) > 1 else None

    payload = {
        "game": {
            "id": game["id"], "source": game["source"], "date": game["date"], "opponent": game["opponent"],
            "result": game["result"], "color": you, "time_control": game["time_control"],
            "opening_name": game["opening_name"], "eco": game.get("eco"), "book_plies": book_plies,
            "player_rating": game["player_rating"], "opponent_rating": game["opponent_rating"],
            "termination": game.get("termination_method"), "shape": game.get("game_shape"),
            "termination_label": game_meta.TERMINATION_LABELS.get(game.get("termination_method") or ""),
            "shape_blurb": game_meta.SHAPE_BLURBS.get(game.get("game_shape") or ""),
            "reviewed_at": game.get("reviewed_at"),
        },
        "players": {
            "white": {"name": headers.get("White", "White"), "rating": headers.get("WhiteElo")},
            "black": {"name": headers.get("Black", "Black"), "rating": headers.get("BlackElo")},
        },
        "start_fen": start_fen,
        "accuracy": accuracy,
        "phase_accuracy": phase_accuracy,
        "phase_grades": phase_grades,
        "class_counts": class_counts,
        "moves": moves_out,
        "key_moments": key_moments,
        "turning_point": turning_point,
        "summary": explain.game_summary(
            you=you, accuracy=accuracy, class_counts=class_counts, weakest_phase=weakest,
            turning_point=turning_point, opening=game.get("opening_name"),
        ),
    }
    if len(_payload_cache) > 32:
        _payload_cache.clear()
    _payload_cache[cache_key] = payload
    return payload


class RetryError(ValueError):
    pass


def check_retry(game_id: int, ply: int, from_square: str, to_square: str, promotion: str | None = None) -> dict:
    """Judge a "retry this mistake" attempt: the move the person tries at the
    position before `ply`, against the engine's stored best move there. A
    move outside the stored line gets one quick engine search."""
    import puzzles
    from mistakes import classify_tier

    conn = get_connection()
    try:
        game = conn.execute("SELECT pgn FROM games WHERE id = ?", (game_id,)).fetchone()
        row = conn.execute(
            "SELECT color_moved, eval_before_cp, best_move_uci, best_eval_cp, best_mate_in "
            "FROM game_moves WHERE game_id = ? AND ply = ?", (game_id, ply)).fetchone()
    finally:
        conn.close()
    if game is None or row is None or not row["best_move_uci"]:
        raise RetryError("This position hasn't been reviewed yet.")

    pgn = chess.pgn.read_game(io.StringIO(game["pgn"]))
    board = pgn.board()
    for i, node in enumerate(pgn.mainline(), start=1):
        if i == ply:
            break
        board.push(node.move)
    fen_before = board.fen()

    try:
        move = chess.Move.from_uci(f"{from_square}{to_square}{promotion or ''}")
    except ValueError:
        raise RetryError("That isn't a valid move.")
    if move not in board.legal_moves:
        queen = chess.Move(move.from_square, move.to_square, promotion=chess.QUEEN)
        if queen not in board.legal_moves:
            raise RetryError("That move isn't legal here.")
        move = queen
    san = board.san(move)

    best_line = {"eval_cp": row["best_eval_cp"], "mate_in": row["best_mate_in"]}
    best_cp = puzzles._line_cp(best_line)
    if move.uci() == row["best_move_uci"]:
        verdict, played = "best", {"cp": best_cp, "mate_in": row["best_mate_in"]}
    else:
        played = puzzles.evaluate_move(fen_before, move.uci())
        verdict = classify_tier(best_cp, played["cp"])
    best_move = chess.Move.from_uci(row["best_move_uci"])
    return {
        "correct": verdict in ("best", "excellent"),
        "verdict": verdict,
        "played_san": san,
        "played_uci": move.uci(),
        "played_cp": None if played["mate_in"] is not None else played["cp"],
        "played_mate_in": played["mate_in"],
        "best_san": board.san(best_move),
        "best_uci": row["best_move_uci"],
    }

"""
Stage 7 — FastAPI dashboard server.

One route serves the dashboard page; a few small JSON endpoints feed it,
each accepting an optional `source` filter ('lichess' | 'chesscom' | omitted
for both) so the frontend's All/Lichess/Chess.com toggle can re-query
without the backend knowing anything about how it's rendered.
"""

import json
import logging
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

import alerts
import auth
import cli_state
import clock_analysis
import export
import game_report
import insights
import manual_analysis
import opening_explorer
import opening_puzzles
import profiles
import progress
import puzzles
import srs
import srs_sm2
import stats
import tablebase
from config import SESSION_COOKIE_NAME, SESSION_COOKIE_SECURE, SESSION_TTL_DAYS, STOCKFISH_DEPTH
from db import get_connection

# Celery/Redis (Web platform, Section 4) are optional infrastructure — only
# needed for /api/analyze/*, not for the app's existing analysis paths
# (CLI, batch_analyze.py, the `refresh` button). Importing tasks.py fails if
# `celery`/`redis` aren't installed, or, at task-dispatch time, if Redis
# itself isn't reachable; either way this app should still start and serve
# every other route rather than refusing to boot over an optional feature.
try:
    from tasks import analyze_game_task
except ImportError:
    analyze_game_task = None

logger = logging.getLogger(__name__)

app = FastAPI(title="Chesswise")

STATIC_DIR = Path(__file__).parent / "static"

PHASES = ("opening", "middlegame", "endgame")

# Web-triggered refresh runs in a plain background thread (not a
# multiprocessing pool — spawning one from a request handler that isn't
# behind a `__main__` guard is asking for trouble on Windows). State lives
# in memory since it only needs to survive one server process's lifetime.
#
# Keyed per logged-in user id (or _LOCAL_KEY for the no-login/local-install
# case), NOT one shared dict — a single global status here would mean two
# different users on a shared deployment triggering a refresh around the
# same time read and overwrite each other's progress and results. Keying
# still serializes a given user (or the local install) against itself, same
# as the original single-refresh-at-a-time behavior; different users can now
# run refreshes concurrently, which the DB layer already supports (db.py's
# WAL mode note) and the CLI/batch path already relies on.
_LOCAL_KEY = "local"


def _empty_refresh_status() -> dict:
    return {"running": False, "started_at": None, "finished_at": None, "error": None, "result": None}


_refresh_status: dict[object, dict] = {_LOCAL_KEY: _empty_refresh_status()}


def _normalize_source(source: str | None) -> str | None:
    return None if source in (None, "", "all") else source


@app.get("/health")
def health():
    """Basic liveness/status check — an easy way to confirm a deployed
    instance is up and see how stale its data is, without exposing the
    database itself (see note in README/deployment docs: this route
    intentionally returns only counts and a timestamp, nothing personal).
    """
    conn = get_connection()
    try:
        total_games = conn.execute("SELECT COUNT(*) as n FROM games").fetchone()["n"]
        analyzed_games = conn.execute(
            "SELECT COUNT(*) as n FROM games WHERE analyzed = 1"
        ).fetchone()["n"]
        last_analyzed_at = conn.execute(
            "SELECT MAX(analyzed_at) as t FROM games"
        ).fetchone()["t"]
    finally:
        conn.close()

    return {
        "status": "ok",
        "total_games": total_games,
        "analyzed_games": analyzed_games,
        "last_analysis_run_at": last_analyzed_at,
    }


class SettingsUpdate(BaseModel):
    lichess_user: str | None = None
    chesscom_user: str | None = None


# Per-user override for /api/settings and /api/refresh: cli_state.json
# (a single shared file, by design — see its own docstring) stays the
# CLI-parity store for the no-login/local-install case, unchanged. When a
# session IS logged in, remembered usernames go in this in-memory,
# per-user-id dict instead — otherwise two different logged-in users on a
# shared deployment would read and overwrite the same shared file's
# usernames. Same "survives one process's lifetime" tradeoff as
# _refresh_status above.
_user_settings: dict[int, dict] = {}


def _settings_for(user: dict | None) -> dict:
    return cli_state.load_state() if user is None else dict(_user_settings.get(user["id"], {}))


def _save_settings_for(user: dict | None, lichess_user: str | None, chesscom_user: str | None) -> dict:
    if user is None:
        cli_state.save_state(lichess_user=lichess_user, chesscom_user=chesscom_user)
        return cli_state.load_state()
    current = _user_settings.setdefault(user["id"], {})
    if lichess_user is not None:
        current["lichess_user"] = lichess_user
    if chesscom_user is not None:
        current["chesscom_user"] = chesscom_user
    return dict(current)


@app.get("/api/settings")
def api_get_settings(user: dict | None = Depends(auth.get_current_user_optional)):
    """Currently remembered usernames. Logged out (or no OAuth in play at
    all): the same file the CLI's `refresh`/`digest` commands read from and
    write to (cli_state.py), so setting a username here also makes
    `python cli.py refresh` work without retyping it, and vice versa.
    Logged in: this session's own remembered usernames instead — see
    _user_settings above for why.
    """
    return _settings_for(user)


@app.post("/api/settings")
def api_save_settings(settings: SettingsUpdate, user: dict | None = Depends(auth.get_current_user_optional)):
    return _save_settings_for(user, settings.lichess_user, settings.chesscom_user)


def _run_refresh(key: object, lichess_user: str | None, chesscom_user: str | None) -> None:
    from batch_analyze import run_batch_analysis
    from db import fetch_and_store
    from puzzles import generate_all_puzzles

    try:
        result = fetch_and_store(lichess_user, chesscom_user, refresh=True)
        # Forced sequential: this thread isn't a `__main__`-guarded
        # script, so spawning a multiprocessing pool from inside a
        # running web server is exactly the kind of thing that works on
        # your machine and breaks on someone else's.
        newly_analyzed = run_batch_analysis(workers=1)
        generate_all_puzzles()
        alerts.send_alerts_for_games(newly_analyzed)
        _refresh_status[key]["result"] = result
        _refresh_status[key]["error"] = None
    except Exception as e:
        logger.exception("Web-triggered refresh failed")
        _refresh_status[key]["error"] = str(e)
    finally:
        _refresh_status[key]["running"] = False
        _refresh_status[key]["finished_at"] = datetime.now(timezone.utc).isoformat()


@app.post("/api/refresh")
def api_trigger_refresh(
    settings: SettingsUpdate = SettingsUpdate(), user: dict | None = Depends(auth.get_current_user_optional),
):
    """Kick off fetch (incremental) + analyze in the background and
    return immediately — poll /api/refresh/status for progress. Any
    username given here is saved for next time, same as the CLI.
    """
    key = user["id"] if user else _LOCAL_KEY
    if _refresh_status.get(key, {}).get("running"):
        raise HTTPException(status_code=409, detail="A refresh is already running.")

    state = _settings_for(user)
    lichess_user = settings.lichess_user or state.get("lichess_user")
    chesscom_user = settings.chesscom_user or state.get("chesscom_user")
    if not lichess_user and not chesscom_user:
        raise HTTPException(
            status_code=400,
            detail="No usernames configured. Set at least one in Settings first.",
        )

    if settings.lichess_user or settings.chesscom_user:
        _save_settings_for(user, settings.lichess_user, settings.chesscom_user)

    _refresh_status[key] = {
        "running": True, "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None, "error": None, "result": None,
    }
    threading.Thread(target=_run_refresh, args=(key, lichess_user, chesscom_user), daemon=True).start()
    return {"status": "started"}


@app.get("/api/refresh/status")
def api_refresh_status(user: dict | None = Depends(auth.get_current_user_optional)):
    key = user["id"] if user else _LOCAL_KEY
    return _refresh_status.get(key, _empty_refresh_status())


@app.get("/api/summary")
def api_summary(source: str | None = Query(default=None), profile_id: int | None = Depends(auth.require_profile_filter_access)):
    source = _normalize_source(source)
    return {"takeaway": stats.top_takeaway(source, profile_id), **stats.overall_summary(source, profile_id)}


@app.get("/api/mistakes-by-phase")
def api_mistakes_by_phase(source: str | None = Query(default=None), profile_id: int | None = Depends(auth.require_profile_filter_access)):
    source = _normalize_source(source)
    counts = stats.mistakes_by_phase(source, profile_id)
    return {phase: counts.get(phase, 0) for phase in PHASES}


@app.get("/api/trend")
def api_trend(source: str | None = Query(default=None), profile_id: int | None = Depends(auth.require_profile_filter_access), n_months: int = 6):
    source = _normalize_source(source)
    return {
        "takeaway": stats.trend_takeaway(source, profile_id),
        "months": stats.monthly_trend(source, profile_id, n_months=n_months),
    }


@app.get("/api/openings")
def api_openings(source: str | None = Query(default=None), profile_id: int | None = Depends(auth.require_profile_filter_access)):
    source = _normalize_source(source)
    return {
        "most_played": stats.most_played_openings(source, profile_id, limit=8),
        "best_win_rate": stats.best_win_rate_openings(source, profile_id, limit=8),
        "to_review": stats.openings_to_review(source, profile_id, limit=8),
        "all_families": sorted(
            stats.opening_family_stats(source, profile_id), key=lambda f: f["games_played"], reverse=True
        ),
    }


@app.get("/api/insights")
def api_insights(source: str | None = Query(default=None), profile_id: int | None = Depends(auth.require_profile_filter_access)):
    source = _normalize_source(source)
    return {
        "top_insights": insights.top_insights(source, profile_id),
        "rating_progress": insights.rating_progress(source, profile_id),
        "win_rate_by_color": insights.win_rate_by_color(source, profile_id),
        "win_rate_by_time_control": insights.win_rate_by_time_control(source, profile_id),
        "win_rate_by_day_of_week": insights.win_rate_by_day_of_week(source, profile_id),
        "win_rate_by_time_of_day": insights.win_rate_by_time_of_day(source, profile_id),
        "avg_game_length": insights.avg_game_length_wins_vs_losses(source, profile_id),
        "performance_vs_rating_band": insights.performance_vs_rating_band(source, profile_id),
        "comeback_rate": insights.comeback_rate(source, profile_id),
    }


@app.get("/api/search")
def api_search_games(
    opponent: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    opening: str | None = Query(default=None),
    result: str | None = Query(default=None),
    time_control: str | None = Query(default=None),
    source: str | None = Query(default=None),
    profile_id: int | None = Depends(auth.require_profile_filter_access),
    color: str | None = Query(default=None),
    has_blunder: bool | None = Query(default=None),
    sort_by: str = Query(default="date"),
    sort_dir: str = Query(default="desc"),
    limit: int = 200,
):
    games = stats.search_games(
        opponent=opponent, date_from=date_from, date_to=date_to, opening=opening,
        result=result, time_control=time_control, source=_normalize_source(source),
        profile_id=profile_id, color=color, has_blunder=has_blunder,
        sort_by=sort_by, sort_dir=sort_dir, limit=limit,
    )
    return {
        "games": games,
        "stats": stats.compute_stats_for_game_ids([g["id"] for g in games]),
    }


@app.get("/api/export/games")
def api_export_games(
    format: str = Query(default="json"),
    opponent: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    opening: str | None = Query(default=None),
    result: str | None = Query(default=None),
    time_control: str | None = Query(default=None),
    source: str | None = Query(default=None),
    profile_id: int | None = Depends(auth.require_profile_filter_access),
    color: str | None = Query(default=None),
    has_blunder: bool | None = Query(default=None),
    sort_by: str = Query(default="date"),
    sort_dir: str = Query(default="desc"),
):
    """Same filters as /api/search (Section 6) — this exports exactly
    whatever the Search page's current view shows, with no separate
    filter logic to keep in sync. `limit` is fixed high here rather than
    exposed, since an export should mean "everything that matches", not
    a UI-sized page of results.
    """
    games = stats.search_games(
        opponent=opponent, date_from=date_from, date_to=date_to, opening=opening,
        result=result, time_control=time_control, source=_normalize_source(source),
        profile_id=profile_id, color=color, has_blunder=has_blunder,
        sort_by=sort_by, sort_dir=sort_dir, limit=10000,
    )
    if format == "csv":
        return Response(
            content=export.games_to_csv(games), media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=games.csv"},
        )
    return Response(
        content=json.dumps(games, indent=2), media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=games.json"},
    )


@app.get("/api/export/stats")
def api_export_stats(source: str | None = Query(default=None), profile_id: int | None = Depends(auth.require_profile_filter_access)):
    """Aggregate stats as a single JSON download — nested structure
    (per-phase/per-severity/per-opening breakdowns) doesn't flatten to a
    meaningful CSV, so this is JSON-only, unlike the games export.
    """
    source = _normalize_source(source)
    payload = {
        "overall_summary": stats.overall_summary(source, profile_id),
        "mistakes_by_phase": stats.mistakes_by_phase(source, profile_id),
        "mistakes_by_severity": stats.mistakes_by_severity(source, profile_id),
        "monthly_trend": stats.monthly_trend(source, profile_id),
        "openings": stats.opening_family_stats(source, profile_id),
        "insights": {
            "win_rate_by_color": insights.win_rate_by_color(source, profile_id),
            "win_rate_by_time_control": insights.win_rate_by_time_control(source, profile_id),
            "win_rate_by_day_of_week": insights.win_rate_by_day_of_week(source, profile_id),
            "performance_vs_rating_band": insights.performance_vs_rating_band(source, profile_id),
            "comeback_rate": insights.comeback_rate(source, profile_id),
        },
    }
    return Response(
        content=json.dumps(payload, indent=2), media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=stats.json"},
    )


@app.get("/api/explorer")
def api_explorer(moves: str = Query(default=""), source: str | None = Query(default=None)):
    """`moves` is a comma-separated list of UCI moves from the starting
    position (e.g. "e2e4,e7e5,g1f3") — empty string means the starting
    position itself.
    """
    source = _normalize_source(source)
    move_ucis = [m for m in moves.split(",") if m]
    try:
        return opening_explorer.explore_position(move_ucis, source=source)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid move sequence: {e}")


@app.get("/api/worst-games")
def api_worst_games(source: str | None = Query(default=None), profile_id: int | None = Depends(auth.require_profile_filter_access), limit: int = 5):
    source = _normalize_source(source)
    return stats.worst_games(source, profile_id, limit=limit)


@app.get("/api/games/{game_id}")
def api_game_detail(game_id: int, _access: dict | None = Depends(auth.require_game_access)):
    game = stats.get_game_detail(game_id)
    if game is None:
        raise HTTPException(status_code=404, detail="Game not found")
    if not game["analyzed"]:
        raise HTTPException(status_code=409, detail="This game hasn't been analyzed yet")
    if game["skip_reason"]:
        raise HTTPException(status_code=422, detail=f"Not analyzable: {game['skip_reason']}")

    moves = clock_analysis.annotate_time_spent(stats.get_game_moves(game_id), game["pgn"])
    critical_moment = stats.get_critical_moment(game_id, game["pgn"], game["color"])

    return {
        "game": {
            "id": game["id"], "source": game["source"], "date": game["date"],
            "opponent": game["opponent"], "result": game["result"], "color": game["color"],
            "time_control": game["time_control"], "opening_name": game["opening_name"],
        },
        "moves": moves,
        "accuracy": stats.compute_game_accuracy(moves, game["color"]),
        "critical_moment": critical_moment,
        "notes": stats.get_notes(game_id),
    }


# Game Report jobs, keyed by game_id — same "background thread + in-memory
# status, poll from the frontend" shape as _refresh_status above, for the
# same reason: generating a report costs a second Stockfish pass per move
# (game_report.compute_enriched_classification's docstring — ~0.5s/move,
# so 20-40s for a typical game), too slow to block a request/response
# cycle on, and this app stays single-process/local-in-spirit so plain
# in-memory state (not a job queue) is enough.
_report_jobs: dict[int, dict] = {}


def _run_game_report(game_id: int) -> None:
    try:
        game_report.generate_game_report(game_id, force=True)
        _report_jobs[game_id]["error"] = None
    except Exception as e:
        logger.exception(f"Game report generation failed for game {game_id}")
        _report_jobs[game_id]["error"] = str(e)
    finally:
        _report_jobs[game_id]["running"] = False


@app.get("/api/games/{game_id}/report")
def api_game_report(game_id: int, force: bool = Query(default=False), _access: dict | None = Depends(auth.require_game_access)):
    """Chess.com-style "Game Report": full ten-tier move classification,
    an estimated performance rating, and phase-by-phase accuracy for one
    game (see game_report.py). Poll this same endpoint until `status`
    comes back "ready" — {"status": "computing"} means a background
    thread is working on it (started by this same call, or an earlier
    one); {"status": "failed", "error": ...} means the last attempt
    raised, most commonly because the game hasn't been analyzed yet.
    """
    if stats.get_game_detail(game_id) is None:
        raise HTTPException(status_code=404, detail="Game not found")

    job = _report_jobs.get(game_id)
    if job and job["running"]:
        return {"status": "computing"}

    if not force:
        conn = get_connection()
        try:
            cached = conn.execute("SELECT game_id FROM game_reports WHERE game_id = ?", (game_id,)).fetchone()
        finally:
            conn.close()
        if cached:
            return {"status": "ready", **game_report.generate_game_report(game_id)}
        if job and job["error"]:
            return {"status": "failed", "error": job["error"]}

    _report_jobs[game_id] = {"running": True, "error": None}
    threading.Thread(target=_run_game_report, args=(game_id,), daemon=True).start()
    return {"status": "computing"}


# Coarse per-IP throttle on the two unauthenticated, Stockfish-backed
# Analyze Board endpoints below: each request costs real engine time (a
# full pass per ply for /pgn — see manual_analysis.MAX_ANALYSIS_PLIES),
# and neither route requires login, so without SOME limit here any client
# with network access can pin every core just by looping requests.
# In-memory/per-process, same tradeoff as _refresh_status/_report_jobs
# above — not a substitute for a real rate limiter (e.g. at a reverse
# proxy) in front of a public multi-tenant deployment.
_ANALYZE_RATE_LIMIT = 10
_ANALYZE_RATE_WINDOW_SECONDS = 60.0
_analyze_request_log: dict[str, deque] = defaultdict(deque)
_analyze_rate_lock = threading.Lock()


def _rate_limit_analysis(request: Request) -> None:
    client_ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with _analyze_rate_lock:
        log = _analyze_request_log[client_ip]
        while log and now - log[0] > _ANALYZE_RATE_WINDOW_SECONDS:
            log.popleft()
        if len(log) >= _ANALYZE_RATE_LIMIT:
            raise HTTPException(
                status_code=429,
                detail=f"Too many analysis requests — max {_ANALYZE_RATE_LIMIT} per "
                f"{int(_ANALYZE_RATE_WINDOW_SECONDS)}s. Try again shortly.",
            )
        log.append(now)


class AnalyzeFenRequest(BaseModel):
    fen: str


@app.post("/api/analyze/fen")
def api_analyze_fen(req: AnalyzeFenRequest, _rl: None = Depends(_rate_limit_analysis)):
    try:
        return manual_analysis.analyze_fen(req.fen)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class AnalyzePgnRequest(BaseModel):
    pgn: str
    save: bool = False
    player_color: str | None = None
    opponent: str | None = None


@app.post("/api/analyze/pgn")
def api_analyze_pgn(req: AnalyzePgnRequest, _rl: None = Depends(_rate_limit_analysis)):
    if req.save:
        if req.player_color not in ("white", "black"):
            raise HTTPException(status_code=400, detail="player_color ('white' or 'black') is required to save.")
        try:
            game_id = manual_analysis.save_manual_game(req.pgn, req.player_color, req.opponent)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"saved": True, "game_id": game_id}

    try:
        return {"saved": False, **manual_analysis.analyze_pgn_oneoff(req.pgn)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/mistakes/{mistake_id}/tablebase")
def api_mistake_tablebase(mistake_id: int, _access: dict | None = Depends(auth.require_mistake_access)):
    """For a flagged endgame mistake: was the position tablebase-solvable,
    and did the move played change the theoretical result. On-demand
    (not bundled into /api/games/{id}) since it costs a couple of
    tablebase network calls per mistake — cheap for one, not for a whole
    move list's worth fetched eagerly.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT m.*, g.pgn FROM mistakes m JOIN games g ON m.game_id = g.id WHERE m.id = ?",
            (mistake_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Mistake not found")

    from puzzles import board_before_ply
    board = board_before_ply(row["pgn"], row["ply"])
    move = board.parse_san(row["move_san"])
    result = tablebase.analyze_tablebase_mistake(board.fen(), move.uci())
    if result is None:
        return {"tablebase_solvable": False}
    return result


@app.get("/api/endgame-trainer/positions")
def api_endgame_trainer_positions(source: str | None = Query(default=None), limit: int = 8):
    source = _normalize_source(source)
    return tablebase.find_endgame_trainer_positions(source=source, limit=limit)


class TrainerMove(BaseModel):
    fen: str
    uci: str


@app.post("/api/endgame-trainer/move")
def api_endgame_trainer_move(move: TrainerMove):
    try:
        return tablebase.trainer_attempt_move(move.fen, move.uci)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class NoteCreate(BaseModel):
    text: str
    ply: int | None = None


@app.post("/api/games/{game_id}/notes")
def api_add_note(game_id: int, note: NoteCreate, _access: dict | None = Depends(auth.require_game_access)):
    if not note.text.strip():
        raise HTTPException(status_code=400, detail="Note text can't be empty")
    return stats.add_note(game_id, note.text.strip(), note.ply)


@app.delete("/api/notes/{note_id}")
def api_delete_note(note_id: int, _access: int = Depends(auth.require_note_access)):
    stats.delete_note(note_id)
    return {"status": "deleted"}


@app.get("/api/puzzles/queue")
def api_puzzle_queue(
    source: str | None = Query(default=None),
    mode: str = Query(default="all"),
    phase: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    limit: int = 20,
):
    """mode='all' lists puzzles across every phase (optionally narrowed by
    the explicit `phase`/`severity` filters); mode='practice' auto-picks
    whichever phase has the most mistakes/blunders for this filter, so
    practice sessions focus on the player's actual biggest leak; mode='due'
    returns puzzles due for spaced-repetition review right now (Section 4).
    """
    source = _normalize_source(source)

    if mode == "due":
        due_ids = srs.get_due_puzzle_ids(source=source, phase=phase, severity=severity, limit=limit)
        return {"phase": phase, "puzzles": puzzles.get_puzzles_by_ids(due_ids)}

    if mode == "practice" and not phase:
        phase = stats.worst_mistake_phase(source)

    return {
        "phase": phase,
        "puzzles": puzzles.get_puzzle_queue(source, phase=phase, severity=severity, limit=limit),
    }


@app.get("/api/puzzles/review-stats")
def api_puzzle_review_stats(source: str | None = Query(default=None)):
    return srs.get_review_stats(source=_normalize_source(source))


@app.get("/api/puzzles/{puzzle_id}")
def api_get_puzzle(puzzle_id: int, _access: dict | None = Depends(auth.require_puzzle_access)):
    puzzle = puzzles.get_puzzle(puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")

    # Deliberately omit best_move_san / top_lines / played_move_san — those
    # are the answer, and are only revealed via the /attempt response.
    return {
        "id": puzzle["id"],
        "fen_before": puzzle["fen_before"],
        "side_to_move": puzzle["side_to_move"],
        "legal_moves": puzzles.legal_moves_for_fen(puzzle["fen_before"]),
        "phase": puzzle["phase"],
        "severity": puzzle["severity"],
        "source": puzzle["source"],
        "date": puzzle["date"],
        "opponent": puzzle["opponent"],
        "time_control": puzzle["time_control"],
        "opening_name": puzzle["opening_name"],
    }


class PuzzleAttempt(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_square: str = Field(alias="from")
    to_square: str = Field(alias="to")
    time_taken_ms: int | None = None
    session_type: str = "practice"  # 'practice' | 'rush' | 'review'


@app.post("/api/puzzles/{puzzle_id}/attempt")
def api_puzzle_attempt(puzzle_id: int, attempt: PuzzleAttempt, _access: dict | None = Depends(auth.require_puzzle_access)):
    puzzle = puzzles.get_puzzle(puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")

    try:
        result = puzzles.check_attempt(puzzle, attempt.from_square, attempt.to_square)
    except puzzles.IllegalMoveError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Every attempt, from every mode, feeds the same spaced-repetition
    # history — recorded server-side so nothing depends on the frontend
    # remembering to log it separately.
    srs_state = srs.record_attempt(
        puzzle_id, result["correct"], attempt.time_taken_ms, attempt.session_type
    )
    result["srs"] = srs_state
    return result


# --- Opening-based puzzles (Lichess-sourced, user-requested addition) -------

@app.get("/api/opening-puzzles")
def api_opening_puzzles(
    source: str | None = Query(default=None),
    profile_id: int | None = Depends(auth.require_profile_filter_access),
    limit: int = 12,
):
    """Puzzles for the openings the player actually plays most and makes
    the most opening-phase mistakes in (stats.openings_to_review — the
    same "frequency x error rate" ranking Section 7's insights use),
    sourced live from Lichess and cached locally.
    """
    source = _normalize_source(source)
    top_openings = stats.openings_to_review(source, profile_id, min_games=1, limit=3)
    families = [o["family"] for o in top_openings]
    return {
        "openings": families,
        "puzzles": opening_puzzles.get_puzzles_for_openings(families, limit=limit),
    }


@app.get("/api/opening-puzzles/{puzzle_id}")
def api_get_opening_puzzle(puzzle_id: int):
    puzzle = opening_puzzles.get_puzzle(puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    return {
        "id": puzzle["id"],
        "fen": puzzle["fen"],
        "side_to_move": puzzle["side_to_move"],
        "legal_moves": puzzles.legal_moves_for_fen(puzzle["fen"]),
        "opening_family": puzzle["opening_family"],
        "lichess_rating": puzzle["lichess_rating"],
        "themes": puzzle["themes"],
        "game_url": puzzle["game_url"],
        "move_index": 0,
    }


class OpeningPuzzleAttempt(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_square: str = Field(alias="from")
    to_square: str = Field(alias="to")
    move_index: int = 0


@app.post("/api/opening-puzzles/{puzzle_id}/attempt")
def api_opening_puzzle_attempt(puzzle_id: int, attempt: OpeningPuzzleAttempt):
    try:
        return opening_puzzles.attempt_move(
            puzzle_id, attempt.move_index, attempt.from_square, attempt.to_square
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/puzzles", response_class=HTMLResponse)
def puzzles_page():
    return (STATIC_DIR / "puzzles.html").read_text(encoding="utf-8")


@app.get("/game", response_class=HTMLResponse)
def game_page():
    return (STATIC_DIR / "game.html").read_text(encoding="utf-8")


@app.get("/explorer", response_class=HTMLResponse)
def explorer_page():
    return (STATIC_DIR / "explorer.html").read_text(encoding="utf-8")


@app.get("/endgame", response_class=HTMLResponse)
def endgame_page():
    return (STATIC_DIR / "endgame.html").read_text(encoding="utf-8")


@app.get("/analyze", response_class=HTMLResponse)
def analyze_page():
    return (STATIC_DIR / "analyze.html").read_text(encoding="utf-8")


@app.get("/search", response_class=HTMLResponse)
def search_page():
    return (STATIC_DIR / "search.html").read_text(encoding="utf-8")


@app.get("/insights", response_class=HTMLResponse)
def insights_page():
    return (STATIC_DIR / "insights.html").read_text(encoding="utf-8")


@app.get("/api/clock-analysis")
def api_clock_analysis(source: str | None = Query(default=None), profile_id: int | None = Depends(auth.require_profile_filter_access)):
    source = _normalize_source(source)
    return {
        "avg_thinking_time_by_tier": clock_analysis.avg_thinking_time_by_tier(source, profile_id),
        "pressure": clock_analysis.clock_pressure_games(source, profile_id),
    }


@app.get("/clock", response_class=HTMLResponse)
def clock_page():
    return (STATIC_DIR / "clock.html").read_text(encoding="utf-8")


# --- Multi-profile support (Advanced features, Section 9) -------------------

class ProfileCreate(BaseModel):
    name: str


class UsernameLink(BaseModel):
    source: str
    username: str


@app.get("/api/profiles")
def api_list_profiles(user: dict | None = Depends(auth.get_current_user_optional)):
    return profiles.list_profiles(user_id=user["id"] if user else None)


@app.post("/api/profiles")
def api_create_profile(body: ProfileCreate):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Profile name can't be empty")
    try:
        return profiles.create_profile(name)
    except Exception:
        raise HTTPException(status_code=409, detail=f"A profile named '{name}' already exists")


@app.delete("/api/profiles/{profile_id}")
def api_delete_profile(profile_id: int, _access: dict | None = Depends(auth.require_profile_access)):
    profiles.delete_profile(profile_id)
    return {"status": "deleted"}


@app.post("/api/profiles/{profile_id}/links")
def api_link_username(profile_id: int, body: UsernameLink, _access: dict | None = Depends(auth.require_profile_access)):
    if body.source not in ("lichess", "chesscom"):
        raise HTTPException(status_code=400, detail="source must be 'lichess' or 'chesscom'")
    username = body.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username can't be empty")
    return profiles.link_username(profile_id, body.source, username)


@app.delete("/api/profiles/{profile_id}/links")
def api_unlink_username(
    profile_id: int, source: str = Query(...), username: str = Query(...),
    _access: dict | None = Depends(auth.require_profile_access),
):
    profiles.unlink_username(profile_id, source, username)
    return {"status": "unlinked"}


@app.get("/api/profiles/compare")
def api_compare_profiles(
    a: int = Query(...), b: int = Query(...), user: dict | None = Depends(auth.get_current_user_optional),
):
    auth.verify_can_access_profile(a, user)
    auth.verify_can_access_profile(b, user)
    try:
        return profiles.compare_profiles(a, b)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/profiles", response_class=HTMLResponse)
def profiles_page():
    return (STATIC_DIR / "profiles.html").read_text(encoding="utf-8")


# --- Progress, goals, auto-reports (Section 10) -----------------------------

@app.get("/api/progress")
def api_progress(source: str | None = Query(default=None), profile_id: int | None = Depends(auth.require_profile_filter_access)):
    source = _normalize_source(source)
    return {
        "weekly_summary": progress.weekly_summary(source, profile_id),
        "narrative": progress.generate_narrative(source, profile_id),
        "goals": progress.list_goals(profile_id),
    }


class GoalCreate(BaseModel):
    description: str
    metric: str
    comparison: str
    target_value: float
    phase: str | None = None
    source: str | None = None
    profile_id: int | None = None


@app.post("/api/goals")
def api_create_goal(body: GoalCreate, user: dict | None = Depends(auth.get_current_user_optional)):
    if not body.description.strip():
        raise HTTPException(status_code=400, detail="Description can't be empty")
    if body.profile_id is not None:
        auth.verify_can_access_profile(body.profile_id, user)
    try:
        return progress.create_goal(
            body.description.strip(), body.metric, body.comparison, body.target_value,
            phase=body.phase, source=_normalize_source(body.source), profile_id=body.profile_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/goals/{goal_id}")
def api_delete_goal(goal_id: int, _access: None = Depends(auth.require_goal_access)):
    progress.delete_goal(goal_id)
    return {"status": "deleted"}


@app.get("/progress", response_class=HTMLResponse)
def progress_page():
    return (STATIC_DIR / "progress.html").read_text(encoding="utf-8")


# --- Web platform: Lichess OAuth, per-user SM-2 SRS, background analysis --
#
# Additive, not a rewrite: every route above this line still works with no
# login for a local install, exactly as it did before OAuth existed —
# they're now gated by auth.require_*_access (games, notes, goals,
# profiles, puzzles, mistakes), which lets unowned rows (user_id IS NULL:
# every row from a local/never-logged-in install) through to anyone, but
# 404s an owned row for anyone except the user who owns it. The routes
# below are the fully multi-user-aware surface: they all require a session
# unconditionally (a request with no/expired cookie gets a 401 from
# auth.get_current_user) and are scoped to the requesting user's own data
# via auth.require_game_owner / auth.require_puzzle_owner or an explicit
# `WHERE user_id = ?` filter.
#
# Note: every route above that takes an explicit `profile_id` FILTER
# (dashboard, insights, search, export, puzzle queue, progress/goals) now
# validates it through auth.require_profile_filter_access, so
# `?profile_id=<someone else's>` can't be used to read another user's
# profile-scoped data. What's still NOT user-scoped is the UNFILTERED
# case: calling one of these with no profile_id and no source at all still
# aggregates every profile's data in the database together, local install
# or not — narrowing that default would mean threading `user_id` through
# stats.py/insights.py/srs.py themselves (not just validating a parameter),
# which is a larger, mostly-mechanical follow-up beyond this pass.
#
# Bigger caveat than either of the above: nothing on the *ingestion* side
# (fetch_and_store/save_games, the /api/refresh path) tags a freshly-fetched
# game with user_id yet — only claim_unowned_data does, and only in one
# retroactive bulk sweep triggered by /api/me/claim. So on a real multi-user
# deployment, games pulled in after login via the existing Refresh button
# still land unowned (and therefore world-readable by the rule above) until
# someone claims them. The access checks here are necessary but not
# sufficient for safe multi-tenant use; making ingestion itself user_id-aware
# is a separate, larger follow-up.

@app.get("/auth/lichess/login")
def auth_lichess_login():
    return RedirectResponse(auth.build_authorize_url())


@app.get("/auth/lichess/callback")
def auth_lichess_callback(code: str, state: str):
    access_token = auth.exchange_code_for_token(code, state)
    account = auth.fetch_lichess_account(access_token)
    user = auth.upsert_user(account)
    raw_token, _ = auth.create_session(user["id"])

    response = RedirectResponse(url="/")
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        max_age=SESSION_TTL_DAYS * 86400,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
    )
    return response


@app.post("/auth/logout")
def auth_logout(request: Request):
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if raw_token:
        auth.destroy_session(raw_token)
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/api/me")
def api_me(user: dict = Depends(auth.get_current_user)):
    return {"id": user["id"], "username": user["username"], "lichess_id": user["lichess_id"]}


@app.post("/api/me/claim")
def api_me_claim(user: dict = Depends(auth.get_current_user)):
    """One-time convenience for a pre-existing single-user install: assigns
    every profile/game/puzzle with no owner yet to the logged-in user. See
    auth.claim_unowned_data's docstring for why this is opt-in, not automatic.
    """
    return auth.claim_unowned_data(user["id"])


@app.post("/api/analyze/start")
def api_analyze_start(user: dict = Depends(auth.get_current_user), depth: int = Query(default=STOCKFISH_DEPTH)):
    """Queues every one of the current user's not-yet-analyzed games onto
    Celery. Requires a running worker (see celery_app.py's docstring) and a
    reachable Redis — a connection failure surfaces as a 503 rather than a
    silently-stuck 'processing' row.
    """
    if analyze_game_task is None:
        raise HTTPException(
            status_code=501,
            detail="Background analysis isn't available: `celery`/`redis` aren't installed. "
            "Run `pip install -r requirements.txt` and start a worker (see celery_app.py).",
        )

    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id FROM games WHERE user_id = ? AND (analysis_status IS NULL OR analysis_status = 'pending')",
            (user["id"],),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return {"queued": 0}

    conn = get_connection()
    queued = 0
    try:
        for row in rows:
            game_id = row["id"]
            try:
                async_result = analyze_game_task.apply_async(args=[game_id, user["id"], depth])
            except Exception as e:
                # Commit whatever was already dispatched before this one
                # failed — those tasks are already sitting in Celery
                # regardless of what this transaction does, so losing their
                # task_id/'processing' bookkeeping to a rollback here would
                # leave real in-flight tasks looking like untouched
                # 'pending' rows instead.
                conn.commit()
                raise HTTPException(
                    status_code=503,
                    detail=f"Queued {queued} game(s) before losing the task queue "
                    f"(is Redis/the Celery worker running?): {e}",
                )
            conn.execute(
                "UPDATE games SET analysis_status = 'processing', analysis_task_id = ?, analysis_error = NULL "
                "WHERE id = ?",
                (async_result.id, game_id),
            )
            queued += 1
        conn.commit()
    finally:
        conn.close()

    return {"queued": queued}


@app.get("/api/analyze/status")
def api_analyze_status(user: dict = Depends(auth.get_current_user)):
    conn = get_connection()
    try:
        counts = conn.execute(
            "SELECT COALESCE(analysis_status, 'pending') as status, COUNT(*) as n "
            "FROM games WHERE user_id = ? GROUP BY status",
            (user["id"],),
        ).fetchall()
        in_flight = conn.execute(
            "SELECT id, analysis_status, analysis_task_id, analysis_error FROM games "
            "WHERE user_id = ? AND analysis_status IN ('processing', 'failed') "
            "ORDER BY id DESC LIMIT 50",
            (user["id"],),
        ).fetchall()
    finally:
        conn.close()

    return {
        "counts": {row["status"]: row["n"] for row in counts},
        "in_flight": [dict(r) for r in in_flight],
    }


@app.get("/api/srs2/due")
def api_srs2_due(user: dict = Depends(auth.get_current_user), limit: int = 10):
    return {"puzzles": srs_sm2.get_due_puzzles(user["id"], limit=limit)}


@app.get("/api/srs2/progress")
def api_srs2_progress(user: dict = Depends(auth.get_current_user)):
    return srs_sm2.get_progress_summary(user["id"])


class PuzzleAttemptSM2(BaseModel):
    quality: int  # 0-5 SuperMemo-2 self-assessed recall grade — see srs_sm2.py


@app.post("/api/srs2/puzzles/{puzzle_id}/attempt")
def api_srs2_attempt(puzzle_id: int, attempt: PuzzleAttemptSM2, user: dict = Depends(auth.require_puzzle_owner)):
    try:
        return srs_sm2.record_puzzle_attempt(user["id"], puzzle_id, attempt.quality)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)

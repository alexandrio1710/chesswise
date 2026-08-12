"""
Stage 7 — FastAPI dashboard server.

One route serves the dashboard page; a few small JSON endpoints feed it,
each accepting an optional `source` filter ('lichess' | 'chesscom' | omitted
for both) so the frontend's All/Lichess/Chess.com toggle can re-query
without the backend knowing anything about how it's rendered.
"""

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

import alerts
import cli_state
import clock_analysis
import insights
import manual_analysis
import opening_explorer
import opening_puzzles
import profiles
import progress
import puzzles
import srs
import stats
import tablebase
from db import get_connection

logger = logging.getLogger(__name__)

app = FastAPI(title="Chess Mistake Tracker")

STATIC_DIR = Path(__file__).parent / "static"

PHASES = ("opening", "middlegame", "endgame")

# Web-triggered refresh runs in a plain background thread (not a
# multiprocessing pool — spawning one from a request handler that isn't
# behind a `__main__` guard is asking for trouble on Windows), so the
# whole app stays single-user/local-only in spirit. State lives in memory
# since it only needs to survive one server process's lifetime.
_refresh_status = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "error": None,
    "result": None,
}


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


@app.get("/api/settings")
def api_get_settings():
    """Currently remembered usernames — same file the CLI's `refresh`/
    `digest` commands read from and write to (cli_state.py), so setting a
    username here also makes `python cli.py refresh` work without
    retyping it, and vice versa.
    """
    return cli_state.load_state()


@app.post("/api/settings")
def api_save_settings(settings: SettingsUpdate):
    cli_state.save_state(lichess_user=settings.lichess_user, chesscom_user=settings.chesscom_user)
    return cli_state.load_state()


def _run_refresh(lichess_user: str | None, chesscom_user: str | None) -> None:
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
        _refresh_status["result"] = result
        _refresh_status["error"] = None
    except Exception as e:
        logger.exception("Web-triggered refresh failed")
        _refresh_status["error"] = str(e)
    finally:
        _refresh_status["running"] = False
        _refresh_status["finished_at"] = datetime.now(timezone.utc).isoformat()


@app.post("/api/refresh")
def api_trigger_refresh(settings: SettingsUpdate = SettingsUpdate()):
    """Kick off fetch (incremental) + analyze in the background and
    return immediately — poll /api/refresh/status for progress. Any
    username given here is saved for next time, same as the CLI.
    """
    if _refresh_status["running"]:
        raise HTTPException(status_code=409, detail="A refresh is already running.")

    state = cli_state.load_state()
    lichess_user = settings.lichess_user or state.get("lichess_user")
    chesscom_user = settings.chesscom_user or state.get("chesscom_user")
    if not lichess_user and not chesscom_user:
        raise HTTPException(
            status_code=400,
            detail="No usernames configured. Set at least one in Settings first.",
        )

    if settings.lichess_user or settings.chesscom_user:
        cli_state.save_state(lichess_user=settings.lichess_user, chesscom_user=settings.chesscom_user)

    _refresh_status.update({
        "running": True, "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None, "error": None, "result": None,
    })
    threading.Thread(target=_run_refresh, args=(lichess_user, chesscom_user), daemon=True).start()
    return {"status": "started"}


@app.get("/api/refresh/status")
def api_refresh_status():
    return _refresh_status


@app.get("/api/summary")
def api_summary(source: str | None = Query(default=None), profile_id: int | None = Query(default=None)):
    source = _normalize_source(source)
    return {"takeaway": stats.top_takeaway(source, profile_id), **stats.overall_summary(source, profile_id)}


@app.get("/api/mistakes-by-phase")
def api_mistakes_by_phase(source: str | None = Query(default=None), profile_id: int | None = Query(default=None)):
    source = _normalize_source(source)
    counts = stats.mistakes_by_phase(source, profile_id)
    return {phase: counts.get(phase, 0) for phase in PHASES}


@app.get("/api/trend")
def api_trend(source: str | None = Query(default=None), profile_id: int | None = Query(default=None), n_months: int = 6):
    source = _normalize_source(source)
    return {
        "takeaway": stats.trend_takeaway(source, profile_id),
        "months": stats.monthly_trend(source, profile_id, n_months=n_months),
    }


@app.get("/api/openings")
def api_openings(source: str | None = Query(default=None), profile_id: int | None = Query(default=None)):
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
def api_insights(source: str | None = Query(default=None), profile_id: int | None = Query(default=None)):
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
    profile_id: int | None = Query(default=None),
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
def api_worst_games(source: str | None = Query(default=None), profile_id: int | None = Query(default=None), limit: int = 5):
    source = _normalize_source(source)
    return stats.worst_games(source, profile_id, limit=limit)


@app.get("/api/games/{game_id}")
def api_game_detail(game_id: int):
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


class AnalyzeFenRequest(BaseModel):
    fen: str


@app.post("/api/analyze/fen")
def api_analyze_fen(req: AnalyzeFenRequest):
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
def api_analyze_pgn(req: AnalyzePgnRequest):
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
def api_mistake_tablebase(mistake_id: int):
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
def api_add_note(game_id: int, note: NoteCreate):
    if stats.get_game_detail(game_id) is None:
        raise HTTPException(status_code=404, detail="Game not found")
    if not note.text.strip():
        raise HTTPException(status_code=400, detail="Note text can't be empty")
    return stats.add_note(game_id, note.text.strip(), note.ply)


@app.delete("/api/notes/{note_id}")
def api_delete_note(note_id: int):
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
def api_get_puzzle(puzzle_id: int):
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
def api_puzzle_attempt(puzzle_id: int, attempt: PuzzleAttempt):
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
    profile_id: int | None = Query(default=None),
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
def api_clock_analysis(source: str | None = Query(default=None), profile_id: int | None = Query(default=None)):
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
def api_list_profiles():
    return profiles.list_profiles()


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
def api_delete_profile(profile_id: int):
    if profiles.get_profile(profile_id) is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    profiles.delete_profile(profile_id)
    return {"status": "deleted"}


@app.post("/api/profiles/{profile_id}/links")
def api_link_username(profile_id: int, body: UsernameLink):
    if profiles.get_profile(profile_id) is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    if body.source not in ("lichess", "chesscom"):
        raise HTTPException(status_code=400, detail="source must be 'lichess' or 'chesscom'")
    username = body.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username can't be empty")
    return profiles.link_username(profile_id, body.source, username)


@app.delete("/api/profiles/{profile_id}/links")
def api_unlink_username(profile_id: int, source: str = Query(...), username: str = Query(...)):
    profiles.unlink_username(profile_id, source, username)
    return {"status": "unlinked"}


@app.get("/api/profiles/compare")
def api_compare_profiles(a: int = Query(...), b: int = Query(...)):
    try:
        return profiles.compare_profiles(a, b)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/profiles", response_class=HTMLResponse)
def profiles_page():
    return (STATIC_DIR / "profiles.html").read_text(encoding="utf-8")


# --- Progress, goals, auto-reports (Section 10) -----------------------------

@app.get("/api/progress")
def api_progress(source: str | None = Query(default=None), profile_id: int | None = Query(default=None)):
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
def api_create_goal(body: GoalCreate):
    if not body.description.strip():
        raise HTTPException(status_code=400, detail="Description can't be empty")
    try:
        return progress.create_goal(
            body.description.strip(), body.metric, body.comparison, body.target_value,
            phase=body.phase, source=_normalize_source(body.source), profile_id=body.profile_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/goals/{goal_id}")
def api_delete_goal(goal_id: int):
    progress.delete_goal(goal_id)
    return {"status": "deleted"}


@app.get("/progress", response_class=HTMLResponse)
def progress_page():
    return (STATIC_DIR / "progress.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)

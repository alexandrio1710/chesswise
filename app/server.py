"""
Stage 7 — FastAPI dashboard server.

One route serves the dashboard page; a few small JSON endpoints feed it,
each accepting an optional `source` filter ('lichess' | 'chesscom' | omitted
for both) so the frontend's All/Lichess/Chess.com toggle can re-query
without the backend knowing anything about how it's rendered.
"""

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

import puzzles
import stats
from db import get_connection

app = FastAPI(title="Chess Mistake Tracker")

STATIC_DIR = Path(__file__).parent / "static"

PHASES = ("opening", "middlegame", "endgame")


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


@app.get("/api/summary")
def api_summary(source: str | None = Query(default=None)):
    source = _normalize_source(source)
    return {"takeaway": stats.top_takeaway(source), **stats.overall_summary(source)}


@app.get("/api/mistakes-by-phase")
def api_mistakes_by_phase(source: str | None = Query(default=None)):
    source = _normalize_source(source)
    counts = stats.mistakes_by_phase(source)
    return {phase: counts.get(phase, 0) for phase in PHASES}


@app.get("/api/trend")
def api_trend(source: str | None = Query(default=None), n_months: int = 6):
    source = _normalize_source(source)
    return {
        "takeaway": stats.trend_takeaway(source),
        "months": stats.monthly_trend(source, n_months=n_months),
    }


@app.get("/api/openings")
def api_openings(source: str | None = Query(default=None)):
    source = _normalize_source(source)
    return {
        "most_played": stats.most_played_openings(source, limit=8),
        "best_win_rate": stats.best_win_rate_openings(source, limit=8),
        "to_review": stats.openings_to_review(source, limit=8),
        "all_families": sorted(
            stats.opening_family_stats(source), key=lambda f: f["games_played"], reverse=True
        ),
    }


@app.get("/api/worst-games")
def api_worst_games(source: str | None = Query(default=None), limit: int = 5):
    source = _normalize_source(source)
    return stats.worst_games(source, limit=limit)


@app.get("/api/games/{game_id}")
def api_game_detail(game_id: int):
    game = stats.get_game_detail(game_id)
    if game is None:
        raise HTTPException(status_code=404, detail="Game not found")
    if not game["analyzed"]:
        raise HTTPException(status_code=409, detail="This game hasn't been analyzed yet")
    if game["skip_reason"]:
        raise HTTPException(status_code=422, detail=f"Not analyzable: {game['skip_reason']}")

    return {
        "game": {
            "id": game["id"], "source": game["source"], "date": game["date"],
            "opponent": game["opponent"], "result": game["result"], "color": game["color"],
            "time_control": game["time_control"], "opening_name": game["opening_name"],
        },
        "moves": stats.get_game_moves(game_id),
    }


@app.get("/api/puzzles/queue")
def api_puzzle_queue(
    source: str | None = Query(default=None),
    mode: str = Query(default="all"),
    limit: int = 20,
):
    """mode='all' lists puzzles across every phase; mode='practice' narrows
    to whichever phase has the most mistakes/blunders for this filter, so
    practice sessions focus on the player's actual biggest leak.
    """
    source = _normalize_source(source)
    phase = None
    if mode == "practice":
        phase = stats.worst_mistake_phase(source)
    return {
        "phase": phase,
        "puzzles": puzzles.get_puzzle_queue(source, phase=phase, limit=limit),
    }


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


@app.post("/api/puzzles/{puzzle_id}/attempt")
def api_puzzle_attempt(puzzle_id: int, attempt: PuzzleAttempt):
    puzzle = puzzles.get_puzzle(puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")

    try:
        return puzzles.check_attempt(puzzle, attempt.from_square, attempt.to_square)
    except puzzles.IllegalMoveError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/puzzles", response_class=HTMLResponse)
def puzzles_page():
    return (STATIC_DIR / "puzzles.html").read_text(encoding="utf-8")


@app.get("/game", response_class=HTMLResponse)
def game_page():
    return (STATIC_DIR / "game.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)

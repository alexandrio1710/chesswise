"""
Opening-based puzzles — a user-requested addition alongside the original
advanced-features list: rather than Puzzle Rush only ever drawing from
the player's own flagged mistakes, this sources puzzles for openings the
player actually plays from Lichess's free public puzzle API, so there's
real tactical practice even in openings that haven't produced a flagged
mistake yet.

Free/public data only: Lichess's puzzle collection is explicitly public
domain (https://lichess.org/api#tag/Puzzles), and the batch-by-angle
endpoint used here is the same mechanism Lichess's own
lichess.org/training/<Opening_Name> pages use — verified directly against
the live API before building this (GET
https://lichess.org/api/puzzle/batch/Italian_Game?nb=3 returns real
puzzles). The docs explicitly discourage using these endpoints to
enumerate/mass-download puzzles (pointing at the bulk CSV export
instead) — this only ever fetches a handful of puzzles at a time for
openings the player has actually played, not a bulk crawl.

Kept as its own table rather than folded into the existing `puzzles`
table (which Sections 1/4/6 all key off, with NOT NULL game_id/mistake_id
tying every row to one of the player's own analyzed games) — that would
need a disruptive nullable-column rebuild of a table three other
features already depend on, for puzzles that don't actually have an
owning game/mistake in this database. Attempts are tracked with simple
counters here, not the Leitner spaced-repetition schedule (Section 4) —
that schedule is specifically about re-surfacing YOUR OWN mistakes on a
spaced schedule, which doesn't fit a puzzle freshly pulled from Lichess
the same way.

Lichess puzzles are genuinely multi-move forced sequences (solve a move,
the opponent's only reply auto-plays, solve the next move, ...) rather
than the single-best-move puzzles the rest of this app generates — grading
replays the position with python-chess and advances it server-side one
pair of plies at a time, same "server does chess logic, client only
renders FEN and handles clicks" split as the rest of the app.
"""

import io
import json
import logging
from datetime import datetime, timezone

import chess
import chess.pgn
import requests

from db import get_connection
from puzzles import legal_moves_for_fen

logger = logging.getLogger(__name__)

LICHESS_PUZZLE_BATCH_URL = "https://lichess.org/api/puzzle/batch/{angle}"
MIN_CACHED_PER_OPENING = 6
FETCH_BATCH_SIZE = 10

# Some opening family names (especially Chess.com's longer/less standard
# ones — see opening_family()'s own docstring in stats.py) don't match
# any real Lichess opening slug and will always come back empty. Without
# this, ensure_cached() would re-hit the live API for the same dead
# opening on every single page load. Process-lifetime only, same style
# as opening_explorer.py's _community_cache / tablebase.py's cache.
_known_empty: set[str] = set()


def slugify_opening(family_name: str) -> str:
    """"Caro-Kann Defense" -> "Caro-Kann_Defense", "Queen's Gambit" ->
    "Queens_Gambit" — matches the slug format Lichess's own
    /training/<Opening_Name> pages and puzzle `angle` param use (verified
    live against /api/puzzle/batch/Italian_Game).
    """
    return family_name.replace("'", "").strip().replace(" ", "_")


def _fen_and_side_after_pgn(pgn_text: str) -> tuple[str, str]:
    """(fen, side_to_move) after replaying every move in `pgn_text`.

    Lichess's puzzle payload gives the whole source game's PGN alongside
    an `initialPly` field — but verified empirically against 5 live
    puzzles (replaying each candidate ply count and checking whether the
    ENTIRE solution sequence stays legal move-by-move, not just the
    first move, which gave a false positive during initial testing): the
    returned `game.pgn` is already truncated to end exactly at the
    puzzle's starting position. `initialPly` is consistently one less
    than the PGN's actual move count and isn't needed here — replaying
    every move already-given is both simpler and correct, rather than
    trying to reverse-engineer initialPly's exact indexing convention.
    """
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    board = game.board()
    node = game
    while node.variations:
        node = node.variations[0]
        board.push(node.move)
    return board.fen(), ("white" if board.turn == chess.WHITE else "black")


def fetch_puzzles_for_opening(opening_family: str, nb: int = FETCH_BATCH_SIZE) -> list[dict]:
    """Up to `nb` puzzles from games that started with this opening,
    straight from Lichess's public puzzle API. Returns [] (not an error)
    if Lichess has no puzzles tagged for this exact opening name — common
    for rarer openings, not a bug to raise on.
    """
    angle = slugify_opening(opening_family)
    url = LICHESS_PUZZLE_BATCH_URL.format(angle=angle)
    try:
        resp = requests.get(url, params={"nb": nb}, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"Lichess puzzle batch fetch failed for '{opening_family}' ({angle}): {e}")
        return []

    results = []
    for entry in resp.json().get("puzzles", []):
        puzzle, game = entry.get("puzzle"), entry.get("game")
        if not puzzle or not game:
            continue
        try:
            fen, side_to_move = _fen_and_side_after_pgn(game["pgn"])
        except Exception:
            continue
        results.append({
            "external_id": puzzle["id"],
            "opening_family": opening_family,
            "fen": fen,
            "side_to_move": side_to_move,
            "solution_uci": puzzle["solution"],
            "lichess_rating": puzzle.get("rating"),
            "themes": puzzle.get("themes", []),
            "game_url": f"https://lichess.org/{game['id']}",
        })
    return results


def ensure_cached(opening_family: str, min_count: int = MIN_CACHED_PER_OPENING) -> None:
    """Tops up the local cache for this opening if it's running low, so
    repeat visits don't re-hit Lichess every time."""
    if opening_family in _known_empty:
        return
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT COUNT(*) as n FROM opening_puzzles WHERE opening_family = ?", (opening_family,)
        ).fetchone()["n"]
        if existing >= min_count:
            return
        fetched = fetch_puzzles_for_opening(opening_family)
        if not fetched and existing == 0:
            _known_empty.add(opening_family)
            return
        now = datetime.now(timezone.utc).isoformat()
        for p in fetched:
            conn.execute(
                """
                INSERT OR IGNORE INTO opening_puzzles
                    (external_id, opening_family, fen, side_to_move, solution_uci,
                     lichess_rating, themes, game_url, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (p["external_id"], p["opening_family"], p["fen"], p["side_to_move"],
                 json.dumps(p["solution_uci"]), p["lichess_rating"], json.dumps(p["themes"]),
                 p["game_url"], now),
            )
        conn.commit()
    finally:
        conn.close()


def get_puzzles_for_openings(opening_families: list[str], limit: int = 12) -> list[dict]:
    """Cached puzzle summaries across the given openings (auto-topping-up
    the cache first), for building a practice queue.
    """
    for family in opening_families:
        ensure_cached(family)

    if not opening_families:
        return []
    conn = get_connection()
    try:
        placeholders = ",".join("?" * len(opening_families))
        rows = conn.execute(
            f"""
            SELECT id, opening_family, lichess_rating, themes, game_url, attempts, correct
            FROM opening_puzzles WHERE opening_family IN ({placeholders})
            ORDER BY RANDOM() LIMIT ?
            """,
            opening_families + [limit],
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["themes"] = json.loads(d["themes"]) if d["themes"] else []
            results.append(d)
        return results
    finally:
        conn.close()


def get_puzzle(puzzle_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM opening_puzzles WHERE id = ?", (puzzle_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["solution_uci"] = json.loads(d["solution_uci"])
        d["themes"] = json.loads(d["themes"]) if d["themes"] else []
        return d
    finally:
        conn.close()


def record_attempt(puzzle_id: int, correct: bool) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE opening_puzzles SET attempts = attempts + 1, correct = correct + ? WHERE id = ?",
            (1 if correct else 0, puzzle_id),
        )
        conn.commit()
    finally:
        conn.close()


def _board_after(fen: str, solution_uci: list[str], through_index: int) -> chess.Board:
    """Board state after replaying solution_uci[:through_index] moves from
    the puzzle's starting FEN — used to validate/apply each new attempt
    against the actual current position rather than trusting client state.
    """
    board = chess.Board(fen)
    for uci in solution_uci[:through_index]:
        board.push(chess.Move.from_uci(uci))
    return board


def attempt_move(puzzle_id: int, move_index: int, from_square: str, to_square: str) -> dict:
    """Grades one of the solver's moves against solution_uci[move_index]
    (always an even index — odd indices are the opponent's forced replies,
    which this plays automatically rather than asking the solver for them).

    Returns one of:
      - {"correct": False, "done": True, "solution_san": [...]} — wrong
        move; the puzzle ends here and the full solution is revealed.
      - {"correct": True, "done": False, "fen": ..., "legal_moves": [...],
         "next_move_index": ...} — right so far, opponent's reply auto-
        played, more of the sequence remains.
      - {"correct": True, "done": True} — right, and that was the last
        move in the sequence.
    """
    puzzle = get_puzzle(puzzle_id)
    if puzzle is None:
        raise ValueError(f"Opening puzzle {puzzle_id} not found")

    solution = puzzle["solution_uci"]
    if not 0 <= move_index < len(solution):
        # Client-supplied, unvalidated — solution[move_index] below would
        # otherwise raise an uncaught IndexError (a raw 500) instead of the
        # clean 404 the route's own error handling implies for a bad
        # puzzle_id (server.py only catches ValueError here).
        raise ValueError(f"move_index {move_index} is out of range for this puzzle's solution")

    board = _board_after(puzzle["fen"], solution, move_index)

    try:
        move = chess.Move.from_uci(f"{from_square}{to_square}")
    except ValueError:
        move = None
    if move is not None and move not in board.legal_moves:
        queen_move = chess.Move.from_uci(f"{from_square}{to_square}q")
        move = queen_move if queen_move in board.legal_moves else None

    expected = chess.Move.from_uci(solution[move_index])
    # Exact match only — NOT just (from_square, to_square) — so a queen-
    # auto-promotion attempt is correctly graded wrong (not silently
    # accepted then secretly replaced with a different move) when the
    # puzzle's actual solution needs a different promotion piece. This
    # already handles the common "solution IS a queen promotion" case
    # correctly, since `move` gets auto-filled to the queen-promotion UCI
    # above when the bare move isn't legal.
    is_correct = move is not None and move == expected

    if not is_correct:
        solved_board = _board_after(puzzle["fen"], solution, 0)
        solution_san = []
        for uci in solution:
            mv = chess.Move.from_uci(uci)
            solution_san.append(solved_board.san(mv))
            solved_board.push(mv)
        record_attempt(puzzle_id, correct=False)
        return {"correct": False, "done": True, "solution_san": solution_san}

    board.push(expected)
    next_index = move_index + 1

    if next_index >= len(solution):
        record_attempt(puzzle_id, correct=True)
        return {"correct": True, "done": True}

    # Opponent's forced reply auto-plays; the solver never enters it.
    board.push(chess.Move.from_uci(solution[next_index]))
    next_index += 1

    if next_index >= len(solution):
        record_attempt(puzzle_id, correct=True)
        return {"correct": True, "done": True}

    return {
        "correct": True, "done": False,
        "fen": board.fen(), "side_to_move": "white" if board.turn == chess.WHITE else "black",
        "legal_moves": legal_moves_for_fen(board.fen()),
        "next_move_index": next_index,
    }

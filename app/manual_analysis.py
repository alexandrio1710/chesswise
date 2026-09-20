"""
Advanced features, Section 5 — Free Analysis Board.

Analyze any pasted PGN or FEN with the exact same Stockfish +
classification pipeline as the rest of the app (analysis.py,
mistakes.py's tier/phase grading, stats.py's accuracy formula) — not a
separate, simplified analyzer. Two paths:

  - Save it: inserted into `games` as source='manual' (a synthetic id
    since a pasted game has no natural external one) and run through
    mistakes.analyze_and_store_game() unchanged, so it shows up in every
    existing stat/feature exactly like a synced game — reuses the whole
    pipeline rather than a parallel one.
  - One-off: analyzed without touching the database at all, for a quick
    look at an over-the-board game or a position you're curious about.
"""

import hashlib
import io
from datetime import datetime, timezone

import chess
import chess.pgn

from analysis import analyze_game_moves
from config import STOCKFISH_DEPTH
from db import find_game_id, save_games
from fetchers import _parse_pgn_tags, _split_pgn_blobs, normalize_pgn_game
from mistakes import STANDARD_VARIANT_TAGS, classify_tier, get_pgn_variant
from puzzles import get_top_lines, legal_moves_for_fen
from stats import annotate_fen, capped_eval_drop, compute_game_accuracy, get_starting_fen

# This endpoint takes arbitrary pasted text with no login required (Advanced
# features, Section 5 keeps the Analyze board zero-login, same as the rest
# of the local app) and runs a full Stockfish pass per ply — real CPU cost,
# not bounded by anything else in the request. 400 plies is ~200 full
# moves, generous headroom over any real game (even long GM games rarely
# clear 150), so this only ever rejects input that isn't a single
# well-formed game — while still capping the worst-case cost of a request
# to a fixed number of engine calls.
MAX_ANALYSIS_PLIES = 400


def _count_plies(pgn_text: str) -> int:
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return 0
    n = 0
    node = game
    while node.variations:
        node = node.variations[0]
        n += 1
    return n


def looks_like_fen(text: str) -> bool:
    """A FEN is one line with exactly 6 space-separated fields and no PGN
    move numbers; a PGN has move text (numbers followed by '.') regardless
    of whether headers are present. Best-effort, not a full parser.
    """
    text = text.strip()
    if "\n\n" in text or "[" in text:
        return False
    fields = text.split()
    return len(fields) == 6 and "/" in fields[0]


def analyze_fen(fen: str, depth: int = STOCKFISH_DEPTH) -> dict:
    """One-shot analysis of a bare position: evaluation + top engine
    lines, from the perspective of whoever is about to move there. Each
    line carries the resulting position's FEN too, so the frontend's
    board can preview what the position looks like after that suggested
    move — computed here (server does chess logic, client only ever
    renders a FEN it's given, same split as every other board in this
    app) rather than needing a second round-trip per line clicked.
    """
    try:
        board = chess.Board(fen)
    except ValueError as e:
        raise ValueError(f"Not a valid FEN: {e}")
    lines = get_top_lines(fen, depth=depth, num_lines=3, with_pv=True)
    for line in lines:
        move = chess.Move.from_uci(line["move_uci"])
        board.push(move)
        line["fen_after"] = board.fen()
        board.pop()
    return {"fen": fen, "top_lines": lines, "legal_moves": legal_moves_for_fen(fen)}


def apply_move(fen: str, from_square: str, to_square: str, promotion: str | None = None) -> dict:
    """Validate and apply one move to `fen`, server-side — same "server
    does chess logic, client only renders a FEN it's given" split as
    analyze_fen's fen_after above. Powers the Analyze board's click/drag-
    to-move interactivity: the client gets legal destinations from
    puzzles.legal_moves_for_fen, then calls this once one is picked, then
    re-calls analyze_fen with the returned fen for live engine feedback.
    """
    try:
        board = chess.Board(fen)
    except ValueError as e:
        raise ValueError(f"Not a valid FEN: {e}")

    uci = f"{from_square}{to_square}{promotion or ''}"
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        raise ValueError(f"'{uci}' isn't a valid move")

    if move not in board.legal_moves and promotion is None:
        # A promoting pawn move needs a piece letter to be a legal UCI
        # move at all; default to queen (same "assume queen in the UI"
        # simplification puzzles.legal_moves_for_fen's dedup already
        # makes) rather than rejecting an otherwise-legal move just
        # because the client didn't ask about underpromotion.
        move = chess.Move.from_uci(uci + "q")

    if move not in board.legal_moves:
        raise ValueError(f"'{from_square}{to_square}' isn't legal in this position")

    san = board.san(move)
    board.push(move)
    return {
        "fen": board.fen(),
        "san": san,
        "uci": move.uci(),
        "is_check": board.is_check(),
        "is_checkmate": board.is_checkmate(),
        "is_game_over": board.is_game_over(),
    }


def _graded_moves(pgn_text: str, depth: int) -> list[dict]:
    variant = get_pgn_variant(pgn_text)
    if variant not in STANDARD_VARIANT_TAGS:
        raise ValueError(f"Unsupported variant: {variant} — Stockfish can only analyze standard chess.")

    ply_count = _count_plies(pgn_text)
    if ply_count > MAX_ANALYSIS_PLIES:
        raise ValueError(
            f"This game has {ply_count} half-moves, over the {MAX_ANALYSIS_PLIES}-ply limit for "
            "on-demand analysis — check the PGN is a single well-formed game."
        )

    moves_raw = analyze_game_moves(pgn_text, depth=depth)
    if not moves_raw:
        raise ValueError("No moves found — check the PGN is well-formed.")

    moves = []
    for m in moves_raw:
        is_white = m["color_moved"] == "white"
        eval_before = m["eval_before_cp"] if is_white else -m["eval_before_cp"]
        eval_after = m["eval_after_cp"] if is_white else -m["eval_after_cp"]
        # Magnitude-capped (see stats.ACCURACY_EVAL_CAP_CP) — this one-off
        # path had its own separate, uncapped `eval_before - eval_after`
        # here, missing the same missed-mate/blunder-misclassification fix
        # already applied to the saved-game path in mistakes.py.
        eval_drop = capped_eval_drop(eval_before, eval_after)
        moves.append({
            "ply": m["ply"], "move_number": m["move_number"], "color_moved": m["color_moved"],
            "move_san": m["move_san"], "eval_cp": m["eval_cp"], "eval_before_cp": eval_before,
            "eval_drop": eval_drop, "tier": classify_tier(eval_before, eval_after),
            "phase": m["phase"],
            "clock_seconds_remaining": m["clock_seconds_remaining"],
        })
    return moves


def analyze_pgn_oneoff(pgn_text: str, depth: int = STOCKFISH_DEPTH) -> dict:
    """Full move-by-move analysis of a pasted PGN without touching the
    database. Accuracy is reported for both colors — there's no inherent
    "my color" for an arbitrary pasted game the way there is for a synced
    one, so both are shown rather than guessing.

    Each move carries its resulting position's FEN (annotate_fen), and
    the response includes the starting one too, so the frontend can show
    a real interactive board for a one-off analysis exactly like it does
    for a saved game (stats.annotate_fen/get_starting_fen are the same
    functions /api/games/{id} uses) — no separate chess logic needed
    client-side, no second round-trip.
    """
    pgn_text, _ = first_game_of(pgn_text)
    moves = _graded_moves(pgn_text, depth)
    moves = annotate_fen(moves, pgn_text)

    game = chess.pgn.read_game(io.StringIO(pgn_text))
    headers = dict(game.headers) if game else {}

    return {
        "moves": moves,
        "start_fen": get_starting_fen(pgn_text),
        "accuracy_white": compute_game_accuracy(moves, "white"),
        "accuracy_black": compute_game_accuracy(moves, "black"),
        "headers": {
            "white": headers.get("White"), "black": headers.get("Black"),
            "result": headers.get("Result"), "date": headers.get("Date"),
            "event": headers.get("Event"),
        },
    }


def _parse_pgn_date(date_str: str) -> str:
    """PGN dates are 'YYYY.MM.DD', sometimes with '??' for unknown parts.
    Falls back to today (UTC) if unparseable — a manually-pasted game
    needs *some* date to sort/filter alongside synced ones.
    """
    try:
        parts = date_str.split(".")
        if len(parts) == 3 and "?" not in date_str:
            return datetime(int(parts[0]), int(parts[1]), int(parts[2]), tzinfo=timezone.utc).isoformat()
    except (ValueError, IndexError):
        pass
    return datetime.now(timezone.utc).isoformat()


_BARE_HEADERS = '[Event "?"]\n[White "?"]\n[Black "?"]\n[Result "*"]\n\n'


def first_game_of(pgn_text: str) -> tuple[str, int]:
    """The first game in pasted text, and how many games the paste held.

    A paste can carry stray leading whitespace, Windows line endings, several
    games back to back, or just bare movetext with no tags; python-chess only
    reads one game and chokes on indented tags, so this cleans that up once
    for every caller. Bare movetext gets a minimal tag block so the stored PGN
    is a normal one.
    """
    blobs = _split_pgn_blobs(pgn_text)
    first = blobs[0] if blobs else pgn_text.strip()
    if not _parse_pgn_tags(first):
        first = _BARE_HEADERS + first
    return first, max(1, len(blobs))


def save_manual_game(
    pgn_text: str, player_color: str, opponent_override: str | None = None, profile_id: int | None = None,
) -> int:
    """Insert a pasted PGN as a game and run it through the exact same
    analysis pipeline as a synced one. Returns the game_id.

    A PGN that came from Lichess or Chess.com keeps its real source and id
    (fetchers.normalize_pgn_game), so pasting a game you already synced —
    or that a later sync brings in — is recognized as the same game and
    returns the existing row rather than storing (and counting) it twice.
    Anything else (an over-the-board game, another site) is saved as
    source='manual' with a per-paste id, so pasting it twice makes two rows.
    Only the first game of a multi-game paste is saved; the API reports how
    many were left out.
    """
    pgn_text, _ = first_game_of(pgn_text)
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None or not game.headers:
        raise ValueError("Couldn't parse this PGN — check it's well-formed.")

    normalized = normalize_pgn_game(pgn_text, player_color, opponent_override, default_source="manual")
    if normalized is None:
        raise ValueError("Couldn't parse this PGN — check it's well-formed.")

    # Unlike the bulk fetch (where an unrecognized Result tag means "skip this
    # game entirely" — see fetchers.py), this is a single, deliberate "save
    # this exact game" action: dropping it over a missing/unclear Result tag
    # (common for a manually-typed-out OTB game) would be worse than a
    # best-effort default, since the moves/analysis are still perfectly valid.
    normalized["result"] = normalized["result"] or "draw"
    normalized["date"] = normalized["date"] or _parse_pgn_date(game.headers.get("Date", ""))
    if normalized["source"] == "manual":
        # No natural external id — hash the content plus a timestamp so
        # re-pasting the same PGN twice creates two rows (each paste is a
        # deliberate save action).
        normalized["source_game_id"] = hashlib.sha256(f"{pgn_text}{datetime.now().isoformat()}".encode()).hexdigest()[:16]

    existing_id = find_game_id(normalized)
    if existing_id is not None:
        return existing_id

    # The caller passes the profile currently selected in the Analyze
    # board's own switcher; a caller with no switcher of its own (the CLI,
    # or an older client) falls back to whichever profile was created
    # first, same as before that switcher existed.
    if profile_id is None:
        from profiles import default_profile_id

        profile_id = default_profile_id()

    save_games([normalized], profile_id=profile_id)

    game_id = find_game_id(normalized)
    if game_id is None:
        raise RuntimeError("Saved game not found immediately after insert — this shouldn't happen.")

    from mistakes import analyze_and_store_game
    analyze_and_store_game(game_id, normalized["pgn"])
    return game_id

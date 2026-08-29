"""
Stage 4 — Mistake detection.

Takes the per-move Stockfish evals from analysis.py, converts them to the
mover's own perspective (a "good" move should never look bad to the person
who played it), classifies each move's eval drop by severity and the game
phase it happened in, and stores flagged moves in the `mistakes` table.

All thresholds below are intentionally named constants — tune them here if
the classification feels too strict or too lenient.
"""

import re

from analysis import analyze_game_moves
from config import STOCKFISH_DEPTH
from db import get_connection
from stats import capped_eval_drop, win_percent

# Lichess/Chess.com PGNs tag non-standard rule sets with a Variant header
# (e.g. "Three-check", "Horde", "Atomic", "Crazyhouse"). Those use board
# setups or win conditions Stockfish (a standard-chess engine) can't
# evaluate meaningfully — python-chess even produces non-standard FEN for
# some of them (e.g. Horde's pawn-flooded start, Three-check's embedded
# check counter), which Stockfish rejects outright. We skip these rather
# than erroring, since no amount of retrying will make them analyzable.
STANDARD_VARIANT_TAGS = {None, "Standard", "From Position"}


def get_pgn_variant(pgn_text: str) -> str | None:
    match = re.search(r'\[Variant\s+"([^"]*)"\]', pgn_text)
    return match.group(1) if match else None

# --- Severity thresholds: Lichess's own move-judgment algorithm -------------
# https://github.com/lichess-org/lila/blob/master/modules/tree/src/main/Advice.scala
# Ported directly rather than tuning our own magnitude-capped centipawn
# thresholds further: a flat centipawn drop doesn't distinguish a real
# swing from one that doesn't change who's winning (the "moves that miss
# mate show up as a blunder" bug fixed earlier this session was a cruder,
# narrower version of exactly this problem) — Lichess's own judgment is
# win-percentage-based throughout, with a further special case for a move
# that specifically creates or loses a forced mate.
#
# Ordinary case: loss in win% (0-100 scale, stats.win_percent) between the
# mover's own eval right before and right after their move.
INACCURACY_WIN_PERCENT_LOSS = 5.0
MISTAKE_WIN_PERCENT_LOSS = 10.0
BLUNDER_WIN_PERCENT_LOSS = 15.0

# Special case: a move that creates a forced mate against the mover (from a
# position that wasn't already a mate either way), or loses a forced mate
# the mover already had. Severity depends on the mover's own eval right
# before (creating a mate against yourself matters less if you were already
# losing badly) or right after (losing your own mate matters less if you're
# still clearly winning anyway) — Lichess's own MateAdvice thresholds.
MATE_ADVICE_INACCURACY_CP = 999
MATE_ADVICE_MISTAKE_CP = 700

# analysis.py represents a forced mate as roughly +-MATE_SCORE_CP (10000);
# any eval this close to that is mate-encoded, not a real centipawn value —
# a genuine Stockfish cp eval never gets anywhere near this magnitude.
MATE_SCORE_DETECTION_THRESHOLD_CP = 9000

# --- Game phase thresholds -------------------------------------------------
OPENING_MOVE_CUTOFF = 10     # moves 1-10 => opening
ENDGAME_MOVE_CUTOFF = 30     # move 30+ => endgame, regardless of material
ENDGAME_PIECE_COUNT = 7      # fewer than 7 non-king pieces on board => endgame


def _is_mate_score(cp: float) -> bool:
    return abs(cp) >= MATE_SCORE_DETECTION_THRESHOLD_CP


def _mate_advice_severity(eval_before_cp: float, eval_after_cp: float) -> str | None:
    """The two forced-mate-specific cases Lichess's MateAdvice special-cases
    (see module comment above); None if this move doesn't match either
    (including "still a forced mate for the same side either way", which
    falls through to the ordinary win%-based case below — its win% is
    already saturated by the cap in stats.win_percent, so a mate found a
    few moves slower than the fastest one available correctly costs ~0).
    """
    before_mate, after_mate = _is_mate_score(eval_before_cp), _is_mate_score(eval_after_cp)

    if not before_mate and after_mate and eval_after_cp < 0:
        # Walked into a forced mate against yourself.
        if eval_before_cp < -MATE_ADVICE_INACCURACY_CP:
            return "inaccuracy"
        if eval_before_cp < -MATE_ADVICE_MISTAKE_CP:
            return "mistake"
        return "blunder"

    if before_mate and eval_before_cp > 0 and not (after_mate and eval_after_cp > 0):
        # Had a forced mate, and it's gone (resolved into a plain eval, or
        # flipped into a mate against you instead).
        after_cp_or_zero = 0.0 if after_mate else eval_after_cp
        if after_cp_or_zero > MATE_ADVICE_INACCURACY_CP:
            return "inaccuracy"
        if after_cp_or_zero > MATE_ADVICE_MISTAKE_CP:
            return "mistake"
        return "blunder"

    return None


def _severity_from_win_percent_loss(loss: float) -> str | None:
    if loss >= BLUNDER_WIN_PERCENT_LOSS:
        return "blunder"
    if loss >= MISTAKE_WIN_PERCENT_LOSS:
        return "mistake"
    if loss >= INACCURACY_WIN_PERCENT_LOSS:
        return "inaccuracy"
    return None


def classify_severity(eval_before_cp: float, eval_after_cp: float) -> str | None:
    """Grade a single move by how much win probability it cost the player
    who made it — both evals already converted to the mover's own
    perspective by the caller, so a smaller eval_after always means "this
    move made things worse for them" regardless of color. Returns None if
    the loss didn't clear even the inaccuracy bar — most moves in most
    games, since only real errors get flagged at all.
    """
    mate_severity = _mate_advice_severity(eval_before_cp, eval_after_cp)
    if mate_severity is not None:
        return mate_severity
    loss = win_percent(eval_before_cp) - win_percent(eval_after_cp)
    return _severity_from_win_percent_loss(loss)


# --- Move quality tiers (Full Game Review) ----------------------------------
# classify_severity() above only cares about moves bad enough to flag —
# most moves in most games never get graded at all. The Full Game Review
# wants every move graded, including the good ones, so this adds three
# tiers BELOW the existing inaccuracy floor. The inaccuracy/mistake/blunder
# boundaries themselves are untouched here — changing them would shift the
# meaning of every already-stored mistake, puzzle, and stat in the app.
# GOOD_WIN_PERCENT_LOSS is therefore pinned to INACCURACY_WIN_PERCENT_LOSS
# so the two scales meet exactly at the same edge rather than leaving an
# ungraded gap or double-counting a band. Lichess doesn't publish a
# best/excellent/good breakdown of its own (only Inaccuracy/Mistake/
# Blunder) — these three finer bands are this project's own judgment call,
# same relative proportions as before, just re-expressed on the win%-loss
# scale that now governs everything else here.
BEST_WIN_PERCENT_LOSS = 1.0
EXCELLENT_WIN_PERCENT_LOSS = 2.5
GOOD_WIN_PERCENT_LOSS = INACCURACY_WIN_PERCENT_LOSS


def classify_tier(eval_before_cp: float, eval_after_cp: float) -> str:
    """Six-tier quality grade for a single move, mover's-own-perspective
    eval before/after (same input convention as classify_severity()).
    Unlike classify_severity(), this always returns something — every move
    gets graded, not just flagged mistakes.
    """
    mate_severity = _mate_advice_severity(eval_before_cp, eval_after_cp)
    if mate_severity is not None:
        return mate_severity

    # A move that improved the position beyond what was already best
    # (the opponent's prior move was itself weak) clamps to 0 loss rather
    # than yielding some notion of "better than best".
    loss = max(0.0, win_percent(eval_before_cp) - win_percent(eval_after_cp))
    if loss >= BLUNDER_WIN_PERCENT_LOSS:
        return "blunder"
    if loss >= MISTAKE_WIN_PERCENT_LOSS:
        return "mistake"
    if loss >= INACCURACY_WIN_PERCENT_LOSS:
        return "inaccuracy"
    if loss >= EXCELLENT_WIN_PERCENT_LOSS:
        return "good"
    if loss >= BEST_WIN_PERCENT_LOSS:
        return "excellent"
    return "best"


def classify_phase(move_number: int, non_king_piece_count: int) -> str:
    """Bucket a move into opening/middlegame/endgame by a simple, cheap
    heuristic rather than real positional understanding (no engine can
    reliably say "this is strategically an endgame" without much deeper
    analysis than this project does per move).

    Opening is purely move-count based (moves 1-10) since that's genuinely
    how openings are understood — a fixed number of moves before both
    sides have mostly developed, regardless of the actual position.

    Endgame is the OR of two independent signals, either being enough:
    material has thinned out (fewer than ENDGAME_PIECE_COUNT non-king
    pieces — the classic "few pieces left" definition), OR the game has
    simply gone long (move 30+) even if material is still on the board,
    since long grinding games function like endgames strategically even
    when pieces remain. This means a move-30+ middlegame-material position
    still gets called "endgame" — a deliberate simplification, not a bug.

    Everything else falls through to middlegame.
    """
    if move_number <= OPENING_MOVE_CUTOFF:
        return "opening"
    if non_king_piece_count < ENDGAME_PIECE_COUNT or move_number >= ENDGAME_MOVE_CUTOFF:
        return "endgame"
    return "middlegame"


def _classify_move(m: dict) -> dict | None:
    """Grade one move (a dict from analyze_game_moves) and return its
    mistake record, or None if it wasn't inaccurate enough to flag.
    Factored out of detect_mistakes() so analyze_and_store_game() can
    derive flagged mistakes AND store the full move trace from a single
    analyze_game_moves() pass, instead of analyzing the same game twice.
    """
    is_white = m["color_moved"] == "white"
    eval_before = m["eval_before_cp"] if is_white else -m["eval_before_cp"]
    eval_after = m["eval_after_cp"] if is_white else -m["eval_after_cp"]

    severity = classify_severity(eval_before, eval_after)
    if severity is None:
        return None

    # eval_drop itself stays a magnitude-capped centipawn value (see
    # stats.ACCURACY_EVAL_CAP_CP) — severity/tier no longer derive from it
    # (see classify_severity/classify_tier's own win%-based logic above),
    # but compute_game_acpl and the estimated-rating pipeline still want a
    # plain, literal "centipawns lost" number.
    eval_drop = capped_eval_drop(eval_before, eval_after)

    phase = classify_phase(m["move_number"], m["non_king_piece_count"])

    return {
        "ply": m["ply"],
        "move_number": m["move_number"],
        "move_san": m["move_san"],
        "color_moved": m["color_moved"],
        "phase": phase,
        "severity": severity,
        "eval_before": eval_before,
        "eval_after": eval_after,
        "eval_drop": eval_drop,
        "clock_seconds_remaining": m["clock_seconds_remaining"],
    }


def detect_mistakes(pgn_text: str, depth: int = STOCKFISH_DEPTH) -> list[dict]:
    """Analyze a game and return the list of flagged mistakes (moves whose
    eval drop, from the mover's perspective, meets the inaccuracy threshold
    or worse).
    """
    moves = analyze_game_moves(pgn_text, depth=depth)
    return [flagged for m in moves if (flagged := _classify_move(m)) is not None]


def analyze_and_store_game(game_id: int, pgn_text: str, depth: int = STOCKFISH_DEPTH) -> list[dict] | None:
    """Run mistake detection for one game and persist results to the
    `mistakes` table. Marks the game as analyzed so Stage 5's batch runner
    can skip it on re-runs.

    Returns None (instead of a list) if the game uses a non-standard
    variant and was skipped rather than analyzed.
    """
    variant = get_pgn_variant(pgn_text)
    if variant not in STANDARD_VARIANT_TAGS:
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE games SET analyzed = 1, skip_reason = ?, analyzed_at = datetime('now') WHERE id = ?",
                (f"unsupported variant: {variant}", game_id),
            )
            conn.commit()
        finally:
            conn.close()
        return None

    moves = analyze_game_moves(pgn_text, depth=depth)
    flagged = [m for move in moves if (m := _classify_move(move)) is not None]

    conn = get_connection()
    try:
        # Re-analyzing a game whose mistakes already have generated
        # puzzles (Stage B) would otherwise violate puzzles.mistake_id's
        # foreign key when the old mistakes rows are deleted below. Their
        # puzzles would be stale anyway (still pointing at eval/position
        # data for a mistake that's about to be replaced), so drop them
        # too — puzzles.py will regenerate for whatever this re-analysis
        # finds instead, next time it's run.
        conn.execute(
            "DELETE FROM puzzles WHERE mistake_id IN (SELECT id FROM mistakes WHERE game_id = ?)",
            (game_id,),
        )
        conn.execute("DELETE FROM mistakes WHERE game_id = ?", (game_id,))
        conn.executemany(
            """
            INSERT INTO mistakes
                (game_id, ply, move_number, move_san, color_moved, phase, severity,
                 eval_before, eval_after, eval_drop, clock_seconds_remaining)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    game_id, m["ply"], m["move_number"], m["move_san"], m["color_moved"],
                    m["phase"], m["severity"], m["eval_before"], m["eval_after"],
                    m["eval_drop"], m["clock_seconds_remaining"],
                )
                for m in flagged
            ],
        )

        # Full per-move eval trace (game analysis view), from the same
        # analyze_game_moves() pass, so a full-game review doesn't cost a
        # second round of Stockfish work. Every move also gets a quality
        # tier (Full Game Review, Section 1) — not just flagged mistakes.
        conn.execute("DELETE FROM game_moves WHERE game_id = ?", (game_id,))
        game_moves_rows = []
        for move in moves:
            is_white = move["color_moved"] == "white"
            eval_before = move["eval_before_cp"] if is_white else -move["eval_before_cp"]
            eval_after = move["eval_after_cp"] if is_white else -move["eval_after_cp"]
            eval_drop = capped_eval_drop(eval_before, eval_after)
            game_moves_rows.append((
                game_id, move["ply"], move["move_number"], move["color_moved"],
                move["move_san"], move["eval_cp"], move["clock_seconds_remaining"],
                eval_before, eval_drop, classify_tier(eval_before, eval_after),
            ))
        conn.executemany(
            """
            INSERT INTO game_moves
                (game_id, ply, move_number, color_moved, move_san, eval_cp,
                 clock_seconds_remaining, eval_before_cp, eval_drop, tier)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            game_moves_rows,
        )

        conn.execute(
            "UPDATE games SET analyzed = 1, analyzed_at = datetime('now') WHERE id = ?",
            (game_id,),
        )
        conn.commit()
    finally:
        conn.close()

    return flagged


if __name__ == "__main__":
    from db import get_all_games

    games = get_all_games()
    if not games:
        print("No games in DB yet. Run db.py first (Stage 2).")
        raise SystemExit(1)

    test_game = games[0]
    print(f"Detecting mistakes in: {test_game['source']} | {test_game['date']} | "
          f"{test_game['color']} vs {test_game['opponent']}\n")

    flagged = analyze_and_store_game(test_game["id"], test_game["pgn"])

    if not flagged:
        print("No mistakes flagged (>= inaccuracy threshold) in this game.")
    for m in flagged:
        print(f"  move {m['move_number']:>3} ({m['color_moved']:<5}) {m['move_san']:<8} "
              f"{m['severity']:<11} drop={m['eval_drop']:>7.0f}cp "
              f"[{m['eval_before']:.0f} -> {m['eval_after']:.0f}] "
              f"phase={m['phase']:<11} clock={m['clock_seconds_remaining']}")

    print(f"\n{len(flagged)} mistakes flagged and stored for game_id={test_game['id']}")

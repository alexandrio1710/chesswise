"""
Coaching report — repertoire structure tagging.

This player's repertoire is narrow and specific (London as White; Dragon/
Hyperaccelerated Dragon as Black vs 1.e4; a King's Indian/Grünfeld fork as
Black vs 1.d4), with known, named structural failure patterns already
identified for each line. This module's only job is recognizing which of
those four structures a given game actually reached, from the PGN alone —
no Stockfish needed, since a structure is a fact about piece/pawn
placement, not about move quality.

None of this judges whether the player handled the structure well — that
is exactly what coaching_report.py's drift-candidate detection (real
engine analysis) is for. This only answers "which known trouble spot did
this game walk into," so the report can attach the right checklist for a
human to review, not render a verdict a heuristic isn't equipped to make
(see the project-wide non-goal: don't overclaim what analysis can decide).
These are all this project's own hand-authored heuristics, not ported
from anywhere — documented plainly so their limits are visible, same
approach game_report.py's own heuristics already take.
"""

import io

import chess
import chess.pgn

# --- Structural primitives ---------------------------------------------

def _pawn_files(board: chess.Board, color: chess.Color) -> set[int]:
    return {chess.square_file(sq) for sq in board.pieces(chess.PAWN, color)}


def _has_isolated_pawn(board: chess.Board, color: chess.Color, file: int) -> bool:
    """True if `color` has a pawn on `file` with no friendly pawn on
    either adjacent file — the standard definition of an isolated pawn."""
    files = _pawn_files(board, color)
    if file not in files:
        return False
    return not any(f in files for f in (file - 1, file + 1) if 0 <= f <= 7)


def _castling_sides(san_moves: list[str]) -> dict[str, str | None]:
    """{"white": "kingside"|"queenside"|None, "black": ...} — which side
    (if any) each color actually castled to, read straight off the SAN
    move list rather than replayed board state (simpler, and castling SAN
    is unambiguous: "O-O-O" is always queenside, "O-O" always kingside).
    """
    sides: dict[str, str | None] = {"white": None, "black": None}
    for i, san in enumerate(san_moves):
        color = "white" if i % 2 == 0 else "black"
        if san.startswith("O-O-O"):
            sides[color] = "queenside"
        elif san.startswith("O-O"):
            sides[color] = "kingside"
    return sides


def _pawn_storm_move_count(san_moves: list[str], color: str, files: str) -> int:
    """A rough, explicitly approximate proxy for "how many tempi has this
    side spent attacking" — counts pawn moves (and pawn captures) on the
    given files by `color`. Not a real tempo count (doesn't know if a move
    was actually useful, or account for piece moves that also attack),
    just a cheap first-order signal for "who's moved faster" in an
    opposite-wing race.
    """
    count = 0
    for i, san in enumerate(san_moves):
        move_color = "white" if i % 2 == 0 else "black"
        if move_color != color:
            continue
        token = san.rstrip("+#")
        # Pawn moves have no piece-letter prefix; a pawn capture looks
        # like "bxc3" (lowercase file letter first). A promotion suffix
        # ("=Q") comes after the destination square, so strip it first —
        # the destination square is then always exactly the last 2
        # characters, regardless of whether this was a push or a capture.
        if not token or token[0].isupper() or token in ("O-O", "O-O-O"):
            continue
        dest_file = token.split("=")[0][-2]
        if dest_file in files:
            count += 1
    return count


# --- Per-structure detectors ---------------------------------------------

LONDON_CHECKLIST = [
    "Identify whether the position is symmetric/minority-attack (no "
    "isolated queen's pawn) or an IQP structure before picking a plan — "
    "they call for opposite approaches (slow queenside expansion vs. "
    "active piece play to justify the weak pawn).",
    "In the IQP version, White's compensation is dynamic — it fades if "
    "the position simplifies without progress.",
]

DRAGON_CHECKLIST = [
    "In opposite-side castling, count tempi honestly: every non-attacking "
    "move (a defensive retreat, an unnecessary prophylactic move) is a "
    "tempo the other side's attack gets for free.",
    "Watch the timing of trading the dark-squared bishop — it's the "
    "Dragon's main defensive/attacking piece on the long diagonal, and "
    "giving it up too early (or too late) changes who's actually faster.",
    "...Rxc3 is a thematic exchange sacrifice, not an automatic one — its "
    "timing (whether it truly opens lines fast enough) is the actual "
    "judgment call, not just recognizing the pattern exists.",
]

KID_CHECKLIST = [
    "Same race-counting discipline as the Dragon: in a locked Mar del "
    "Plata center, the position is decided by who breaks through on their "
    "wing first, not by objective evaluation of any single position along "
    "the way.",
    "A locked center means piece moves that don't advance the actual "
    "wing break (f5-f4-g5 for Black, c5/b4 for White) are close to wasted "
    "tempi, not neutral.",
]

GRUNFELD_CHECKLIST = [
    "The whole point of the Grünfeld's early ...d5 is to question White's "
    "center immediately — whether White's center is genuinely strong or "
    "just big is the judgment call to slow down for, not something to "
    "answer on instinct.",
    "In the Exchange structure specifically, count how many pieces are "
    "actually attacking the center (Bg7, ...c5, ...Nc6/...Qa5 ideas) "
    "before committing to a plan that assumes it's already overextended.",
]


def _detect_london(board: chess.Board, eco_name: str | None) -> dict | None:
    if not eco_name or "london" not in eco_name.lower():
        return None
    is_iqp = _has_isolated_pawn(board, chess.WHITE, chess.square_file(chess.D4))
    structure = "london_iqp" if is_iqp else "london_symmetric"
    return {"structure": structure, "checklist": LONDON_CHECKLIST}


def _race_note(san_moves: list[str], attacker_color: str, attacker_files: str,
                defender_color: str, defender_files: str) -> str:
    """Plain-language readout of the (approximate — see
    _pawn_storm_move_count) tempo count in an opposite-wing race, for the
    report to quote directly rather than just naming the pattern."""
    attacker_count = _pawn_storm_move_count(san_moves, attacker_color, attacker_files)
    defender_count = _pawn_storm_move_count(san_moves, defender_color, defender_files)
    return (
        f"Rough pawn-storm tempo count this game: {attacker_color} spent {attacker_count} "
        f"move(s) advancing on {defender_color}'s king, {defender_color} spent {defender_count} "
        f"on the counter-storm — a proxy, not a real calculation, but worth checking against "
        f"how the race actually felt."
    )


def _detect_dragon(board: chess.Board, san_moves: list[str], eco_name: str | None) -> dict | None:
    name = (eco_name or "").lower()
    if "dragon" not in name:
        return None
    sides = _castling_sides(san_moves)
    opposite_castling = (
        sides["white"] is not None and sides["black"] is not None and sides["white"] != sides["black"]
    )
    structure = "dragon_opposite_castling" if opposite_castling else "dragon_same_side"
    checklist = list(DRAGON_CHECKLIST)
    if any(san.rstrip("+#") == "Rxc3" for san in san_moves):
        checklist.append("This game actually played ...Rxc3 — review whether the timing held up.")
    if opposite_castling:
        # White stormed on the kingside (f/g/h) at Black's king; Black
        # countered on the queenside (a/b/c) at White's.
        checklist.append(_race_note(san_moves, "white", "fgh", "black", "abc"))
    return {"structure": structure, "checklist": checklist}


def _detect_kings_indian(board: chess.Board, san_moves: list[str], eco_name: str | None) -> dict | None:
    name = (eco_name or "").lower()
    if "king's indian" not in name and "kings indian" not in name:
        return None
    # The Mar del Plata signature: a locked e5/d6 (Black) vs d5 (White)
    # center — White's d-pawn and Black's e-pawn on adjacent, blocked
    # squares one rank apart.
    white_pawns = board.pieces(chess.PAWN, chess.WHITE)
    black_pawns = board.pieces(chess.PAWN, chess.BLACK)
    locked_center = chess.D5 in white_pawns and chess.E5 in black_pawns and chess.D6 in black_pawns
    structure = "kid_mar_del_plata" if locked_center else "kid_other"
    checklist = list(KID_CHECKLIST)
    if locked_center:
        # Classic Mar del Plata race: White expands queenside (b/c),
        # Black storms kingside (f/g).
        checklist.append(_race_note(san_moves, "black", "fg", "white", "bc"))
    return {"structure": structure, "checklist": checklist}


def _detect_grunfeld(board: chess.Board, eco_name: str | None) -> dict | None:
    name = (eco_name or "").lower()
    if "grünfeld" not in name and "grunfeld" not in name:
        return None
    white_pawn_files = _pawn_files(board, chess.WHITE)
    black_has_d_pawn = any(chess.square_file(sq) == chess.square_file(chess.D4) for sq in board.pieces(chess.PAWN, chess.BLACK))
    is_exchange = (
        chess.square_file(chess.C4) in white_pawn_files
        and chess.square_file(chess.D4) in white_pawn_files
        and not black_has_d_pawn
    )
    structure = "grunfeld_exchange" if is_exchange else "grunfeld_other"
    return {"structure": structure, "checklist": GRUNFELD_CHECKLIST}


def detect_structure(pgn_text: str, eco_name: str | None) -> dict | None:
    """Returns {"structure": str, "checklist": [str, ...]} for the first
    matching repertoire structure, or None if this game doesn't reach any
    of them. Checked against the position after the game's last move
    (or the final position reached, for a game that ended early) — good
    enough for a structural fact that, once reached, mostly persists
    (a locked center rarely unlocks; an IQP rarely un-isolates).
    """
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return None
    board = game.end().board()
    san_moves = _san_moves(game)

    for detector in (
        lambda: _detect_london(board, eco_name),
        lambda: _detect_dragon(board, san_moves, eco_name),
        lambda: _detect_kings_indian(board, san_moves, eco_name),
        lambda: _detect_grunfeld(board, eco_name),
    ):
        match = detector()
        if match:
            return match
    return None


def _san_moves(game) -> list[str]:
    moves = []
    board = game.board()
    for move in game.mainline_moves():
        moves.append(board.san(move))
        board.push(move)
    return moves

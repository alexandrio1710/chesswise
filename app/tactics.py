"""
Tactical-motif detection — forks, pins, skewers, discovered attacks, hanging
pieces, mate patterns. Pure python-chess board inspection: no engine calls.

Feeds three places that all want the same answer to "what kind of tactic is
this move?": puzzle themes (puzzles.py), the review coach's explanations
(explain.py), and Insights' found-vs-missed tactic counts (review.py).

These are this project's own heuristics, described plainly so their limits
are visible: they recognise the textbook shape of each motif on the board,
they don't prove the tactic wins anything. Callers that care whether a
tactic actually *works* pair a motif with the engine's own verdict (e.g. only
counting a fork as "available" when it was also the engine's best move).
"""

from __future__ import annotations

import itertools

import chess

from game_report import PIECE_VALUES, _is_sacrifice, _see

# game_report.PIECE_VALUES has the king at 0 (a placeholder, never exercised
# as a *captured* piece there); motif detection needs it to rank above
# everything so "a piece pinned to the king" reads as a pin, not a wash.
VALUE = {**PIECE_VALUES, chess.KING: 100}

# Smallest piece worth calling "a piece" for hanging-piece purposes — leaving
# a pawn en prise is a different (and much more common) kind of slip.
MIN_HANGING_VALUE = 3

SLIDER_DIRECTIONS = {
    chess.BISHOP: [(1, 1), (1, -1), (-1, 1), (-1, -1)],
    chess.ROOK: [(1, 0), (-1, 0), (0, 1), (0, -1)],
    chess.QUEEN: [(1, 1), (1, -1), (-1, 1), (-1, -1), (1, 0), (-1, 0), (0, 1), (0, -1)],
}

MOTIF_LABELS = {
    "mate": "Checkmate",
    "back_rank_mate": "Back-rank mate",
    "smothered_mate": "Smothered mate",
    "fork": "Fork",
    "pin": "Pin",
    "skewer": "Skewer",
    "discovered_attack": "Discovered attack",
    "discovered_check": "Discovered check",
    "hanging_piece": "Hanging piece",
    "promotion": "Promotion",
    "sacrifice": "Sacrifice",
    "defensive": "Defense",
    "check": "Check",
}

# The themes a puzzle can be filtered by, in the order the UI lists them.
THEME_KEYS = [
    "mate", "fork", "pin", "skewer", "discovered_attack", "hanging_piece",
    "back_rank_mate", "smothered_mate", "promotion", "sacrifice", "defensive",
]


def theme_label(key: str) -> str:
    if key.startswith("mate_in_"):
        return f"Mate in {key.removeprefix('mate_in_')}"
    return MOTIF_LABELS.get(key, key.replace("_", " ").capitalize())


def _value(piece: chess.Piece) -> int:
    return VALUE[piece.piece_type]


def _ray(board: chess.Board, start: chess.Square, df: int, dr: int):
    """Occupied squares (with their pieces) walking outward from `start`."""
    f, r = chess.square_file(start) + df, chess.square_rank(start) + dr
    while 0 <= f < 8 and 0 <= r < 8:
        sq = chess.square(f, r)
        piece = board.piece_at(sq)
        if piece:
            yield sq, piece
        f, r = f + df, r + dr


def hanging_pieces(board: chess.Board, color: chess.Color, min_value: int = MIN_HANGING_VALUE) -> list[chess.Square]:
    """Pieces of `color` the opponent could win by capturing: attacked and
    either undefended, or attacked by something worth less than the piece
    itself (trading a knight for a pawn is still losing a knight).
    """
    hanging = []
    for sq, piece in board.piece_map().items():
        if piece.color != color or piece.piece_type == chess.KING or _value(piece) < min_value:
            continue
        attackers = board.attackers(not color, sq)
        if not attackers:
            continue
        defended = bool(board.attackers(color, sq))
        cheapest = min(VALUE[board.piece_type_at(a)] for a in attackers)
        if not defended or cheapest < _value(piece):
            hanging.append(sq)
    return sorted(hanging)


def mate_pattern(board: chess.Board) -> str | None:
    """Name the mate just delivered, given a position where the side to move
    is checkmated. None if it isn't checkmate at all."""
    if not board.is_checkmate():
        return None
    color = board.turn
    king = board.king(color)
    checkers = list(board.checkers())

    if all(board.piece_type_at(c) == chess.KNIGHT for c in checkers):
        neighbours = [chess.square(f, r)
                      for f in range(max(0, chess.square_file(king) - 1), min(7, chess.square_file(king) + 1) + 1)
                      for r in range(max(0, chess.square_rank(king) - 1), min(7, chess.square_rank(king) + 1) + 1)
                      if (f, r) != (chess.square_file(king), chess.square_rank(king))]
        if all((p := board.piece_at(s)) is not None and p.color == color for s in neighbours):
            return "smothered_mate"

    back_rank = 0 if color == chess.WHITE else 7
    if chess.square_rank(king) == back_rank and all(
        board.piece_type_at(c) in (chess.ROOK, chess.QUEEN) and chess.square_rank(c) == back_rank for c in checkers
    ):
        forward = back_rank + (1 if color == chess.WHITE else -1)
        squares = [chess.square(f, forward) for f in (chess.square_file(king) - 1, chess.square_file(king), chess.square_file(king) + 1)
                   if 0 <= f < 8]
        if all(
            ((p := board.piece_at(s)) is not None and p.color == color) or board.is_attacked_by(not color, s)
            for s in squares
        ):
            return "back_rank_mate"
    return "mate"


def _line_motifs(after: chess.Board, to: chess.Square, mover: chess.Color) -> set[str]:
    """Pin / skewer along the rays the just-moved slider now stares down."""
    piece = after.piece_at(to)
    motifs: set[str] = set()
    if piece is None or piece.piece_type not in SLIDER_DIRECTIONS:
        return motifs
    for df, dr in SLIDER_DIRECTIONS[piece.piece_type]:
        first_two = list(itertools.islice(_ray(after, to, df, dr), 2))
        if len(first_two) < 2:
            continue
        (_, front), (_, behind) = first_two
        if front.color == mover or behind.color == mover:
            continue
        front_value, behind_value = _value(front), _value(behind)
        # A pawn "pinned" to a knight, or a king "skewering" a pawn, is the
        # textbook shape but never the tactic anyone means — the piece in
        # front has to be worth something (or be shielding the king), and
        # so does whatever is behind it in a skewer.
        if behind_value > front_value and (front_value >= MIN_HANGING_VALUE or behind.piece_type == chess.KING):
            motifs.add("pin")
        elif front_value > behind_value and behind_value >= MIN_HANGING_VALUE:
            motifs.add("skewer")
    return motifs


def move_motifs(board: chess.Board, move: chess.Move) -> set[str]:
    """Every tactical motif the move `move` (played from `board`) carries.

    Empty for an ordinary quiet move. Never raises for a legal move.
    """
    mover = board.turn
    moved = board.piece_at(move.from_square)
    if moved is None:
        return set()
    motifs: set[str] = set()

    after = board.copy()
    after.push(move)

    if after.is_checkmate():
        motifs.add("mate")
        pattern = mate_pattern(after)
        if pattern and pattern != "mate":
            motifs.add(pattern)
    if after.is_check():
        motifs.add("check")
    if move.promotion:
        motifs.add("promotion")

    if board.is_capture(move) and not board.is_en_passant(move):
        captured = board.piece_at(move.to_square)
        if captured is not None and _value(captured) >= MIN_HANGING_VALUE and not board.attackers(not mover, move.to_square):
            motifs.add("hanging_piece")

    # The moved piece must survive being captured, or "fork"/"pin" isn't real.
    safe = _see(after, move.to_square) == 0
    landing = after.piece_at(move.to_square)

    if safe and landing is not None:
        motifs |= _line_motifs(after, move.to_square, mover)
        if landing.piece_type != chess.KING:
            targets = 0
            for sq in after.attacks(move.to_square):
                target = after.piece_at(sq)
                if target is None or target.color == mover:
                    continue
                if target.piece_type == chess.KING:
                    targets += 1
                elif _value(target) >= MIN_HANGING_VALUE and (
                    _value(target) > _value(landing) or not after.attackers(not mover, sq)
                ):
                    targets += 1
            if targets >= 2:
                motifs.add("fork")

    for sq, piece in board.piece_map().items():
        if piece.color != mover or piece.piece_type not in SLIDER_DIRECTIONS or sq == move.from_square:
            continue
        before_attacks = board.attacks(sq)
        for target_sq in after.attacks(sq):
            target = after.piece_at(target_sq)
            if target is None or target.color == mover or target_sq in before_attacks:
                continue
            if target.piece_type == chess.KING:
                motifs.add("discovered_check")
                motifs.add("discovered_attack")
            elif _value(target) >= MIN_HANGING_VALUE:
                motifs.add("discovered_attack")

    if _is_sacrifice(board, move):
        motifs.add("sacrifice")

    before_hanging = hanging_pieces(board, mover)
    if before_hanging and not motifs & {"mate", "hanging_piece", "fork", "pin", "skewer"}:
        after_hanging = set(hanging_pieces(after, mover))
        if len(after_hanging) < len(before_hanging):
            motifs.add("defensive")

    return motifs


def puzzle_themes(fen: str, best_uci: str, mate_in: int | None = None) -> list[str]:
    """Theme keys for a puzzle whose answer is `best_uci` from `fen`.
    `mate_in` is the mover-perspective mate distance when the best line is a
    forced mate (positive), else None."""
    board = chess.Board(fen)
    try:
        move = chess.Move.from_uci(best_uci)
    except ValueError:
        return []
    if move not in board.legal_moves:
        return []
    motifs = move_motifs(board, move)
    themes = [k for k in THEME_KEYS if k in motifs]
    if mate_in is not None and mate_in > 0:
        themes.insert(0, f"mate_in_{min(mate_in, 5)}")
        if "mate" not in themes:
            themes.insert(1, "mate")
    return themes

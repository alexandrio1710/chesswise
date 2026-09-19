"""
Coach-style explanations for a reviewed game, in plain English.

chess.com's "Explain" feature is a proprietary coach; this is this project's
own version, built only from things it can actually establish: the engine's
best move and line at each position (stored by the review pass), what the
board says about those moves (tactics.move_motifs — forks, pins, hanging
pieces, mates), and how much material the engine's line wins or loses.
Deterministic — the same game always gets the same text — and every claim is
something checked on the board or read off an engine line, never guessed. When
it can't say *why* a move was bad, it says what the engine preferred and how
the evaluation moved, not something invented.
"""

from __future__ import annotations

import chess

from tactics import (
    VALUE, hanging_pieces, material_balance, motif_details, move_motifs, piece_name, pv_material_swing,
)

MATE_CP = 9000

CLASS_LABEL = {
    "brilliant": "brilliant", "great": "a great move", "best": "the best move", "excellent": "an excellent move",
    "good": "a good move", "book": "a book move", "inaccuracy": "an inaccuracy", "mistake": "a mistake",
    "miss": "a miss", "blunder": "a blunder",
}
BAD_CLASSES = ("inaccuracy", "mistake", "miss", "blunder")


def _join(names: list[str]) -> str:
    names = list(dict.fromkeys(names))
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _the(name: str) -> str:
    return f"the {name}"


def fmt_eval(cp: float | None, mate: int | None = None) -> str:
    """Eval in pawns from the mover's perspective ("+1.20", "-0.40", "mate in 3")."""
    if mate is not None:
        return f"mate in {abs(mate)}" if mate > 0 else f"getting mated in {abs(mate)}"
    if cp is None:
        return "?"
    if abs(cp) >= MATE_CP:
        return "a forced mate" if cp > 0 else "getting mated"
    return f"{cp / 100:+.2f}"


def wins_phrase(swing: int) -> str:
    """"wins ..." for a material gain of `swing` pawns."""
    if swing <= 1:
        return "wins a pawn"
    if swing == 2:
        return "wins two pawns"
    if swing == 3:
        return "wins a piece"
    if swing in (4, 5):
        return "wins a rook" if swing == 5 else "wins material"
    if swing >= 9:
        return "wins the queen"
    return "wins a lot of material"


def _captured_name(board: chess.Board, move: chess.Move) -> str | None:
    if not board.is_capture(move):
        return None
    if board.is_en_passant(move):
        return "pawn"
    return piece_name(board.piece_at(move.to_square))


def describe_move_effect(board: chess.Board, move: chess.Move, pv: list[str] | None = None,
                         mate_in: int | None = None) -> str | None:
    """What a move accomplishes, as a verb phrase ("forks the queen and the
    king", "wins a piece"), or None for a plain quiet move."""
    mover = board.turn
    after = board.copy()
    after.push(move)

    if after.is_checkmate():
        motifs = move_motifs(board, move)
        if "smothered_mate" in motifs:
            return "delivers a smothered mate"
        if "back_rank_mate" in motifs:
            return "delivers a back-rank mate"
        return "checkmates the king"
    if mate_in is not None and mate_in > 0:
        return f"leads to a forced mate in {mate_in}"

    details = motif_details(board, move)
    if "fork" in details:
        return "forks " + _join([_the(n) for n in details["fork"]])
    if "pin" in details:
        front, behind = details["pin"][0]
        return f"pins the {front} to the {behind}"
    if "skewer" in details:
        front, behind = details["skewer"][0]
        return f"skewers the {front} and the {behind}"
    if "discovered_attack" in details:
        return f"uncovers an attack on the {details['discovered_attack'][0]}"

    swing = pv_material_swing(board.fen(), pv or [move.uci()], mover)
    captured = _captured_name(board, move)
    target = board.piece_at(move.to_square)
    if captured and target is not None and VALUE[target.piece_type] >= 3 and not board.attackers(not mover, move.to_square):
        return f"captures the undefended {captured}"
    if swing >= 2:
        return wins_phrase(swing)
    if captured:
        return f"captures the {captured}"
    if move.promotion:
        return f"promotes to a {piece_name(chess.Piece(move.promotion, mover))}"
    if after.is_check():
        return "gives check"
    if board.is_castling(move):
        return "castles to safety"
    return None


def _san(board: chess.Board, uci: str | None) -> tuple[chess.Move | None, str | None]:
    if not uci:
        return None, None
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        return None, None
    if move not in board.legal_moves:
        return None, None
    return move, board.san(move)


def explain_move(board_before: chess.Board, move: chess.Move, *, classification: str,
                 eval_before_cp: float | None, eval_after_cp: float | None,
                 best_uci: str | None, best_pv: list[str] | None, best_mate_in: int | None,
                 reply_uci: str | None = None, reply_pv: list[str] | None = None, reply_mate_in: int | None = None,
                 opening_name: str | None = None) -> dict:
    """Explain one move. Evals are from the MOVER's perspective (mate-coded
    as +-~10000); `reply_*` is the opponent's best reply after the move, as
    the opponent sees it (so a positive reply_mate_in means they have mate)."""
    mover = board_before.turn
    san = board_before.san(move)
    best_move, best_san = _san(board_before, best_uci)
    played_is_best = best_move == move
    after = board_before.copy()
    after.push(move)

    label = CLASS_LABEL.get(classification, classification)
    headline = f"{san} is {label}"

    best_effect = None
    if best_move is not None:
        best_effect = describe_move_effect(board_before, best_move, best_pv, best_mate_in)

    sentences: list[str] = []
    if classification == "book":
        sentences.append(f"This is a well-known opening move{f' in the {opening_name}' if opening_name else ''}.")
    elif classification == "brilliant":
        effect = describe_move_effect(board_before, move, best_pv, best_mate_in)
        gave_up = "sacrifice" in move_motifs(board_before, move)
        sentences.append(
            f"You {'sacrificed material' if gave_up else 'found a difficult move'}"
            f"{f' — it {effect}' if effect else ''}, and the engine agrees it's best."
        )
    elif classification == "great":
        if eval_before_cp is not None and eval_before_cp >= 100:
            sentences.append("Nearly every other move lets the advantage slip — this was the only way to keep it.")
        elif eval_before_cp is not None and eval_before_cp <= -100:
            sentences.append("Nearly every other move loses — this was the only way to stay in the game.")
        else:
            sentences.append("A critical moment where almost every other move was worse — you found the right one.")
    elif classification == "best":
        effect = describe_move_effect(board_before, move, best_pv, best_mate_in)
        if effect:
            sentences.append(f"It {effect}.")
    elif classification in ("excellent", "good"):
        if not played_is_best and best_san:
            sentences.append(f"The best move was {best_san}" + (f", which {best_effect}" if best_effect else "") + ".")
    elif classification in BAD_CLASSES:
        sentences.extend(_bad_move_reasons(
            board_before, after, move, mover, best_move, best_san, best_effect, best_mate_in,
            eval_before_cp, eval_after_cp, reply_uci, reply_pv, reply_mate_in, classification,
        ))

    if best_effect:
        hint_kind = "checkmate" if (best_mate_in and best_mate_in > 0) else (
            "fork" if best_effect.startswith("forks") else "idea")
    else:
        hint_kind = "idea"

    return {
        "headline": headline,
        "text": " ".join(sentences),
        "best_uci": best_move.uci() if best_move else None,
        "best_san": best_san,
        "best_effect": best_effect,
        "played_is_best": played_is_best,
        "hint_kind": hint_kind,
        "motifs": sorted(move_motifs(board_before, move)),
        "eval_before": fmt_eval(eval_before_cp),
        "eval_after": fmt_eval(eval_after_cp),
    }


def _bad_move_reasons(board_before, after, move, mover, best_move, best_san, best_effect, best_mate_in,
                      eval_before_cp, eval_after_cp, reply_uci, reply_pv, reply_mate_in, classification) -> list[str]:
    reasons: list[str] = []
    reply_move, reply_san = _san(after, reply_uci)

    missed_mate = best_mate_in is not None and best_mate_in > 0 and (eval_after_cp is None or eval_after_cp < MATE_CP)
    if missed_mate and best_san:
        reasons.append(f"You had a forced mate in {best_mate_in} with {best_san}.")
    elif reply_mate_in is not None and reply_mate_in > 0 and reply_san:
        reasons.append(f"This allows {reply_san}, and a forced mate in {reply_mate_in}.")
    else:
        swing = pv_material_swing(after.fen(), reply_pv or ([reply_uci] if reply_uci else []), not mover) if reply_uci else 0
        if reply_san and swing >= 1:
            captured = _captured_name(after, reply_move) if reply_move else None
            if captured and swing >= 3:
                reasons.append(f"This allows {reply_san}, winning your {captured}.")
            else:
                reasons.append(f"This allows {reply_san}, which {wins_phrase(swing)}.")
        else:
            hanging = hanging_pieces(after, mover)
            if hanging:
                sq = max(hanging, key=lambda s: VALUE[after.piece_type_at(s)])
                reasons.append(f"Your {piece_name(after.piece_at(sq))} on {chess.square_name(sq)} is left hanging.")

    if best_san and best_move != move and not missed_mate:
        reasons.append(f"The best move was {best_san}" + (f", which {best_effect}" if best_effect else "") + ".")

    if not reasons:
        reasons.append(f"This drops your evaluation from {fmt_eval(eval_before_cp)} to {fmt_eval(eval_after_cp)}.")
    if classification == "miss" and reasons and not missed_mate:
        reasons.insert(0, "You had a winning position and let it slip.")
    return reasons


def grade_from_accuracy(accuracy: float | None) -> str | None:
    """A phase's move-quality label, from its accuracy — the closest analogue
    to chess.com's per-phase report-card icons."""
    if accuracy is None:
        return None
    if accuracy >= 93:
        return "best"
    if accuracy >= 85:
        return "excellent"
    if accuracy >= 75:
        return "good"
    if accuracy >= 62:
        return "inaccuracy"
    if accuracy >= 45:
        return "mistake"
    return "blunder"


def game_summary(*, you: str, accuracy: dict, class_counts: dict, weakest_phase: str | None,
                 turning_point: dict | None, opening: str | None) -> str:
    """A short coach paragraph about the whole game, from `you`'s side."""
    other = "black" if you == "white" else "white"
    parts = []
    mine, theirs = accuracy.get(you), accuracy.get(other)
    if mine is not None:
        sentence = f"You played {you.capitalize()} with {mine}% accuracy"
        if theirs is not None:
            sentence += f" (your opponent: {theirs}%)"
        parts.append(sentence + ".")
    counts = class_counts.get(you, {})
    highlights = []
    if counts.get("brilliant"):
        highlights.append(f"{counts['brilliant']} brilliant move{'s' if counts['brilliant'] != 1 else ''}")
    if counts.get("great"):
        highlights.append(f"{counts['great']} great move{'s' if counts['great'] != 1 else ''}")
    if highlights:
        parts.append("You found " + _join(highlights) + ".")
    errors = []
    for key, singular, plural in (("blunder", "blunder", "blunders"), ("mistake", "mistake", "mistakes"),
                                  ("miss", "miss", "misses")):
        n = counts.get(key, 0)
        if n:
            errors.append(f"{n} {singular if n == 1 else plural}")
    if errors:
        parts.append("Your side had " + _join(errors) + ".")
    elif mine is not None:
        parts.append("No blunders, mistakes or misses on your side — a clean game.")
    if turning_point:
        parts.append(f"The turning point was move {turning_point['move_number']} ({turning_point['san']}), "
                     f"{turning_point['label']}.")
    if weakest_phase:
        parts.append(f"The {weakest_phase} was your weakest phase.")
    return " ".join(parts)

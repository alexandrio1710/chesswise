"""
Insights, in the shape chess.com's Insights page has: one filter bar (time
class, color, date range, source/profile) and a set of sections that all
describe the same filtered set of games —

  overview   results, accuracy, games & accuracy over time, accuracy by move number
  results    how games end (checkmate / resignation / time / ...), from your side
  shapes     game shapes (balanced, sharp, wild, giveaway, smooth, sudden, intense)
  phases     opening / middlegame / endgame accuracy and error rate
  openings   your most-played openings per color, with results and book depth
  tactics    forks, pins, skewers, discovered attacks, mates, free and hanging
             pieces — found and missed, by you and by your opponents
  moves      move-quality mix, quality over time, per-piece accuracy, castling
  calendar   when you play and how it goes (in the viewer's own timezone)

Everything comes from data this app already stores per game — the eval trace
(game_moves), the review pass (classifications, best moves, game_tactics) and
the PGN-derived metadata (termination, shape, book depth). Sections that need
the review pass only count reviewed games and report how many that is, rather
than quietly extrapolating from a subset.
"""

from __future__ import annotations

import io
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

import chess
import chess.pgn

import game_meta
import insights
import review
import stats
from db import get_connection
from insights import Filters

CLASS_KEYS = ["brilliant", "great", "best", "excellent", "good", "book", "inaccuracy", "mistake", "miss", "blunder"]
PHASES = ["opening", "middlegame", "endgame"]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Tactic motifs shown as found/missed, and the ones shown as a pair of counts.
FOUND_MISSED_MOTIFS = [
    ("fork", "Forks"), ("pin", "Pins"), ("skewer", "Skewers"),
    ("discovered_attack", "Discovered attacks"), ("mate", "Forced mates"),
]
MIN_SAMPLES_PER_MOVE_NUMBER = 3
MAX_MOVE_NUMBER = 45
OPENINGS_PER_COLOR = 10

_RESULT_LABELS = {
    "win": {
        "checkmate": "You checkmated", "resignation": "Opponent resigned",
        "timeout": "Opponent out of time", "abandonment": "Opponent abandoned",
    },
    "loss": {
        "checkmate": "You were checkmated", "resignation": "You resigned",
        "timeout": "You ran out of time", "abandonment": "You abandoned",
    },
    "draw": {
        "agreement": "Draw by agreement", "stalemate": "Stalemate", "repetition": "Repetition",
        "insufficient_material": "Insufficient material", "fifty_move": "50-move rule",
        "timeout_vs_insufficient": "Timeout vs. insufficient material",
    },
}


# --- loading ----------------------------------------------------------------------------


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _pct(part: int, whole: int) -> float | None:
    return round(part / whole * 100, 1) if whole else None


def _avg(values: list[float], digits: int = 1) -> float | None:
    return round(sum(values) / len(values), digits) if values else None


def _record(games: list[dict]) -> dict:
    wins = sum(1 for g in games if g["result"] == "win")
    draws = sum(1 for g in games if g["result"] == "draw")
    losses = sum(1 for g in games if g["result"] == "loss")
    return {"games": len(games), "wins": wins, "draws": draws, "losses": losses,
            "win_rate_pct": _pct(wins, len(games)), "draw_rate_pct": _pct(draws, len(games)),
            "loss_rate_pct": _pct(losses, len(games))}


def _load(flt: Filters) -> list[dict]:
    """The filtered games, each with its moves and per-game/per-move
    accuracy already worked out, so every section reads the same numbers."""
    where, params = insights._source_clause(flt.source, flt.profile_id, flt=flt)
    conn = get_connection()
    try:
        games = [dict(r) for r in conn.execute(
            f"SELECT g.id, g.source, g.date, g.opponent, g.result, g.color, g.time_control, g.opening_name, g.eco, "
            f"g.player_rating, g.opponent_rating, g.termination_method, g.game_shape, g.book_plies "
            f"FROM games g WHERE g.analyzed = 1 AND g.skip_reason IS NULL {where} ORDER BY g.date", params)]
        by_game: dict[int, list[dict]] = defaultdict(list)
        for r in conn.execute(
            f"SELECT gm.game_id, gm.ply, gm.move_number, gm.color_moved, gm.move_san, gm.eval_cp, gm.eval_drop, "
            f"gm.tier, gm.classification, gm.phase FROM game_moves gm JOIN games g ON g.id = gm.game_id "
            f"WHERE g.analyzed = 1 AND g.skip_reason IS NULL {where} ORDER BY gm.game_id, gm.ply", params):
            by_game[r["game_id"]].append(dict(r))
    finally:
        conn.close()

    pending = set(review.games_needing_review([g["id"] for g in games]))
    out = []
    for g in games:
        moves = by_game.get(g["id"], [])
        dt = _parse_dt(g["date"])
        opp = "black" if g["color"] == "white" else "white"
        g.update({
            "moves": moves, "dt": dt, "day": dt.date() if dt else None, "reviewed": g["id"] not in pending,
            "maccs": stats.move_accuracies(moves),
            "acc": stats.compute_game_accuracy(moves, g["color"]) if len(moves) >= 2 else None,
            "opp_acc": stats.compute_game_accuracy(moves, opp) if len(moves) >= 2 else None,
            "acpl": stats.compute_game_acpl(moves, g["color"]) if moves else None,
        })
        out.append(g)
    return out


# --- time bucketing -----------------------------------------------------------------------


def _granularity(games: list[dict]) -> str:
    days = [g["day"] for g in games if g["day"]]
    if not days:
        return "week"
    span = (max(days) - min(days)).days
    return "day" if span <= 45 else "week" if span <= 400 else "month"


def _bucket(d: date, gran: str) -> str:
    if gran == "day":
        return d.isoformat()
    if gran == "week":
        return (d - timedelta(days=d.weekday())).isoformat()
    return d.replace(day=1).isoformat()


# --- sections -------------------------------------------------------------------------------


def _overview(games: list[dict], gran: str) -> dict:
    over_time: dict[str, list[dict]] = defaultdict(list)
    for g in games:
        if g["day"]:
            over_time[_bucket(g["day"], gran)].append(g)
    series = []
    for period in sorted(over_time):
        rec = _record(over_time[period])
        rec["period"] = period
        rec["accuracy"] = _avg([g["acc"] for g in over_time[period] if g["acc"] is not None])
        series.append(rec)

    by_number: dict[int, dict[str, list[float]]] = defaultdict(lambda: {"you": [], "opp": []})
    for g in games:
        for m in g["moves"]:
            acc = g["maccs"].get(m["ply"])
            if acc is None or m["move_number"] > MAX_MOVE_NUMBER:
                continue
            by_number[m["move_number"]]["you" if m["color_moved"] == g["color"] else "opp"].append(acc)
    accuracy_by_move = [
        {"move": n, "you": _avg(v["you"]), "opp": _avg(v["opp"]), "samples": len(v["you"])}
        for n, v in sorted(by_number.items()) if len(v["you"]) >= MIN_SAMPLES_PER_MOVE_NUMBER
    ]
    return {
        **_record(games),
        "accuracy": _avg([g["acc"] for g in games if g["acc"] is not None]),
        "opponent_accuracy": _avg([g["opp_acc"] for g in games if g["opp_acc"] is not None]),
        "granularity": gran, "over_time": series, "accuracy_by_move": accuracy_by_move,
    }


def _results(games: list[dict]) -> dict:
    out: dict[str, list[dict]] = {}
    for result in ("win", "loss", "draw"):
        counts = Counter(g["termination_method"] or "other" for g in games if g["result"] == result)
        out[result] = [
            {"method": m, "count": c, "label": _RESULT_LABELS[result].get(m) or game_meta.TERMINATION_LABELS.get(m, m)}
            for m, c in counts.most_common()
        ]
    return out


def _shapes(games: list[dict]) -> list[dict]:
    rows = []
    for shape in game_meta.SHAPES:
        picked = [g for g in games if g["game_shape"] == shape]
        if picked:
            rows.append({"shape": shape, "blurb": game_meta.SHAPE_BLURBS[shape], **_record(picked)})
    return rows


def _phases(games: list[dict]) -> dict:
    acc: dict[str, dict[str, list[float]]] = {p: {"you": [], "opp": []} for p in PHASES}
    counts = {p: {"you": 0, "opp": 0} for p in PHASES}
    errors = {p: {"you": 0, "opp": 0} for p in PHASES}
    ended = {p: [] for p in PHASES}
    for g in games:
        last_phase = None
        for m in g["moves"]:
            phase = m["phase"]
            if phase not in acc:
                continue
            last_phase = phase
            side = "you" if m["color_moved"] == g["color"] else "opp"
            counts[phase][side] += 1
            if m["tier"] in ("mistake", "blunder"):
                errors[phase][side] += 1
            a = g["maccs"].get(m["ply"])
            if a is not None:
                acc[phase][side].append(a)
        if last_phase:
            ended[last_phase].append(g)
    return {
        "phases": [
            {"phase": p,
             "you": {"accuracy": _avg(acc[p]["you"]), "moves": counts[p]["you"],
                     "errors_per_100": round(errors[p]["you"] / counts[p]["you"] * 100, 1) if counts[p]["you"] else None},
             "opp": {"accuracy": _avg(acc[p]["opp"]), "moves": counts[p]["opp"],
                     "errors_per_100": round(errors[p]["opp"] / counts[p]["opp"] * 100, 1) if counts[p]["opp"] else None}}
            for p in PHASES
        ],
        "ended_in": [{"phase": p, **_record(ended[p])} for p in PHASES],
    }


def _family(name: str | None) -> str:
    return (name or "Unknown opening").split(":")[0].strip() or "Unknown opening"


def _openings(games: list[dict]) -> dict:
    out = {}
    for color in ("white", "black"):
        groups: dict[str, list[dict]] = defaultdict(list)
        for g in games:
            if g["color"] == color:
                groups[_family(g["opening_name"])].append(g)
        rows = []
        for family, picked in groups.items():
            # book_plies counts both sides' moves; own book moves are the half that were this player's.
            own_book = [(g["book_plies"] + 1) // 2 if color == "white" else g["book_plies"] // 2
                        for g in picked if g["book_plies"] is not None]
            variations = defaultdict(list)
            for g in picked:
                variations[g["opening_name"] or family].append(g)
            rows.append({
                "opening": family, "eco": Counter(g["eco"] for g in picked if g["eco"]).most_common(1)[0][0] if any(g["eco"] for g in picked) else None,
                **_record(picked),
                "accuracy": _avg([g["acc"] for g in picked if g["acc"] is not None]),
                "avg_book_moves": _avg(own_book), "max_book_moves": max(own_book) if own_book else None,
                "variations": [{"name": n, **_record(v)} for n, v in sorted(variations.items(), key=lambda kv: -len(kv[1]))[:4]],
            })
        rows.sort(key=lambda r: (-r["games"], r["opening"]))
        out[color] = rows[:OPENINGS_PER_COLOR]
    return out


def _tactics(games: list[dict], flt: Filters) -> dict:
    where, params = insights._source_clause(flt.source, flt.profile_id, flt=flt)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"SELECT gt.motif, gt.outcome, (gt.color = g.color) AS mine FROM game_tactics gt JOIN games g ON g.id = gt.game_id "
            f"WHERE g.analyzed = 1 AND g.skip_reason IS NULL {where}", params).fetchall()
    finally:
        conn.close()
    counts: Counter = Counter((r["motif"], "you" if r["mine"] else "opp", r["outcome"]) for r in rows)

    def pair(motif: str, side: str, good: str, bad: str) -> dict:
        found, missed = counts[(motif, side, good)], counts[(motif, side, bad)]
        return {good: found, bad: missed, "rate_pct": _pct(found, found + missed)}

    return {
        "found_missed": [
            {"motif": m, "label": label, "you": pair(m, "you", "found", "missed"), "opp": pair(m, "opp", "found", "missed")}
            for m, label in FOUND_MISSED_MOTIFS
        ],
        "free_pieces": {"you": pair("free_piece", "you", "taken", "ignored"), "opp": pair("free_piece", "opp", "taken", "ignored")},
        "left_hanging": {"you": pair("left_hanging", "you", "punished", "escaped"), "opp": pair("left_hanging", "opp", "punished", "escaped")},
        "reviewed_games": sum(1 for g in games if g["reviewed"]),
    }


def _piece_of(san: str) -> str:
    if san.startswith("O-O"):
        return "K"
    return san[0] if san and san[0] in "KQRBN" else "P"


def _moves(games: list[dict], gran: str) -> dict:
    reviewed = [g for g in games if g["reviewed"]]
    quality = {"you": Counter(), "opp": Counter()}
    over_time: dict[str, Counter] = defaultdict(Counter)
    for g in reviewed:
        for m in g["moves"]:
            cls = m["classification"]
            if cls not in CLASS_KEYS:
                continue
            side = "you" if m["color_moved"] == g["color"] else "opp"
            quality[side][cls] += 1
            if side == "you" and g["day"]:
                over_time[_bucket(g["day"], gran)][cls] += 1

    def shares(counter: Counter) -> dict:
        total = sum(counter.values())
        return {"total": total, "counts": {k: counter.get(k, 0) for k in CLASS_KEYS},
                "pct": {k: _pct(counter.get(k, 0), total) for k in CLASS_KEYS}}

    series = []
    for period in sorted(over_time):
        c = over_time[period]
        total = sum(c.values())
        series.append({"period": period, "moves": total,
                       **{k: _pct(c.get(k, 0), total) for k in ("best", "excellent", "good", "inaccuracy", "mistake", "miss", "blunder")}})

    per_piece: dict[str, dict] = {p: {"moves": 0, "acc": [], "errors": 0} for p in "PNBRQK"}
    castle: dict[str, list[dict]] = {"kingside": [], "queenside": [], "none": []}
    castle_move: dict[str, list[int]] = {"kingside": [], "queenside": []}
    for g in games:
        castled = None
        for m in g["moves"]:
            if m["color_moved"] != g["color"]:
                continue
            bucket = per_piece[_piece_of(m["move_san"])]
            bucket["moves"] += 1
            if m["tier"] in ("mistake", "blunder"):
                bucket["errors"] += 1
            a = g["maccs"].get(m["ply"])
            if a is not None:
                bucket["acc"].append(a)
            if castled is None and m["move_san"].startswith("O-O"):
                castled = "queenside" if m["move_san"].startswith("O-O-O") else "kingside"
                castle_move[castled].append(m["move_number"])
        castle[castled or "none"].append(g)
    total_moves = sum(b["moves"] for b in per_piece.values())
    names = {"P": "Pawn", "N": "Knight", "B": "Bishop", "R": "Rook", "Q": "Queen", "K": "King"}
    return {
        "reviewed_games": len(reviewed),
        "quality": {"you": shares(quality["you"]), "opp": shares(quality["opp"])},
        "over_time": series,
        "pieces": [
            {"piece": p, "name": names[p], "moves": b["moves"], "share_pct": _pct(b["moves"], total_moves),
             "accuracy": _avg(b["acc"]), "errors_per_100": round(b["errors"] / b["moves"] * 100, 1) if b["moves"] else None}
            for p, b in per_piece.items()
        ],
        "castling": [
            {"side": side, "label": {"kingside": "Castled kingside", "queenside": "Castled queenside", "none": "Never castled"}[side],
             "avg_move": _avg(castle_move[side]) if side != "none" else None, **_record(castle[side])}
            for side in ("kingside", "queenside", "none")
        ],
    }


def _calendar(games: list[dict], tz_offset: int) -> dict:
    """Weekday / hour breakdowns in the viewer's timezone. `tz_offset` is
    JavaScript's Date.getTimezoneOffset() (minutes to ADD to local time to get
    UTC), so local = UTC - offset; games are stored in UTC."""
    shift = timedelta(minutes=tz_offset)
    weekday: dict[int, list[dict]] = defaultdict(list)
    hour: dict[int, list[dict]] = defaultdict(list)
    days: dict[str, list[dict]] = defaultdict(list)
    heat = [[0] * 24 for _ in range(7)]
    for g in games:
        if not g["dt"]:
            continue
        local = g["dt"] - shift
        weekday[local.weekday()].append(g)
        hour[local.hour].append(g)
        days[local.date().isoformat()].append(g)
        heat[local.weekday()][local.hour] += 1

    def row(picked: list[dict]) -> dict:
        return {**_record(picked), "accuracy": _avg([g["acc"] for g in picked if g["acc"] is not None])}

    return {
        "weekday": [{"day": WEEKDAYS[i], **row(weekday.get(i, []))} for i in range(7)],
        "hour": [{"hour": h, **row(hour.get(h, []))} for h in range(24)],
        "heatmap": heat,
        "days": [{"date": d, **_record(v)} for d, v in sorted(days.items())],
    }


def _by_time_control(games: list[dict]) -> tuple[dict, dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for g in games:
        groups[g["time_control"]].append(g)
    accuracy = {tc: {"games": len(v), "avg_accuracy": _avg([g["acc"] for g in v if g["acc"] is not None])} for tc, v in groups.items()}
    acpl = {tc: {"games": len(v), "avg_acpl": _avg([g["acpl"] for g in v if g["acpl"] is not None])} for tc, v in groups.items()}
    return accuracy, acpl


# --- entry points -----------------------------------------------------------------------------


def build_report(flt: Filters, tz_offset: int = 0) -> dict:
    """Every Insights section for the filtered games, in one payload."""
    game_meta.ensure_metadata()
    games = _load(flt)
    gran = _granularity(games)
    accuracy_tc, acpl_tc = _by_time_control(games)
    src, pid = flt.source, flt.profile_id
    return {
        "filters": {"source": src, "profile_id": pid, "time_class": flt.time_class, "color": flt.color,
                    "date_from": flt.date_from, "date_to": flt.date_to},
        "coverage": {"games": len(games), "reviewed": sum(1 for g in games if g["reviewed"]),
                     "pending": sum(1 for g in games if not g["reviewed"])},
        "overview": _overview(games, gran),
        "results": _results(games),
        "shapes": _shapes(games),
        "phases": _phases(games),
        "openings": _openings(games),
        "tactics": _tactics(games, flt),
        "moves": _moves(games, gran),
        "calendar": _calendar(games, tz_offset),
        "legacy": {
            "rating_progress": insights.rating_progress(src, pid, flt),
            "win_rate_by_color": insights.win_rate_by_color(src, pid, flt),
            "win_rate_by_time_control": insights.win_rate_by_time_control(src, pid, flt),
            "accuracy_by_time_control": accuracy_tc,
            "acpl_by_time_control": acpl_tc,
            "performance_rating_by_time_control": insights.performance_rating_by_time_control(src, pid, flt),
            "avg_game_length": insights.avg_game_length_wins_vs_losses(src, pid, flt),
            "performance_vs_rating_band": insights.performance_vs_rating_band(src, pid, flt),
            "comeback_rate": insights.comeback_rate(src, pid, flt=flt),
        },
        "top_insights": insights.top_insights(src, pid, flt=flt),
    }


def tactic_examples(flt: Filters, motif: str, side: str, outcome: str, limit: int = 4) -> list[dict]:
    """Example positions for one tactic bucket (e.g. your missed forks): the
    position before the move, what the engine wanted, and what was played —
    biggest material swings first."""
    where, params = insights._source_clause(flt.source, flt.profile_id, flt=flt)
    mine = 1 if side == "you" else 0
    conn = get_connection()
    try:
        rows = conn.execute(
            f"SELECT gt.game_id, gt.ply, gt.gain, gt.best_uci, gt.played_uci, g.pgn, g.opponent, g.date, g.color "
            f"FROM game_tactics gt JOIN games g ON g.id = gt.game_id "
            f"WHERE gt.motif = ? AND gt.outcome = ? AND (gt.color = g.color) = ? AND g.analyzed = 1 AND g.skip_reason IS NULL {where} "
            f"ORDER BY gt.gain DESC, g.date DESC LIMIT ?", (motif, outcome, mine, *params, limit)).fetchall()
    finally:
        conn.close()

    examples = []
    for r in rows:
        game = chess.pgn.read_game(io.StringIO(r["pgn"]))
        if game is None:
            continue
        board = game.board()
        for i, move in enumerate(game.mainline_moves(), start=1):
            if i == r["ply"]:
                break
            board.push(move)
        fen_before = board.fen()

        def san(uci: str | None) -> str | None:
            try:
                return board.san(chess.Move.from_uci(uci)) if uci else None
            except ValueError:
                return None

        examples.append({
            "game_id": r["game_id"], "ply": r["ply"], "fen": fen_before, "turn": "white" if board.turn == chess.WHITE else "black",
            "best_uci": r["best_uci"], "best_san": san(r["best_uci"]), "played_uci": r["played_uci"], "played_san": san(r["played_uci"]),
            "gain": r["gain"], "opponent": r["opponent"], "date": r["date"], "you_are": r["color"],
        })
    return examples

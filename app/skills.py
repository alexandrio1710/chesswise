"""
Skills & plan: where you actually lose points, and what to do about it.

Two ideas from how chess coaches (and tools like Aimchess) frame improvement,
built only from data this app already stores:

  * Skills — six measures of how you play (opening, tactics, endgames,
    converting advantages, defending worse positions, time management). Each
    is shown with its sample size, because a figure over a handful of games is
    noise. There is deliberately no single overall score and no comparison to
    "players at your rating": that population data doesn't exist here, and an
    invented benchmark would be worse than none.
  * Mistake anatomy — every mistake and blunder of yours gets one primary
    cause (a piece left hanging, a tactic missed or walked into, time trouble,
    an opening/endgame error, or plain middlegame judgment), so you can see
    *why* you lose games rather than only that you do. The causes are
    heuristics from the engine's best moves and simple board facts, not a
    verdict on what you were thinking.

The plan ranks the causes of the mistakes that decided your lost and drawn
games by the points they cost (a loss is a point, a draw half), so the top item
is the one that would pay back the most.
"""

from __future__ import annotations

import io
from collections import Counter, defaultdict

import chess
import chess.pgn

import insights_report
from db import get_connection
from insights import Filters

BAD_CLASSES = ("mistake", "blunder", "miss")
TACTIC_MOTIFS = ("fork", "pin", "skewer", "discovered_attack")

ADVANTAGE_CP = 300          # "clearly winning" / "clearly losing", from your side
ADVANTAGE_PLIES = 2         # …held for this many consecutive plies, so one blip doesn't count
THROWN_AWAY_CP = 150        # a move of yours that gives back at least this much
ENDGAME_EDGE_CP = 150       # entering the endgame at least this much better
LOW_CLOCK_SHARE = 0.10      # "time trouble": under 10% of the starting clock…
LOW_CLOCK_FLOOR = 10        # …or under this many seconds
LOW_SAMPLE = 20             # fewer games than this and a figure is flagged as noisy
MIN_OPENING_GAMES = 5
DECISIVE_LOSS_CP = 100      # a mistake must drop at least this much to count as "the" mistake of a game

CAUSES = {
    "hung_piece": {
        "label": "Left a piece hanging",
        "blurb": "A piece your move left en prise, and it was taken.",
        "habit": "Before you commit, ask what the move leaves undefended — then scan checks, captures and threats.",
        "theme": "hanging_piece",
    },
    "allowed_tactic": {
        "label": "Walked into a tactic",
        "blurb": "Your move allowed a fork, pin, skewer, discovered attack or mate on the very next move.",
        "habit": "After every opponent move, say what it changed and what it now threatens; do the same for your own move's replies.",
        "theme": "fork",
    },
    "missed_mate": {
        "label": "Missed a forced mate",
        "blurb": "A forced mate was available and you played something else.",
        "habit": "In any sharp position look at every check first — mates hide in forcing lines.",
        "theme": "mate",
    },
    "missed_tactic": {
        "label": "Missed a tactic",
        "blurb": "The engine's best move was a fork, pin, skewer or discovered attack that you didn't play.",
        "habit": "Look for forcing moves first (checks, captures, threats) before choosing a quiet plan.",
        "theme": "fork",
    },
    "missed_free_piece": {
        "label": "Ignored a free piece",
        "blurb": "Your opponent left a piece capturable for nothing and you played something else.",
        "habit": "Each move, glance at what the opponent's last move left loose before you follow your own plan.",
        "theme": "hanging_piece",
    },
    "time_trouble": {
        "label": "Time trouble",
        "blurb": "Made with under a tenth of the starting clock left.",
        "habit": "Budget time by move number, and use a shortened scan when short on time: checks, captures, threats, loose pieces.",
        "theme": None,
    },
    "opening": {
        "label": "Opening error",
        "blurb": "A mistake in the opening phase, before the position had settled.",
        "habit": "Ask of each opening move whether it develops, contests the centre or answers a direct threat.",
        "theme": None,
    },
    "endgame": {
        "label": "Endgame error",
        "blurb": "A mistake once the game had simplified into an endgame.",
        "habit": "Count candidate moves, activate the king, and avoid needless pawn weaknesses.",
        "theme": None,
    },
    "judgment": {
        "label": "Middlegame judgment",
        "blurb": "A mistake with no tactical cause the app can name — a poor plan, trade or piece placement.",
        "habit": "Slow down at the moment the position changes character, and compare two candidate plans before moving.",
        "theme": None,
    },
}


# --- helpers ---------------------------------------------------------------------------------


def _pct(part: int, whole: int) -> float | None:
    return round(part / whole * 100, 1) if whole else None


def _avg(values: list[float], digits: int = 1) -> float | None:
    return round(sum(values) / len(values), digits) if values else None


def _score(result: str) -> float:
    return 1.0 if result == "win" else 0.5 if result == "draw" else 0.0


def _mine(g: dict, m: dict) -> bool:
    return m["color_moved"] == g["color"]


def _your_eval(g: dict, m: dict) -> float:
    """Evaluation after this ply, from your side (positive = you're better)."""
    return m["eval_cp"] if g["color"] == "white" else -m["eval_cp"]


def _sustained_from(g: dict, threshold: float, sign: int = 1) -> int | None:
    """Index into g['moves'] where the first run of ADVANTAGE_PLIES consecutive
    plies at or beyond `threshold` (from your side; sign=-1 for being worse)
    begins, or None."""
    run = 0
    for i, m in enumerate(g["moves"]):
        if m["eval_cp"] is not None and sign * _your_eval(g, m) >= threshold:
            run += 1
            if run >= ADVANTAGE_PLIES:
                return i - ADVANTAGE_PLIES + 1
        else:
            run = 0
    return None


def _class_of(m: dict) -> str | None:
    return m["classification"] or m["tier"]


def _low_clock(g: dict, m: dict) -> bool:
    base = g.get("base_seconds")
    clock = m.get("clock_seconds_remaining")
    return bool(base) and clock is not None and clock <= max(LOW_CLOCK_FLOOR, LOW_CLOCK_SHARE * base)


def _load_tactics(game_ids: list[int]) -> dict[tuple[int, int, str], list[tuple[str, str]]]:
    """{(game_id, ply, color): [(motif, outcome), ...]} for the games."""
    events: dict[tuple[int, int, str], list[tuple[str, str]]] = defaultdict(list)
    if not game_ids:
        return events
    conn = get_connection()
    try:
        for i in range(0, len(game_ids), 400):
            chunk = game_ids[i:i + 400]
            marks = ",".join("?" * len(chunk))
            for r in conn.execute(f"SELECT game_id, ply, color, motif, outcome FROM game_tactics WHERE game_id IN ({marks})", chunk):
                events[(r["game_id"], r["ply"], r["color"])].append((r["motif"], r["outcome"]))
    finally:
        conn.close()
    return events


# --- mistake anatomy --------------------------------------------------------------------------


def classify_mistake(g: dict, m: dict, events: dict) -> str:
    """The primary cause of one of your mistakes (first match wins, in the order
    that best names what happened)."""
    mine = events.get((g["id"], m["ply"], g["color"]), [])
    opponent = "black" if g["color"] == "white" else "white"
    theirs = events.get((g["id"], m["ply"] + 1, opponent), [])
    if ("left_hanging", "punished") in mine:
        return "hung_piece"
    if any(motif in TACTIC_MOTIFS + ("mate",) and outcome == "found" for motif, outcome in theirs):
        return "allowed_tactic"
    if ("mate", "missed") in mine:
        return "missed_mate"
    if any(motif in TACTIC_MOTIFS and outcome == "missed" for motif, outcome in mine):
        return "missed_tactic"
    if ("free_piece", "ignored") in mine:
        return "missed_free_piece"
    if _low_clock(g, m):
        return "time_trouble"
    if m["phase"] == "opening":
        return "opening"
    if m["phase"] == "endgame":
        return "endgame"
    return "judgment"


def _mistakes(games: list[dict], events: dict) -> list[dict]:
    out = []
    for g in games:
        if not g["reviewed"]:
            continue
        for m in g["moves"]:
            if _mine(g, m) and _class_of(m) in BAD_CLASSES:
                out.append({"game": g, "move": m, "cause": classify_mistake(g, m, events),
                            "drop": m["eval_drop"] or 0})
    return out


def _anatomy(games: list[dict], mistakes: list[dict]) -> dict:
    counts = Counter(x["cause"] for x in mistakes)
    total = sum(counts.values())
    reviewed = [g for g in games if g["reviewed"]]

    # The mistake that decided each lost or drawn game: your single biggest drop, if it was a real one.
    decisive = Counter()
    lost_or_drawn = [g for g in reviewed if g["result"] in ("loss", "draw")]
    by_game: dict[int, dict] = {}
    for x in mistakes:
        best = by_game.get(x["game"]["id"])
        if best is None or x["drop"] > best["drop"]:
            by_game[x["game"]["id"]] = x
    points = Counter()
    for g in lost_or_drawn:
        top = by_game.get(g["id"])
        if top and top["drop"] >= DECISIVE_LOSS_CP:
            decisive[top["cause"]] += 1
            points[top["cause"]] += 1 - _score(g["result"])

    def row(key: str) -> dict:
        return {"key": key, "label": CAUSES[key]["label"], "blurb": CAUSES[key]["blurb"], "habit": CAUSES[key]["habit"], "theme": CAUSES[key]["theme"]}

    return {
        "total": total,
        "causes": [{**row(k), "count": counts.get(k, 0), "pct": _pct(counts.get(k, 0), total)} for k in CAUSES if counts.get(k, 0)],
        "decisive": {
            "games": sum(decisive.values()), "lost_or_drawn": len(lost_or_drawn),
            "causes": [{**row(k), "games": n, "points_lost": round(points[k], 1),
                        "per_100_games": round(points[k] / len(reviewed) * 100, 1) if reviewed else None}
                       for k, n in decisive.most_common()],
        },
    }


# --- skills -----------------------------------------------------------------------------------


def _skill(key: str, label: str, value: float | None, unit: str, n: int, detail: str, blurb: str) -> dict:
    return {"key": key, "label": label, "value": value, "unit": unit, "n": n, "detail": detail, "blurb": blurb,
            "low_sample": n < LOW_SAMPLE}


def _opening_skill(games: list[dict]) -> dict:
    accs, evals = [], []
    for g in games:
        book = g["book_plies"]
        if book is None:
            continue
        theirs = [g["maccs"].get(m["ply"]) for m in g["moves"] if _mine(g, m) and book < m["ply"] <= 24]
        theirs = [a for a in theirs if a is not None]
        if len(theirs) >= 3:
            accs.append(sum(theirs) / len(theirs))
        at20 = next((m for m in g["moves"] if m["ply"] == 20), None)
        if at20 and at20["eval_cp"] is not None:
            evals.append(_your_eval(g, at20))
    detail = f"Average position after move 10: {'+' if (_avg(evals) or 0) > 0 else ''}{round(_avg(evals) or 0)} cp for you." if evals else "Not enough games reach move 10."
    return _skill("opening", "Opening", _avg(accs), "% accuracy after theory", len(accs), detail,
                  "How precisely you play once the memorized moves run out (moves after your book line, up to move 12).")


def _skills(games: list[dict], events: dict) -> list[dict]:
    reviewed = [g for g in games if g["reviewed"]]
    by_id = {g["id"]: g for g in games}

    # Tactics: found vs missed, and free pieces taken vs ignored — your side only.
    found = missed = taken = ignored = 0
    for (gid, ply, color), evs in events.items():
        g = by_id.get(gid)
        if g is None or color != g["color"]:
            continue
        for motif, outcome in evs:
            if motif in TACTIC_MOTIFS + ("mate",):
                found += outcome == "found"
                missed += outcome == "missed"
            elif motif == "free_piece":
                taken += outcome == "taken"
                ignored += outcome == "ignored"
    tactics = _skill("tactics", "Tactics", _pct(found, found + missed), "% of tactics found", len(reviewed),
                     f"{found} found, {missed} missed (forks, pins, skewers, discovered attacks, mates); free pieces taken {taken} of {taken + ignored}.",
                     "How often you play the tactic the engine wanted when one was on.")

    # Endgames: accuracy there, and converting a clear edge into a win.
    end_accs, other_accs, edge_games = [], [], []
    for g in games:
        end = [g["maccs"].get(m["ply"]) for m in g["moves"] if _mine(g, m) and m["phase"] == "endgame"]
        rest = [g["maccs"].get(m["ply"]) for m in g["moves"] if _mine(g, m) and m["phase"] != "endgame"]
        end = [a for a in end if a is not None]
        rest = [a for a in rest if a is not None]
        if len(end) >= 3:
            end_accs.append(sum(end) / len(end))
            first_end = next((m for m in g["moves"] if m["phase"] == "endgame"), None)
            if first_end and first_end["eval_cp"] is not None and _your_eval(g, first_end) >= ENDGAME_EDGE_CP:
                edge_games.append(g)
        if rest:
            other_accs.append(sum(rest) / len(rest))
    converted = sum(1 for g in edge_games if g["result"] == "win")
    end_detail = (f"Entered the endgame clearly better in {len(edge_games)} games and won {converted} of them. "
                  if edge_games else "") + (f"Your other phases average {_avg(other_accs)}% accuracy." if other_accs else "")
    endgames = _skill("endgames", "Endgames", _avg(end_accs), "% accuracy", len(end_accs), end_detail.strip(),
                      "Your accuracy once the game has simplified, and whether a clear edge becomes a win.")

    # Advantage capitalization / resourcefulness: games where you were clearly better / worse for a sustained stretch.
    better = [g for g in games if _sustained_from(g, ADVANTAGE_CP) is not None]
    won = sum(1 for g in better if g["result"] == "win")
    blown_points = sum(1 - _score(g["result"]) for g in better)
    advantage = _skill("advantage", "Converting advantages", _pct(won, len(better)), "% of winning positions won", len(better),
                       f"{len(better) - won} of {len(better)} clearly-winning positions (≥ +3.0 held for two moves) were not won — {round(blown_points, 1)} points given back.",
                       "How often a clearly winning position ends in a win.")
    worse = [g for g in games if _sustained_from(g, ADVANTAGE_CP, sign=-1) is not None]
    saved_points = sum(_score(g["result"]) for g in worse)
    saved = sum(1 for g in worse if g["result"] in ("win", "draw"))
    resource = _skill("resourcefulness", "Defending worse positions", _pct(saved, len(worse)), "% of bad positions saved", len(worse),
                      f"Saved {saved} of {len(worse)} clearly-losing positions (≤ −3.0 held for two moves), earning {round(saved_points, 1)} points.",
                      "How often a clearly losing position becomes a draw or a win.")

    # Time management.
    timed = [g for g in games if g.get("base_seconds") and any(m.get("clock_seconds_remaining") is not None for m in g["moves"])]
    low = normal = 0
    low_accs, normal_accs = [], []
    for g in timed:
        for m in g["moves"]:
            if not _mine(g, m) or m.get("clock_seconds_remaining") is None:
                continue
            acc = g["maccs"].get(m["ply"])
            if _low_clock(g, m):
                low += 1
                if acc is not None:
                    low_accs.append(acc)
            else:
                normal += 1
                if acc is not None:
                    normal_accs.append(acc)
    flag_losses = sum(1 for g in timed if g["result"] == "loss" and g["termination_method"] == "timeout")
    time_detail = (f"Under time pressure your accuracy is {_avg(low_accs)}% versus {_avg(normal_accs)}% otherwise; " if low_accs and normal_accs else "") + \
                  f"{flag_losses} of {sum(1 for g in timed if g['result'] == 'loss')} losses were on time."
    time_skill = _skill("time", "Time management", _pct(low, low + normal), "% of moves in time trouble", len(timed), time_detail.strip(),
                        "The share of your moves played with under a tenth of the starting clock left (lower is better).")

    return [_opening_skill(games), tactics, endgames, advantage, resource, time_skill]


# --- opening leaks ---------------------------------------------------------------------------------


def _opening_leaks(games: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for g in games:
        groups[(g["color"], insights_report._family(g["opening_name"]))].append(g)
    rows = []
    for (color, family), picked in groups.items():
        if len(picked) < MIN_OPENING_GAMES:
            continue
        evals = []
        for g in picked:
            at20 = next((m for m in g["moves"] if m["ply"] == 20), None)
            if at20 and at20["eval_cp"] is not None:
                evals.append(_your_eval(g, at20))
        if len(evals) < MIN_OPENING_GAMES:
            continue
        rows.append({"opening": family, "color": color, "games": len(picked),
                     "avg_eval_move10": round(sum(evals) / len(evals)),
                     "win_rate_pct": _pct(sum(1 for g in picked if g["result"] == "win"), len(picked)),
                     "accuracy": _avg([g["acc"] for g in picked if g["acc"] is not None])})
    rows.sort(key=lambda r: r["avg_eval_move10"])
    return rows


# --- positions to play out --------------------------------------------------------------------------


def _fen_before(pgn_text: str, ply: int) -> str | None:
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return None
    board = game.board()
    for i, move in enumerate(game.mainline_moves(), start=1):
        if i == ply:
            return board.fen()
        board.push(move)
    return None


def _convert_candidates(games: list[dict]) -> list[dict]:
    """Games where a clearly winning position slipped: the position just before
    the first move that gave a big chunk of it back — the one to play out again."""
    out = []
    for g in games:
        start = _sustained_from(g, ADVANTAGE_CP)
        if start is None or g["result"] == "win":
            continue
        for m in g["moves"][start:]:
            if _mine(g, m) and (m["eval_drop"] or 0) >= THROWN_AWAY_CP:
                before = m["eval_before_cp"]
                out.append({"game_id": g["id"], "ply": m["ply"], "color": g["color"], "opponent": g["opponent"], "date": g["date"],
                            "eval_before": before, "eval_drop": m["eval_drop"], "result": g["result"]})
                break
    out.sort(key=lambda r: (r["date"] or ""), reverse=True)
    return out


def _with_fens(candidates: list[dict]) -> list[dict]:
    if not candidates:
        return []
    conn = get_connection()
    try:
        marks = ",".join("?" * len(candidates))
        pgns = {r["id"]: r["pgn"] for r in conn.execute(f"SELECT id, pgn FROM games WHERE id IN ({marks})", [c["game_id"] for c in candidates])}
    finally:
        conn.close()
    out = []
    for c in candidates:
        fen = _fen_before(pgns.get(c["game_id"], ""), c["ply"])
        if fen:
            out.append({**c, "fen": fen})
    return out


# --- the plan ----------------------------------------------------------------------------------------


def _actions(key: str, has_convert: bool) -> list[dict]:
    theme = CAUSES[key]["theme"]
    acts = []
    if theme:
        acts.append({"label": f"Train {theme.replace('_', ' ')} puzzles", "href": f"/puzzles?theme={theme}"})
    if key == "time_trouble":
        acts.append({"label": "See your clock usage", "href": "/clock"})
    if key == "opening":
        acts.append({"label": "Open the opening explorer", "href": "/explorer"})
        acts.append({"label": "Train opening puzzles", "href": "/puzzles?phase=opening"})
    if key == "endgame":
        acts.append({"label": "Endgame trainer", "href": "/endgame"})
        acts.append({"label": "Train endgame puzzles", "href": "/puzzles?phase=endgame"})
    if key in ("judgment", "hung_piece", "allowed_tactic", "missed_tactic") and has_convert:
        acts.append({"label": "Play out winning positions you let slip", "href": "#convert"})
    if key == "judgment":
        acts.append({"label": "Practice mistakes from your games", "href": "/puzzles?phase=middlegame"})
    return acts


def _plan(anatomy: dict, skills: list[dict], has_convert: bool, reviewed: int) -> list[dict]:
    plan = []
    for c in anatomy["decisive"]["causes"][:3]:
        plan.append({
            "key": c["key"], "title": c["label"],
            "why": f"It was the deciding mistake in {c['games']} of your lost or drawn games — about {c['per_100_games']} points lost per 100 games.",
            "habit": c["habit"], "points_lost": c["points_lost"], "actions": _actions(c["key"], has_convert),
            "low_sample": reviewed < LOW_SAMPLE,
        })
    by_key = {s["key"]: s for s in skills}
    adv = by_key["advantage"]
    if adv["value"] is not None and adv["n"] >= 5 and adv["value"] < 85 and has_convert:
        plan.append({
            "key": "convert", "title": "Convert your winning positions",
            "why": f"{adv['detail']}",
            "habit": "When ahead, trade pieces, remove counterplay and check what each move allows before pressing on.",
            "points_lost": None, "actions": [{"label": "Play out positions you let slip", "href": "#convert"}], "low_sample": adv["low_sample"],
        })
    return plan[:4]


# --- entry point --------------------------------------------------------------------------------------


def build(flt: Filters) -> dict:
    """The whole Skills & plan payload for the filtered games."""
    games = insights_report._load(flt)
    reviewed = [g for g in games if g["reviewed"]]
    events = _load_tactics([g["id"] for g in reviewed])
    mistakes = _mistakes(games, events)
    anatomy = _anatomy(games, mistakes)
    skills = _skills(games, events)
    convert = _with_fens(_convert_candidates(games)[:6])
    return {
        "filters": {"source": flt.source, "profile_id": flt.profile_id, "time_class": flt.time_class, "color": flt.color,
                    "date_from": flt.date_from, "date_to": flt.date_to},
        "coverage": {"games": len(games), "reviewed": len(reviewed), "pending": len(games) - len(reviewed)},
        "skills": skills,
        "anatomy": anatomy,
        "plan": _plan(anatomy, skills, bool(convert), len(reviewed)),
        "convert": convert,
        "openings": _opening_leaks(games)[:10],
        "notes": {
            "low_sample_games": LOW_SAMPLE,
            "thresholds": {"advantage_cp": ADVANTAGE_CP, "thrown_away_cp": THROWN_AWAY_CP, "endgame_edge_cp": ENDGAME_EDGE_CP,
                           "time_trouble": f"under {int(LOW_CLOCK_SHARE * 100)}% of the starting clock (or {LOW_CLOCK_FLOOR}s)"},
        },
    }


def mistake_examples(flt: Filters, cause: str, limit: int = 6) -> list[dict]:
    """Example positions for one cause: the position before the mistake, what was
    played and what the engine wanted — biggest drops first."""
    games = insights_report._load(flt)
    reviewed = [g for g in games if g["reviewed"]]
    mistakes = [x for x in _mistakes(games, _load_tactics([g["id"] for g in reviewed])) if x["cause"] == cause]
    mistakes.sort(key=lambda x: -x["drop"])
    picked = mistakes[:limit]
    if not picked:
        return []
    conn = get_connection()
    try:
        marks = ",".join("?" * len(picked))
        pgns = {r["id"]: r["pgn"] for r in conn.execute(f"SELECT id, pgn FROM games WHERE id IN ({marks})", [x["game"]["id"] for x in picked])}
        best = {(r["game_id"], r["ply"]): r["best_move_uci"] for r in conn.execute(
            f"SELECT game_id, ply, best_move_uci FROM game_moves WHERE game_id IN ({marks})", [x["game"]["id"] for x in picked])}
    finally:
        conn.close()
    out = []
    for x in picked:
        g, m = x["game"], x["move"]
        game = chess.pgn.read_game(io.StringIO(pgns.get(g["id"], "")))
        if game is None:
            continue
        board = game.board()
        played = None
        for i, move in enumerate(game.mainline_moves(), start=1):
            if i == m["ply"]:
                played = move
                break
            board.push(move)
        if played is None:
            continue
        best_uci = best.get((g["id"], m["ply"]))
        try:
            best_move = chess.Move.from_uci(best_uci) if best_uci else None
            best_san = board.san(best_move) if best_move and best_move in board.legal_moves else None
        except ValueError:
            best_san = None
        out.append({"game_id": g["id"], "ply": m["ply"], "fen": board.fen(), "turn": "white" if board.turn == chess.WHITE else "black",
                    "played_uci": played.uci(), "played_san": board.san(played), "best_uci": best_uci if best_san else None, "best_san": best_san,
                    "drop": round(x["drop"]), "opponent": g["opponent"], "date": g["date"], "color": g["color"]})
    return out

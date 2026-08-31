"""
Coaching Report — conversational chat coach.

A coaching report is a fixed document; a real coach answers follow-up
questions. This module lets a solver ask about a specific generated report
— "why does the tempo count matter in this Dragon game," "which of these
should I actually focus on this week" — grounded in that report's own
computed data (headline, time-control/structure stats, the highlighted
drift positions with their real engine lines, the repertoire checklists),
not a general chess-advice chatbot reasoning freely.

That grounding matters for the same reason every other heuristic in this
project is documented plainly rather than oversold (see game_report.py's
and repertoire.py's own module comments): an LLM confidently asserting
chess judgment untethered from the actual engine analysis would be worse
than not having this feature at all. The system prompt below explicitly
instructs the model to reason from the supplied data and say so when a
question goes beyond what that data can support — the same "worth
reviewing, not a verdict" framing coaching_report.py itself uses.

Talks to a locally-run Ollama (https://ollama.com) over plain HTTP, not a
paid API — free forever, no API key, runs entirely on this machine, using
the `requests` library this project already depends on (no new package).
Offered only when Ollama is actually reachable right now
(is_configured() — a live check, not just "is a URL configured").
"""

import json
import logging

import requests

from config import OLLAMA_BASE_URL, OLLAMA_CHAT_TIMEOUT_SECONDS, OLLAMA_MODEL

logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 20  # a generous conversation length cap, not a hard product limit

# Short — this is a local reachability probe checked once per page load,
# not the actual (much slower) chat call, so it should fail fast if Ollama
# just isn't running rather than making the page wait.
_AVAILABILITY_CHECK_TIMEOUT_SECONDS = 1.5


def is_configured() -> bool:
    """True only if Ollama is actually reachable right now — installed but
    not currently running is exactly the case a static "is a URL set"
    check would get wrong, unlike a hosted API where the key alone is
    enough to know the feature will work.
    """
    try:
        resp = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=_AVAILABILITY_CHECK_TIMEOUT_SECONDS)
        return resp.ok
    except requests.exceptions.RequestException:
        return False


def _report_context(report: dict) -> str:
    """Compact JSON summary of the report handed to Claude as grounding —
    the full report minus fields that would just be noise for a chat
    coach (raw multipv lines beyond what's already in close_alternatives,
    internal ids). Kept as real JSON, not prose, so the model can quote
    exact numbers back accurately rather than paraphrasing from memory.
    """
    positions = [
        {
            "game_id": p["game_id"],
            "fen_before": p["fen_before"],
            "played_move": p["played_move_san"],
            "engine_top_choice": p["best_move_san"],
            "close_alternatives": [
                {"move": line["move_san"], "eval_cp": line["eval_cp"], "mate_in": line["mate_in"]}
                for line in p["close_alternatives"]
            ],
        }
        for p in report.get("highlighted_positions", [])
    ]
    context = {
        "headline": report.get("headline"),
        "game_count": report.get("game_count"),
        "time_control_stats": report.get("time_control_stats"),
        "repertoire_structure_stats": report.get("structure_stats"),
        "highlighted_drift_positions": positions,
        "repertoire_checklist_items": report.get("what_to_work_on"),
        "trend_vs_previous_batch": report.get("trend_note"),
    }
    return json.dumps(context, indent=2)


def _system_prompt(report: dict) -> str:
    return (
        "You are discussing one specific chess coaching report with the player it was "
        "generated for. The report data (JSON) is below — ground every answer in it. "
        "Quote the actual numbers, moves, and FENs from this data rather than inventing "
        "plausible-sounding ones.\n\n"
        "What this report's 'drift candidate' flags mean, and what they don't: a flagged "
        "move was NOT objectively bad (it wasn't a mistake or blunder) — it's a position "
        "where several engine moves were close in evaluation and the player didn't pick "
        "the top one. That is worth reviewing, not proof of a wrong decision — an engine's "
        "centipawn evaluation can't determine whether a plan was actually incorrect, only "
        "that a different move scored higher in this specific search. Talk about these as "
        "'worth looking at' or 'worth understanding why,' never as confirmed errors.\n\n"
        "The repertoire-structure tags (e.g. 'dragon_opposite_castling', 'london_iqp') come "
        "from hand-written pattern detectors on piece placement, not engine judgment about "
        "whether the structure was handled well. Treat the attached checklist items as "
        "background on known trouble spots in this structure, not a diagnosis of this "
        "specific game.\n\n"
        "If the player asks something this data genuinely can't answer (e.g. deep opening "
        "theory this report didn't analyze, or a definitive verdict on whether a plan was "
        "'correct'), say so plainly rather than guessing with false confidence — that's the "
        "standard this whole tool holds itself to elsewhere.\n\n"
        "Keep answers conversational and concise — a few sentences to a short paragraph, "
        "like a coach talking, not a report.\n\n"
        f"Report data:\n{_report_context(report)}"
    )


def send_message(report: dict, history: list[dict], user_message: str) -> str:
    """One turn of the coaching chat against a local Ollama server.
    `history` is [{"role": "user"|"assistant", "content": str}, ...] from
    earlier in this conversation (client-supplied — this endpoint is
    stateless, matching Ollama's own /api/chat being stateless). Raises
    RuntimeError with a plain message on any failure the caller should
    surface to the user; never a raw requests exception.
    """
    if not is_configured():
        raise RuntimeError(
            f"Ollama isn't reachable at {OLLAMA_BASE_URL} — make sure it's installed and running "
            "(https://ollama.com), and that a model is pulled (`ollama pull " + OLLAMA_MODEL + "`)."
        )

    trimmed_history = history[-MAX_HISTORY_MESSAGES:]
    messages = (
        [{"role": "system", "content": _system_prompt(report)}]
        + trimmed_history
        + [{"role": "user", "content": user_message}]
    )

    try:
        resp = requests.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json={"model": OLLAMA_MODEL, "messages": messages, "stream": False},
            timeout=OLLAMA_CHAT_TIMEOUT_SECONDS,
        )
    except requests.exceptions.Timeout as e:
        logger.warning(f"Ollama chat request timed out: {e}")
        raise RuntimeError(
            f"Ollama didn't respond within {OLLAMA_CHAT_TIMEOUT_SECONDS:.0f}s — "
            f"{OLLAMA_MODEL} may be too large for this machine, or it's still loading."
        ) from e
    except requests.exceptions.RequestException as e:
        logger.warning(f"Ollama unreachable: {e}")
        raise RuntimeError(f"Couldn't reach Ollama at {OLLAMA_BASE_URL} — is it running?") from e

    if resp.status_code == 404:
        raise RuntimeError(
            f"Ollama doesn't have model '{OLLAMA_MODEL}' pulled — run `ollama pull {OLLAMA_MODEL}` first."
        )
    if not resp.ok:
        logger.error(f"Ollama API error ({resp.status_code}): {resp.text}")
        raise RuntimeError(f"Ollama returned an error ({resp.status_code}).")

    data = resp.json()
    reply = data.get("message", {}).get("content", "")
    if not reply:
        raise RuntimeError("Ollama didn't return a text reply — try rephrasing your question.")
    return reply

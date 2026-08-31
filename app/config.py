"""
Shared configuration — the one place environment-specific settings live.

Loads a `.env` file (if present) so machine-specific values (Stockfish
path, DB location, Discord webhook, API tuning) don't need to be hardcoded
or re-exported in every shell session. Every other module reads settings
from here rather than calling os.environ.get() directly, so there's one
place to look when tracing down where a value came from.

See .env.example for the full list of recognized variables.
"""

import glob
import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

# Loaded once, here, before anything below reads os.environ — every other
# module gets this for free by importing from config instead of calling
# load_dotenv() itself. Does nothing (no error) if there's no .env file,
# which is the normal case for CI or a machine using real env vars instead.
load_dotenv()


# --- Database ---------------------------------------------------------------

DB_PATH = Path(os.environ.get("DB_PATH", str(Path(__file__).parent.parent / "chess_tracker.db")))


# --- Stockfish ---------------------------------------------------------------

# Analysis depth: a speed/accuracy tradeoff. 14-16 gives reasonably strong
# evaluations in roughly 0.1-0.5s per position on modern hardware, so a
# ~40-move game analyzes in well under a minute. Overridable so you can
# trade accuracy for speed (a quick sanity-check pass) or vice versa
# (a slower, more thorough one) without editing code.
STOCKFISH_DEPTH = int(os.environ.get("STOCKFISH_DEPTH", "15"))

# Puzzle generation (Stage B) searches a bit deeper than routine per-move
# analysis, since the "correct answer" it hands back needs to hold up —
# and it only runs once per flagged mistake/blunder, not once per move.
PUZZLE_DEPTH = int(os.environ.get("PUZZLE_DEPTH", "18"))
PUZZLE_TOP_LINES = int(os.environ.get("PUZZLE_TOP_LINES", "3"))

# A pure depth target has no wall-clock ceiling: search extensions in an
# unusually sharp position can occasionally make one position take far
# longer than the ~0.1-0.5s typical case above, stalling a whole batch
# run behind it. Lichess's own puzzle generator guards the same way —
# combined depth/time/nodes limits, whichever is hit first
# (https://github.com/ornicar/lichess-puzzler/blob/master/generator/generator.py,
# `pair_limit`) — so this is a rare safety net, not a normal-case
# constraint: every routine position finishes well under it.
STOCKFISH_MAX_SECONDS_PER_POSITION = float(os.environ.get("STOCKFISH_MAX_SECONDS_PER_POSITION", "10"))

# Coaching report (coaching_report.py) — a dedicated MultiPV pass, kept
# out of routine per-move analysis the same way game_report.py's own
# MultiPV=2 enrichment already is (see that module's docstring): worth
# paying for on-demand batch review, not for every synced game forever.
COACHING_REPORT_DEPTH = int(os.environ.get("COACHING_REPORT_DEPTH", "16"))
COACHING_REPORT_MULTIPV = int(os.environ.get("COACHING_REPORT_MULTIPV", "4"))

# A "close decision" position: at least 2 of the top-N engine lines are
# within this many centipawns of the best one — the threshold where plan
# quality, not calculation, actually decides the choice. A first
# calibrated guess (per the user's own suggested 15-25cp range), meant to
# be tuned after checking flagged positions against real games feel like
# real turning points (see coaching_report.py's module docstring).
DRIFT_CLOSE_DECISION_THRESHOLD_CP = int(os.environ.get("DRIFT_CLOSE_DECISION_THRESHOLD_CP", "20"))

# A drift candidate additionally requires the played move to not have
# been objectively bad on its own — otherwise it's already caught as an
# inaccuracy/mistake/blunder, and double-counting a real error as "just
# drift" would understate how bad it actually was.
DRIFT_MAX_EVAL_DROP_CP = int(os.environ.get("DRIFT_MAX_EVAL_DROP_CP", "40"))

# Games analyzed in parallel during batch analysis (Final Pass 5). Each
# worker runs its own single-threaded Stockfish subprocess (see
# analysis.get_engine's Threads=1), so this is roughly one CPU core per
# worker — capped at 4 by default so a batch run doesn't peg every core on
# a typical laptop. Set to 1 to force sequential (also the fallback for
# small batches, where process startup overhead isn't worth it).
#
# Measured on the dev machine this was built on: 4 workers took 260.7s for
# a 35-game batch vs. 304.3s at workers=1 — a real ~14% win, not the ~4x
# you'd hope for from 4 workers. Process spawn overhead (each worker
# cold-starts a fresh interpreter and re-imports everything) and uneven
# per-game runtimes (2-30s+ depending on game length) eat into the
# theoretical gain. Expect this ratio to vary a lot by machine — try
# ANALYSIS_WORKERS=1 vs. the default and compare on yours if it matters to you.
ANALYSIS_WORKERS = int(os.environ.get("ANALYSIS_WORKERS", str(min(4, os.cpu_count() or 4))))


def find_stockfish_path() -> str:
    """Locate the Stockfish binary without assuming a fixed install path,
    so this works across machines/OSes, not just the one it was set up on.

    Resolution order:
      1. STOCKFISH_PATH env var (or .env), if set (explicit override).
      2. `stockfish` on PATH (covers apt/brew installs, and winget once the
         shell that added it to PATH has been restarted).
      3. Common per-OS install locations (winget's package dir on Windows,
         Homebrew/apt paths on macOS/Linux), globbed since versioned
         package folder names change between Stockfish releases.
    """
    env_path = os.environ.get("STOCKFISH_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path

    on_path = shutil.which("stockfish")
    if on_path:
        return on_path

    candidates = []

    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        candidates.extend(glob.glob(os.path.join(
            localappdata, "Microsoft", "WinGet", "Packages",
            "Stockfish.Stockfish_*", "stockfish", "stockfish-windows-*.exe",
        )))

    candidates.extend([
        "/usr/games/stockfish",           # apt (Debian/Ubuntu)
        "/usr/bin/stockfish",             # apt (some distros)
        "/usr/local/bin/stockfish",       # Homebrew (Intel Mac) / manual install
        "/opt/homebrew/bin/stockfish",    # Homebrew (Apple Silicon)
    ])

    for path in candidates:
        if path and os.path.isfile(path):
            return path

    raise FileNotFoundError(
        "Could not find a Stockfish binary. Install it and either make sure "
        "it's on PATH, or set the STOCKFISH_PATH environment variable to "
        "its full path.\n"
        "  Windows: winget install Stockfish.Stockfish  (then restart your shell)\n"
        "  macOS:   brew install stockfish\n"
        "  Linux:   sudo apt install stockfish  (or your distro's equivalent)"
    )


STOCKFISH_PATH = find_stockfish_path()


# --- Chess API etiquette (Final Pass 2) --------------------------------------

API_MAX_RETRIES = int(os.environ.get("API_MAX_RETRIES", "3"))
API_BACKOFF_BASE_SECONDS = float(os.environ.get("API_BACKOFF_BASE_SECONDS", "1.0"))
API_INTER_REQUEST_DELAY_SECONDS = float(os.environ.get("API_INTER_REQUEST_DELAY_SECONDS", "0.3"))


# --- Optional integrations ----------------------------------------------------

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# Where the dashboard is reachable from wherever this process runs —
# used to build a clickable game link in Discord alerts/digests, which
# are sent from a background thread or a CLI script with no HTTP request
# of their own to read a host from. Override if `serve` runs on a
# different host/port than the default.
BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000")

# Advanced features, Section 10 — immediate per-game Discord alerts (as
# opposed to digest.py's weekly summary): a freshly-analyzed game posts
# its own alert if its accuracy is notably low or it contains an
# especially large blunder. Both overridable, same pattern as every
# other tunable in this file.
ALERT_ACCURACY_BELOW = float(os.environ.get("ALERT_ACCURACY_BELOW", "50"))
ALERT_BLUNDER_EVAL_DROP_CP = float(os.environ.get("ALERT_BLUNDER_EVAL_DROP_CP", "500"))

# Convenience defaults so `refresh`/`digest` can run without retyping
# usernames every time — CLI flags (Final Pass 6) always override these.
LICHESS_USERNAME = os.environ.get("LICHESS_USERNAME")
CHESSCOM_USERNAME = os.environ.get("CHESSCOM_USERNAME")


# --- Lichess OAuth (Web platform, Section 1) --------------------------------

# Register a Lichess OAuth app at https://lichess.org/account/oauth/app — no
# client secret is issued for it (Lichess's OAuth apps are "public clients"),
# since this flow uses PKCE instead: the code_verifier proves possession of
# the original request instead of a shared secret, which is also why this
# app is safe to run without ever storing a Lichess client secret at all.
LICHESS_OAUTH_CLIENT_ID = os.environ.get("LICHESS_OAUTH_CLIENT_ID", "chess-mistake-tracker")
LICHESS_OAUTH_REDIRECT_URI = os.environ.get(
    "LICHESS_OAUTH_REDIRECT_URI", f"{BASE_URL}/auth/lichess/callback"
)
LICHESS_OAUTH_SCOPES = os.environ.get("LICHESS_OAUTH_SCOPES", "")  # "" = public profile only

SESSION_COOKIE_NAME = os.environ.get("SESSION_COOKIE_NAME", "cmt_session")
SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "30"))

# Set to true once the app is served over HTTPS (a real deployment) so the
# session cookie is marked Secure. False by default since local dev over
# plain http://127.0.0.1 is the common case for this app.
SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"


# --- Celery / Redis (Web platform, Section 4) -------------------------------

# Decouples Stockfish analysis (CPU-bound, can take minutes for a big batch)
# from the request/response cycle. Redis doubles as both the task broker and
# the result backend here — one moving part instead of two for a project
# this size.
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


# --- Coaching Report chat coach (coaching_chat.py) --------------------------

# Talks to a locally-run Ollama (https://ollama.com) instead of a paid API —
# free forever, no API key, runs entirely on this machine. The Coaching
# Report page's chat feature is only offered when Ollama is actually
# reachable here (coaching_chat.is_configured() — a live check, not just
# "is this URL set", since Ollama being installed doesn't mean it's
# currently running).
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

# A broadly-available, commonly-pulled default — still needs `ollama pull
# <model>` run once on this machine before it'll actually work, same as any
# Ollama model. Configurable since which models are pulled/how capable this
# machine is varies a lot more than anything else in this file.
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1")

# Local inference on modest hardware can be genuinely slow (unlike a paid
# API's managed infrastructure) — generous rather than the short timeout a
# hosted API call would warrant.
OLLAMA_CHAT_TIMEOUT_SECONDS = float(os.environ.get("OLLAMA_CHAT_TIMEOUT_SECONDS", "120"))

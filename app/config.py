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

# Convenience defaults so `refresh`/`digest` can run without retyping
# usernames every time — CLI flags (Final Pass 6) always override these.
LICHESS_USERNAME = os.environ.get("LICHESS_USERNAME")
CHESSCOM_USERNAME = os.environ.get("CHESSCOM_USERNAME")

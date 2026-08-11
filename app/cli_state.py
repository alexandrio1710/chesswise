"""
Final Pass 6 — Remembers previously-used Lichess/Chess.com usernames so
`refresh`/`digest` can run without retyping them every time. An explicit
--lichess-user/--chesscom-user flag on any command always overrides what's
stored here for that one run (and updates what's remembered for next time).

Deliberately a separate small JSON file rather than .env: this is runtime
state the CLI writes to itself after a successful run, not configuration
you hand-edit — mixing the two would make .env's contents unpredictable.
"""

import json
from pathlib import Path

STATE_PATH = Path(__file__).parent.parent / "cli_config.json"


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(**kwargs) -> None:
    state = load_state()
    state.update({k: v for k, v in kwargs.items() if v is not None})
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")

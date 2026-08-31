# Chesswise

[![Tests](https://github.com/alexandrio1710/chesswise/actions/workflows/tests.yml/badge.svg)](https://github.com/alexandrio1710/chesswise/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Pulls your own game history from Lichess and/or Chess.com, runs every game
through Stockfish, and surfaces *patterns* in your mistakes instead of just
raw engine analysis of one game at a time — where you're actually losing
value (which phase, which openings, how time pressure affects you), a
tactics trainer built from your own real blunders, and month-over-month
trend tracking.

![Dashboard overview](docs/screenshots/dashboard.png)
![Puzzle trainer](docs/screenshots/puzzles.png)

## Engineering highlights

- **Real published algorithms, not approximations** — Lichess's own
  accuracy-scoring and move-judgment formulas, Stockfish's own win-rate
  model, US Chess's actual 2024 rating-conversion formula, and Static
  Exchange Evaluation for tactical detection, each verified against its
  original source and against real game data, not implemented from memory.
- **Caught and reverted my own regression** — shipped a puzzle-quality
  filter, then tested it against the app's own puzzle history and found
  it rejected 95% of puzzles that were already good. Diagnosed why,
  reverted it, and wrote up the postmortem in the changelog rather than
  quietly patching over it.
- **300+ automated tests** — hand-verified unit tests for chess-specific
  edge cases (multi-piece tactical exchanges, mate-distance handling,
  opposite-side-castling detection) plus a real-engine integration test,
  running on every push via CI.
- A numbered schema-migration system with automatic pre-migration
  backups — 23 migrations shipped over the project's life without losing
  data.

See [CHANGELOG.md](CHANGELOG.md) for the full, detailed history of *why*
each design decision was made, not just what changed.

## Features

- Fetches and normalizes games from both Lichess and Chess.com into one
  internal shape, with optional Lichess OAuth login and multiple local
  profiles (more than one person's data, or one person's separate
  accounts, in a single install)
- Classifies every move on a ten-tier scale (best/excellent/good/
  inaccuracy/mistake/blunder, plus brilliant/great/book/miss) and by game
  phase (opening/middlegame/endgame), using Lichess's own real move-
  judgment and phase-detection algorithms rather than ad hoc thresholds
- A Game Report per game: accuracy (Lichess's own real accuracy formula),
  an estimated rating (with a USCF-equivalent figure via US Chess's actual
  published conversion formula), and a short written summary
- A dashboard: mistakes by phase, worst games, monthly trend, an openings
  view (win rate and mistake rate by opening family), a real FIDE
  Tournament Performance Rating by time control, and a time-management
  (Clock) view correlating move quality with time spent
- An Opening Explorer (your own games plus a live Lichess community
  reference, filterable by rating band) and an Endgame Tablebase trainer
  (replays your own endgame mistakes against Lichess's free tablebase API
  for provably perfect defense)
- A puzzle trainer generated from your own flagged mistakes — find the
  move you missed, see the engine's top lines, with spaced-repetition
  scheduling (SM-2) and a "practice my mistakes" mode
- **Coaching Report**: bulk-import a bunch of games at once and get a
  batch-level report designed to catch a pattern no single-game review
  can — "quiet strategic drift" (individually fine moves that add up to a
  worse plan than an available alternative), broken down by time control
  and by your own repertoire's known structural trouble spots (e.g. a
  Sicilian Dragon opposite-castling race, a King's Indian Mar del Plata
  structure), with the highlighted positions turned into attemptable
  practice puzzles and a trend note against your last batch. Optionally
  chat about a generated report with a locally-run, free LLM (via
  [Ollama](https://ollama.com) — no API key, no cost) grounded in that
  report's own data.
- An optional weekly Discord digest
- Runs entirely locally: SQLite file, no external services required
  beyond the Stockfish binary (Discord and the chat coach are opt-in)

## Setup

**Requirements:** Python 3.12+, and a Stockfish binary (a separate install,
not a Python package).

1. **Clone and install dependencies**

   ```
   git clone https://github.com/alexandrio1710/chesswise.git
   cd chesswise
   python -m venv venv
   venv\Scripts\activate          # Windows
   source venv/bin/activate       # macOS/Linux
   pip install -r requirements.txt
   ```

2. **Install Stockfish**

   ```
   # Windows
   winget install Stockfish.Stockfish

   # macOS
   brew install stockfish

   # Linux
   sudo apt install stockfish
   ```

   The app auto-detects it (PATH, then common install locations). If it
   can't find it, set `STOCKFISH_PATH` in your `.env`.

3. **(Optional) Install Ollama for the Coaching Report chat coach**

   Only needed if you want to chat about a generated Coaching Report —
   everything else works without it.

   ```
   # Install from https://ollama.com, then:
   ollama pull llama3.1
   ```

   Free and fully local — no API key, no per-message cost. The chat box
   only appears on the Coaching Report page when Ollama is actually
   running; nothing else in the app depends on it.

4. **Configure `.env`** (optional — everything has a sensible default)

   ```
   cp .env.example .env
   ```

   Fill in whatever you need — your usernames, a Discord webhook URL if
   you want the digest, or tuning knobs like `STOCKFISH_DEPTH`. See
   `.env.example` for the full list.

5. **Run it**

   ```
   cd app
   python cli.py fetch --lichess-user yourname --chesscom-user yourname
   python cli.py analyze
   python cli.py puzzles
   python cli.py serve
   ```

   Then open http://127.0.0.1:8000. The Coaching Report (bulk-batch
   pattern mining) is web-UI only, at `/coaching-report` — paste a
   multi-game PGN export there rather than through the CLI.

## Usage

Everything goes through `python cli.py <command>` (run from the `app/`
directory). `python cli.py <command> --help` shows each command's own
options.

| Command | What it does |
|---|---|
| `fetch` | Pull recent games for one or both sites and store them (a full pull, not incremental). `--lichess-user`, `--chesscom-user`, `--max-games` |
| `analyze` | Run Stockfish analysis on every stored game that hasn't been analyzed yet. `--depth` (speed/accuracy tradeoff), `--workers` (parallel processes) |
| `refresh` | `fetch` (incremental — only games newer than what's stored) + `analyze` in one step. Remembers your usernames after the first run, so later calls need no arguments |
| `puzzles` | Generate tactics puzzles from every flagged mistake/blunder that doesn't have one yet |
| `digest` | `refresh` + `analyze` + post a summary to a Discord webhook (`DISCORD_WEBHOOK_URL` in `.env`, or `--webhook-url`) |
| `serve` | Start the local dashboard (`--host`, `--port`, `--reload` for development) |

Re-running any command is always safe — games are deduped, already-analyzed
games are skipped, and puzzles aren't regenerated for mistakes that already
have one.

**Scheduling the digest**: `cli.py digest` only runs when triggered; wire it
up with cron (`crontab -e`) or Windows Task Scheduler if you want it weekly.

## Development

```
pip install -r requirements.txt -r requirements-dev.txt
pytest -v
```

Tests run automatically on every push via GitHub Actions.

## Deployment

The app runs cleanly anywhere Python + Stockfish are available — that's
the whole local setup above. A `Dockerfile` is included for deploying to
Fly.io, Railway, or any other container host:

```
docker build -t chesswise .
docker run -p 8000:8000 chesswise
```

**Persistence matters**: the SQLite database lives inside the container by
default and is lost on every redeploy/restart unless you mount a
persistent volume and point `DB_PATH` at a path inside it. `fly.toml.example`
has a starting point for Fly.io (`flyctl launch`, `flyctl volumes create`,
then `flyctl deploy`) — rename it to `fly.toml` after filling in your app
name. Whichever platform you use, set your `.env` values as that
platform's secrets/environment variables rather than committing `.env`.

`GET /health` reports basic status (game counts, last analysis run) without
exposing any personal data — a quick way to confirm a deployed instance is
alive.

## Roadmap

Ideas for later, not committed to any particular order:

- Multi-move puzzle verification ("cooking" — checking that a puzzle's
  full forced sequence holds up, not just its first move), the way
  Lichess's own puzzle generator does
- Push notifications / mobile app wrapper around the dashboard

## License

MIT — see [LICENSE](LICENSE).

# Changelog

## v3 — Robustness, testing, and deployment prep (unreleased)

A polish pass over the whole project: nothing here changes user-facing
behavior from v2, it makes the existing behavior safer and easier to trust.

- **Data safety**: versioned schema migrations (`migrations.py`), with an
  automatic timestamped backup of the database before any migration runs.
- **API etiquette**: a descriptive User-Agent on every Lichess/Chess.com
  request, retry-with-backoff on 429s and transient network errors, and a
  small delay between requests when paginating Chess.com's monthly archives.
- **Code quality**: replaced ad-hoc `print()` status output with the
  `logging` module (INFO/WARNING/ERROR used deliberately), added error
  handling around every external call (both chess APIs, Stockfish, the
  Discord webhook) so failures produce a clear message instead of a raw
  traceback, pinned `requirements.txt`, centralized machine-specific
  settings into `.env` (see `.env.example`), and expanded docstrings on the
  mistake-classification, phase-detection, and opening-grouping logic.
- **Tests**: unit tests for severity/phase classification, source
  normalization (Lichess vs. Chess.com), opening-family grouping, and the
  API retry logic, plus one integration test that runs the real pipeline
  against a small fixture PGN. Runs on every push via GitHub Actions.
- **Performance**: batch analysis now runs across multiple worker
  processes in parallel (configurable via `ANALYSIS_WORKERS`); the
  `analyzed` flag already in the schema doubles as a cache so re-running
  analysis never repeats Stockfish work on an already-analyzed game;
  Stockfish depth is configurable per-run via `--depth`.
- **CLI**: consolidated the separate scripts into one entry point
  (`cli.py`) with `fetch` / `analyze` / `refresh` / `puzzles` / `digest` /
  `serve` subcommands, each with `--help`. Previously-used usernames are
  remembered so `refresh` doesn't need them retyped every time.
- **Deployment**: added a `Dockerfile` and Fly.io config template, a
  `/health` endpoint, and confirmed the database file and secrets aren't
  exposed by any route.
- Also fixed two real bugs surfaced while building this pass: re-analyzing
  a game that already had generated puzzles violated a foreign-key
  constraint (puzzles referencing the old mistake rows weren't cleared
  first), and a mobile-viewport layout bug where hidden hover-tooltips
  still forced horizontal page scroll.

## v2 — Puzzles, trends, openings, dashboard polish

- **Personalized puzzle trainer**: every mistake/blunder gets a puzzle
  generated from the position right before it (best move, top engine
  lines, and what you actually played), with a "Practice my mistakes"
  mode that prioritizes your worst category.
- **Trend tracking**: an incremental `--refresh` mode that only fetches
  games newer than what's stored, and a month-over-month view of
  blunders/mistakes per game.
- **Opening analysis**: win rate and opening-phase mistake rate by
  opening family (grouped across Lichess's and Chess.com's different
  naming conventions), plus an "openings to review" list ranked by
  frequency × mistake rate.
- **Dashboard polish**: an Openings tab, a manual dark-mode toggle,
  mobile-responsive layout, loading states, and clearer stat labels (e.g.
  "blunders per game" instead of an ambiguous "blunder rate").
- Also fixed real bugs from a dedicated edge-case pass: nonexistent
  usernames crashed instead of being treated as zero games, and
  non-standard chess variants (Three-check, Horde) crashed analysis
  instead of being skipped.

## v1 — Base pipeline

- Fetch recent games from Lichess and Chess.com and normalize both into
  one internal shape.
- Store games in SQLite, deduped per source.
- Analyze every game with Stockfish and classify moves by severity
  (inaccuracy/mistake/blunder) and game phase (opening/middlegame/endgame).
- Aggregate stats: mistakes by phase/severity, clock-pressure correlation,
  worst games, and a plain-English takeaway.
- A local dashboard (FastAPI + vanilla JS) showing all of the above, with
  an All/Lichess/Chess.com filter.

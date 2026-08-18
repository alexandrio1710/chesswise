# Changelog

## v6 — Renamed to Chesswise; Game Report (ten-tier review, Elo estimate)

**Renamed from "Chess Mistake Tracker" to "Chesswise"** — the project outgrew
the old name a while ago (openings, endgames, insights, SRS, now a full game
report); nothing about the rename touches the repo slug, database, or URLs.

**Game Report**, this project's answer to the "Game Review" feature on sites
like Chess.com: everything in that comparison worth building for a local,
self-hosted tool, built from scratch against this project's own data (no
scraping, no copied algorithm — none of those sites publish theirs anyway).
- Four new move-classification tiers on top of the existing six (Best/
  Excellent/Good/Inaccuracy/Mistake/Blunder, migration 4): **Brilliant** (a
  top-choice move that offers real, uncompensated material and still holds
  up), **Great** (a top-choice move in a position sharp enough that only it
  kept the advantage), **Book** (matches the local ECO database's opening
  line, see v5), and **Miss** (a mistake/blunder that specifically threw
  away an advantage the mover already had, rather than merely making the
  position worse). Computed by `game_report.py`'s enrichment pass — one
  extra MultiPV engine query per move, run on demand per game rather than
  as part of routine analysis.
- **Estimated performance rating**: a per-game Elo estimate from that
  game's average centipawn loss, via a documented (not proprietary, not
  statistically fitted) interpolation table — a rough "what strength does
  this game's move quality resemble", not a rating measurement.
- **Phase-by-phase accuracy** (opening/middlegame/endgame) and a short
  coach-style summary, both new fields on the existing per-game accuracy
  score (`stats.compute_game_accuracy`, v4).
- New `GET /api/games/{id}/report` endpoint and a "Game Report" card on the
  game detail page; computes in a background thread and polls to ready
  (~0.5s/move — too slow to block the request on) and caches in a new
  `game_reports` table so repeat views are instant.

Deliberately NOT attempted, since they don't fit a local/self-hosted
analysis tool or would mean reusing another site's actual content: Puzzle
Battle (needs live matched opponents), video lessons/opening courses
(would mean hosting someone else's course content), coach chat, and any
cosmetic/account features.

## v5 — Multi-user web platform foundations

Four additive building blocks toward running this as a shared, multi-user
web app instead of a single local install — none of them change existing
routes or require a login for anyone still using it locally.

- **Lichess OAuth login**: sign in via `oauth.lichess.org` (Authorization
  Code + PKCE, no client secret required or stored). Adds `users`,
  `sessions`, and `oauth_states` tables and a `user_id` ownership column on
  `profiles`/`games`/`puzzles`, plus `auth.require_game_owner` /
  `require_puzzle_owner` FastAPI dependencies that 404 (not 403) any
  request for another user's data. Existing local data is left unowned
  until a user explicitly claims it (`POST /api/me/claim`).
- **Per-user SM-2 spaced repetition**: a `puzzle_progress` table and
  `srs_sm2.py` implement classic SuperMemo-2 scoped to (user, puzzle) —
  kept alongside, not replacing, the existing Leitner-box `srs.py`, which
  only ever worked correctly for a single implicit user.
- **ECO opening classification**: a local `eco_codes` table imported from
  the public lichess-org/chess-openings dataset (`eco_import.py`), and
  `eco.classify_game_opening()`, which matches a game's moves to the
  deepest ECO entry they're consistent with. Wired into game ingestion
  (`db.save_games`) so every newly fetched game gets an exact ECO code and
  opening name, with `eco.backfill_missing_eco()` for games stored before
  this existed.
- **Background analysis via Celery + Redis**: `analyze_game_task` moves
  Stockfish analysis off the request path, with `analysis_status` /
  `analysis_task_id` / `analysis_error` columns on `games` and
  `/api/analyze/start` + `/api/analyze/status` endpoints to queue and poll
  it per user. Optional infrastructure — the app still starts and serves
  every other route if `celery`/`redis` aren't installed or Redis isn't
  reachable.

## v4 — Advanced analysis, training, and insight features

A large expansion across eleven areas, all built on free/public data only
(Lichess's opening-explorer, tablebase, and puzzle APIs; the player's own
stored games; local Stockfish) — no scraping or reuse of any commercial
site's content, branding, or UI text.

- **Full Game Review**: every move (not just flagged mistakes) gets a
  quality tier — Best/Excellent/Good in addition to the existing
  Inaccuracy/Mistake/Blunder — plus a 0–100 per-game accuracy score from
  average centipawn loss (this project's own exponential-decay formula,
  documented and calibrated, not copied from any external site), a full
  eval graph, the game's single critical moment (largest eval swing) with
  board + engine's best move, and free-text notes on a game or move.
- **Opening Explorer**: live community win/draw/loss stats from Lichess's
  public opening-explorer API at any position, shown alongside the
  player's own stats for that same position, with click-through moves and
  divergence flags (positions where the player rarely plays a move the
  community plays often, at a notably better win rate).
- **Endgame Tablebase Integration**: perfect-play results and best moves
  from Lichess's public 7-piece tablebase for endgame-phase mistakes,
  showing whether a blunder actually changed the theoretical result, plus
  an Endgame Trainer that replays the player's own tablebase-eligible
  mistakes against perfect defense.
- **Puzzle Rush + Spaced Repetition**: timed 3/5-minute Puzzle Rush
  sessions with streak/accuracy/avg-time tracking, a Leitner-system
  spaced-repetition scheduler (5 boxes) with a "due for review" queue,
  full per-puzzle attempt history, and phase/severity filters.
- **Free Analysis Board**: paste any PGN or FEN for full analysis through
  the same pipeline as synced games, with optional save-to-database
  (tagged `source=manual`) or one-off analysis.
- **Search and Filter**: combinable filters (opponent, date range,
  opening, result, time control, source, color, has-blunder) over every
  stored game, sortable, feeding their own mini stats dashboard.
- **Advanced Stats and Insights Dashboard**: rating-over-time (backfilled
  from Elo tags already present in stored PGNs — no new API calls
  needed), win rate by color/time-control/day-of-week/time-of-day, game
  length in wins vs. losses, performance vs. opponent rating band,
  comeback rate (from the real per-move eval trace), and a data-driven
  "most notable insights" ranking (deviation from a 50% baseline, weighted
  by sample size) rather than a fixed list of stats to always show.
- **Clock Management Analysis**: time-spent-per-move derived from stored
  clock readings and each game's own PGN time control (no schema change
  needed), a per-game time chart, games where the final clock reading and
  the result don't line up, and average thinking time by move quality —
  reported as the data actually shows it, including a real finding that
  the fastest wrong moves are also the worst ones.
- **Multi-Profile Support**: fully local, no accounts or login — named
  profiles with linked Lichess/Chess.com usernames sharing one database,
  a profile switcher (scoped deliberately to the pages where "whose data"
  matters — Dashboard, Insights, Clock, Search — while Puzzles/Explorer/
  Endgame Trainer/Analyze stay shared across all profiles), and a
  head-to-head stats comparison view.
- **Progress, Goals, and Auto-Reports**: simple goal tracking against
  live stats (e.g. "endgame blunder rate below 20%"), a this-week-vs-
  last-week summary, an auto-generated plain-language narrative
  (template-based, no LLM calls), and Discord alerts extended from a
  weekly-only digest to immediate per-game alerts on notably low accuracy
  or an especially large blunder.
- **Export and Sharing**: a downloadable, Canvas-drawn shareable game
  card (accuracy, eval sparkline, critical moment) for Discord/Reddit,
  plus CSV/JSON export of the Search page's current filtered view and a
  full stats JSON export.
- **Bonus, mid-session addition**: Puzzle Rush no longer draws only from
  the player's own flagged mistakes — a "By opening" mode sources real
  multi-move tactical puzzles from Lichess's free public puzzle API for
  openings the player actually plays, graded server-side move-by-move
  (including the opponent's forced replies), the same way every other
  puzzle in this app is graded.
- Real bugs found and fixed while building this pass: Stockfish
  evaluation-perspective handling, the tablebase API's category field
  being relative to the wrong side, a rating chart that combined two
  non-comparable rating pools into one misleading line, and — found via
  systematic verification against 5 independent live puzzles, not just
  eyeballing one — the opening-puzzle fetcher initially replayed the
  wrong number of plies from Lichess's puzzle API response.

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

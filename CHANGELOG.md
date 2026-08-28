# Changelog

## v13 — Fixed accuracy/ACPL crater bug; rating chart now splits by time control

The new accuracy-by-time-control card (v12) was reporting implausibly low
numbers (39.9% rapid, 53.6% bullet, 62.8% blitz). Root cause: analysis.py
represents a forced mate as roughly +-10000cp (`MATE_SCORE_CP`) so eval
comparisons don't need special-case mate handling — but `compute_game_
accuracy()` averaged that raw into ACPL, so a single move that missed or
walked into mate in an already-decided position could carry a "drop" in
the thousands of centipawns and crater a whole game's accuracy score
regardless of how the other 40+ moves were played. Confirmed against real
data: 11 of the 59 games analyzed at the time had a move with an eval_drop
within a few hundred cp of MATE_SCORE_CP. Fixed by capping the eval
magnitude on both sides of the diff (`stats.ACCURACY_EVAL_CAP_CP = 1000`,
via a new shared `stats.capped_eval_drop()`) before it feeds the average —
a real "winning to losing" swing still counts as the large mistake it is
(up to 2x the cap), but a move that doesn't change an already-decided
verdict now correctly costs ~0. `game_report.py`'s separate `_acpl()`
(the Game Report's headline accuracy and estimated-performance-rating)
had the identical unbounded-ACPL bug independently and gets the same fix.
Post-fix, the same three time controls read 79.2% / 75.0% / 78.9%.

Also fixed while chasing this down:
- **Profile-linking crash**: `profiles.resolve_profile_id()` crashed with
  an unhandled `sqlite3.IntegrityError` whenever a fetched username
  happened to match an existing profile's name under a different source
  (hit while fetching a second Chess.com account for cross-checking the
  accuracy fix — a Lichess profile named "vxyzrs" already existed, and
  fetching Chess.com's *unrelated, different-person* "vxyzrs" account
  tried to create a second same-named profile and crashed). Added
  `get_profile_by_name()`; a same-named existing profile is now reused
  instead of crashing — same-name is the only signal this app has for
  "same person," so this also fixes the legitimate case of linking two
  accounts on different sites that share a handle.
- **Rating-over-time chart didn't separate time controls**: it already
  split Lichess from Chess.com onto separate lines/scales (each site's
  ratings are a different, non-comparable pool), but still mixed e.g.
  bullet and rapid ratings from the same site into one line — those are
  *also* separate rating pools, so the combined line showed the same kind
  of fake "swing" the source-split was meant to prevent. Now grouped by
  (source, time control): color encodes time control, solid-vs-dashed
  stroke encodes source, and a legend lists each line's own game
  count/start/end/change. Verified live: 7 correctly-separated lines
  (chesscom/lichess x bullet/blitz/rapid, plus chesscom classical).

Dataset: fetched and analyzed the last 3 months of Chess.com games for
`vxyzrs00` (218 new games, 277 analyzed total) to have a larger, current
corpus for verifying the accuracy fix.

## v12 — Accuracy averages broken out by time control

Insights had win rate by time control (`insights.win_rate_by_time_control`)
and an overall average accuracy (`progress.average_accuracy`), but nothing
combined the two — no way to see "my blitz accuracy vs. my bullet
accuracy." Added `insights.accuracy_by_time_control()`: groups analyzed
games by time control, then averages `stats.compute_game_accuracy` per
group (accuracy isn't a SQL aggregate — it comes from replaying each
game's move trace — so this reuses the same per-game formula
`average_accuracy()` already uses, just grouped instead of pooled).
Wired into `/api/insights` and `/api/export/stats`, and added a new
"Accuracy by time control" card on the Insights page next to the
existing win-rate-by-time-control one, reusing the same bar-list styling.

## v11 — Fixed sub-pixel clipping on the bottom row of every board

Every board in the app (Analyze, game review, puzzles, rush mode, opening
trainer, Opening Explorer, Endgame Trainer) applied `border-radius` and
`overflow: hidden` directly to the same element that was `display: grid`
with 8 equal tracks. When the board's pixel width doesn't divide evenly
by 8 (e.g. 402px / 8 = 50.25px per square), the browser's rasterizer can
round the rounded-corner clipping mask a hair short of the last row,
shaving a sliver off the bottom tile — reported as "the bottom tile of
the board is cut off a little." Layout-level geometry looked fine
(`board.bottom - lastSquare.bottom` was already 0 in idealized float
math), so this only showed up at the rasterization layer, not in
measurement.

Fixed by separating the rounded/clipped frame from the grid itself: a new
`.board-frame` wrapper now owns `border-radius`/`overflow: hidden`/the
1px border, while the inner grid element just fills it at `width: 100%;
height: 100%` with no rounding or clipping of its own. Applied to every
board across `analyze.html`, `game.html` (both the interactive board and
the critical-moment snapshot), `puzzles.html` (main, rush, and opening
boards), `explorer.html`, and `endgame.html`. Verified live via
`getBoundingClientRect()` on each page (board bottom vs. last-square
bottom now diffs by exactly 0, down from a 1px discrepancy before).

## v10 — Analyze Board rebuilt as a Chess.com-style interactive board

Pasting a PGN or FEN into the Analyze Board previously showed only a plain
eval graph and move table (PGN) or a bare non-interactive board with a
text list of engine lines (FEN) — nothing like a real analysis interface.
Rebuilt both around an actual board: PGN gets the same full move-by-move
interactive board added to the game page in v9 (step through with
buttons, arrow keys, or clicking a move-list row — backend now returns
`start_fen`/`fen_after` for a one-off unsaved analysis via the same
`stats.annotate_fen`/`get_starting_fen` the game page uses); FEN's single
position renders on the same board, with each top engine line clickable
to preview the resulting position (computed server-side, so a click needs
no round-trip). Both boards get a vertical eval bar in the Chess.com
style — white fill grows from the bottom as White's advantage increases,
with the FEN board's mover-relative line evals flipped to stay
White-positive like the PGN board's.

Also fixed while touching this styling: five more undefined-CSS-variable
bugs in the same family as v9's invisible-board-checkering one —
`--tier-brilliant`, `--tier-great`, `--tier-excellent`, `--tier-book`, and
`--tier-miss` were referenced by the Game Report's ten-tier move
classification but never defined anywhere, so half the tiers rendered as
invisible pills. Confirmed via computed style, then defined for both
themes.

## v9 — Interactive game board; every board's checkering was invisible; nav consolidation

**Every chessboard in the app was rendering with no light/dark square
checkering at all.** Puzzles, the Opening Explorer, the Endgame Trainer,
and the Analyze board's FEN viewer all referenced `--board-light`,
`--board-dark`, `--board-selected`, and `--board-dot` in their CSS, but
none of those custom properties were ever actually defined anywhere —
confirmed via computed style (`backgroundColor: rgba(0,0,0,0)`), not just
by reading the CSS. Every board in the app has been a blank grid with
floating piece characters since the Catppuccin redesign. Defined all four
(dark/light theme variants) on every page that uses a board.

**The game analysis page had no interactive board.** The only board on it
was a single static snapshot of the critical moment — the actual
move-by-move review (eval chart + move list) had nothing visual to look
at, unlike Chess.com's Game Review, which is fundamentally board-plus-
move-list. Added `stats.annotate_fen`/`get_starting_fen` (the server
computes every position's FEN via one PGN replay — same "server does
chess logic, client only renders FEN" split as every other board in this
app) and a full interactive board: step through every ply with
prev/next/first/last controls or arrow keys, synced bidirectionally with
the move list, last-move squares highlighted. Verified live against a
real analyzed game, including keyboard shortcuts not hijacking arrow keys
while typing a note.

**Navigation consolidated.** Every page had its own hand-copied nav bar,
and they'd drifted out of sync: some showed all 9 destinations, others
only 5-6 (never linking to Puzzles/Explorer/Endgame/Analyze at all), and
5 pages had no way to reach Profile management without detouring through
Dashboard. Replaced with one consistent structure everywhere: Dashboard,
Puzzles, and Search stay top-level; Explorer/Endgame/Analyze move into a
"Train ▾" dropdown; Insights/Clock/Progress move into an "Insights ▾"
dropdown; every page reaches Profiles via the same 👤 icon (added to the
5 pages missing it), highlighted when it's the current page.

## v8 — Full-codebase correctness audit

A systematic pass over the entire codebase (backend, frontend, infra, tests)
looking for bugs rather than adding features — five parallel reviews (data
ingestion, analysis/stats, puzzles/SRS/CLI, frontend, infra/migrations) each
independently reading their area in full, every finding re-verified against
the actual code (and, where practical, empirically reproduced) before being
treated as real. Grouped by theme; each item below shipped with test
coverage where the app's existing test infrastructure could exercise it.

**Two independent instances of the same datetime-format bug** already fixed
once in v5's `auth.create_session` (SQLite `datetime('now')` is
space-separated UTC text; Python's `datetime.isoformat()` is `T`-separated
and often local time — comparing the two as plain strings silently breaks):
`srs.py`'s Leitner `next_review_at` (puzzles never surfaced as due until a
full calendar day late — migration 16 backfills existing rows) and
`srs_sm2.py`'s SM-2 `next_review_date` (local `date.today()` vs. SQLite's
UTC `date('now')`, shifting due dates by up to a day depending on server
timezone/time of day).

**Fetch/ingestion hardening** (`fetchers.py`): `_combine_lichess_datetime`
used to fall back to the raw, un-normalized `"2026.08.10"`-style PGN tag on
any parse failure — broke incremental-refresh ordering (dot sorts after
dash) and crashed `insights.py`'s day-of-week/time-of-day breakdowns
outright (SQLite's `strftime()` returns NULL for a non-ISO date; now
guarded there too). `_request_with_retry` now retries 5xx (previously only
429s), a malformed `Retry-After` HTTP-date no longer crashes the retry loop,
a shared `requests.Session` replaces one-off connections, an aborted
Lichess game (`"*"` result) is skipped instead of silently counted as a
draw, a Chess.com game with neither a `uuid` nor a parseable `url` is
skipped instead of risking a dedup collision, a CRLF-terminated multi-game
PGN export no longer collapses into one corrupted blob, and a Lichess
incremental refresh no longer silently truncates (and then permanently
skips) a backlog larger than `max_games`. `alerts.py` and `eco_import.py`
now retry their outbound calls like every other one in the app;
`digest.py`'s game/mistake counts are derived from what a run actually did
instead of a global before/after diff that leaked in other local profiles'
activity, and it raises instead of calling `sys.exit()` from library code.

**Analysis pipeline**: `game_report.py` served a cached report forever with
no comparison against the game's last analysis time, so a re-analyzed game
silently kept showing stale accuracy/rating/summary. `batch_analyze.py`
no longer lets one dead worker process abort a whole batch's summary, and a
narrow post-upgrade migration race between parallel workers degrades to a
warning instead of crashing. `tablebase.py` now honors its own "never
crashes on a bad position" contract for malformed FENs, caps its
previously-unbounded cache, and the Endgame Trainer can finally accept a
promotion move (it always 400'd before — the feature was unusable for its
own core use case). `puzzles.py`'s puzzle-generation sweep no longer
misattributes an unrelated game's PGN-fetch failure onto whatever game
triggered it. `opening_puzzles.py` no longer 500s on an out-of-range
`move_index` and no longer silently accepts a queen-promotion attempt as
"correct" when the puzzle's actual solution needs a different piece.

**Frontend**: fixed two real attribute-injection gaps (a Lichess `game_url`
written unescaped into an `href`, and a linked username escaped with a
text-content-only helper that doesn't cover `"` — verified live in-browser
against the real page before and after), six places where a network
failure left the page frozen on its loading spinner forever instead of
showing an error (verified live by forcing `fetch()` to reject and
confirming recovery), a request-race in the Search page's filters, the
Opening Explorer silently playing a non-deterministic promotion piece, and
a `"`-delimited data attribute that truncated on a `|` character.

**Ownership/access control** (server.py, auth.py — see v7): puzzle detail/
attempt routes and every route taking `profile_id` as an optional filter
now go through the same unowned-or-mine check the rest of the app already
had.

Deliberately not touched, and why: Dockerfile's root user (no Docker
available to verify a build in this environment, and getting a mounted-
volume permission fix wrong risks breaking real deployments); the
concurrent-attempt read-then-write race in `srs.py`/`srs_sm2.py` (a correct
fix means expressing the scheduling math as raw SQL `CASE` expressions,
trading real clarity for a low-probability edge case); `opening_puzzles.py`
attempt idempotency (needs a real idempotency-key mechanism); `puzzles.py`
`check_attempt`'s unused `promotion` parameter (would need an API/frontend
change for a rare underpromotion-puzzle scenario); keyboard accessibility
for the Explorer/Endgame/Rush boards (real gap, larger scope than this
pass); and `get_mistakes_without_puzzles` running unscoped on every Celery
task (a scale concern for a future multi-user deployment, not a
correctness bug today).

## v7 — Ownership enforcement for the pre-OAuth routes; Analyze Board hardening

A follow-up to v5's OAuth/session foundations: `games.user_id` /
`profiles.user_id` / `puzzles.user_id` existed and were populated by
`auth.claim_unowned_data()`, but almost none of the legacy (pre-OAuth)
routes actually checked them — `GET /api/games/{id}`, its `/report` and
`/notes` routes, `DELETE /api/notes/{id}`, `DELETE /api/goals/{id}`,
`DELETE /api/profiles/{id}`, `POST /api/profiles/{id}/links`, and
`GET /api/mistakes/{id}/tablebase` would all read or delete any row by id
regardless of who was asking. Harmless for a single local install, but a
real bug the day this runs as the shared multi-user service the README's
roadmap describes.

- New `auth.require_*_access` dependencies (game, profile, note, goal,
  mistake) enforce one rule: a row with no owner (`user_id IS NULL` —
  everything from a local, never-logged-in install) stays visible to
  anyone, same as before OAuth existed; an owned row is visible only to
  the logged-in user who owns it, 404 (not 403) otherwise — same shape as
  the existing `require_game_owner`/`require_puzzle_owner`, just usable
  without forcing a login. Notes/goals have no owner column of their own;
  ownership is resolved through the game/profile they're attached to.
- Removed `GET /api/games/{id}/owned`, the "reference implementation"
  route left in the router from v5 — the pattern it demonstrated is now
  the real routes above, not a standalone example.
- `_refresh_status` (the in-memory `/api/refresh` progress dict) is now
  keyed per logged-in user (or one shared key for the no-login/local
  case), so two different users on a shared deployment triggering a
  refresh around the same time no longer read or overwrite each other's
  progress. `/api/settings` gets the same per-user split for remembered
  usernames, while staying on the original shared `cli_config.json` file
  for the no-login case (so CLI/web parity for a local install is
  unchanged).
- Known remaining gap, called out where it lives
  (`server.py`'s Section-1 comment block): fetch/ingestion
  (`fetch_and_store`/`save_games`) still doesn't tag new games with
  `user_id` — only the one-time `/api/me/claim` sweep does. So on a real
  multi-user deployment, games pulled in after login via the existing
  Refresh button land unowned (world-readable by the rule above) until
  claimed. These access checks are necessary but not sufficient for safe
  multi-tenant use; making ingestion itself user_id-aware is a separate,
  larger follow-up.
- **Analyze Board** (`POST /api/analyze/pgn`, `/api/analyze/fen`) takes
  arbitrary pasted input with no login required, and each request costs
  real Stockfish time — unbounded before this. Now: pasted PGNs over 400
  plies (`manual_analysis.MAX_ANALYSIS_PLIES`, ~200 full moves — well past
  any real game) are rejected before any engine work runs, and both
  routes are behind a coarse in-memory per-IP rate limit (10 requests /
  60s). Neither is a substitute for a real rate limiter at a reverse proxy
  in front of a public deployment, but both close the "one client loops
  requests and pins every core" case for a local/small deployment.
- `DELETE /api/notes/{id}` and `DELETE /api/goals/{id}` used to report
  `{"status": "deleted"}` even for a nonexistent id (unlike
  `DELETE /api/profiles/{id}`, which already 404'd). Fixed as a side
  effect of the access-check dependency, which resolves and 404s on a
  missing row before the route body runs.
- New `tests/test_auth.py` and `tests/test_server.py` — the app's HTTP
  layer (`server.py`, 1000+ lines) and session/OAuth layer (`auth.py`) had
  zero test coverage before this; both are now exercised via
  `fastapi.testclient.TestClient` against a throwaway DB, covering every
  access-check permutation above plus the ply cap and rate limit.

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

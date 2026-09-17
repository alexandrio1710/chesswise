# Changelog

## v41 — Removed a second, self-inflicted source of rating inflation

Reported as still inflated right after v40 shipped. v40 correctly fixed
`estimated_rating` itself (calibrated against a player's own real online
rating instead of a universal guess) but left it running through US
Chess's real FIDE-to-USCF conversion formula in the summary sentence —
e.g. a legitimately-calibrated 1425 became "~1736 USCF". That formula
converts a real FIDE (classical, over-the-board) rating into a US Chess
one; both are the same *kind* of rating. An online blitz/bullet rating
isn't on that scale and isn't related to it by any published formula, so
running one through the formula anyway doesn't convert it to anything
real — it just adds a misleading +200-400ish bump. The formula itself
was a correct, verified port; it was simply the wrong formula to apply
here once estimated_rating's meaning changed in v40. Removed the USCF
figure from the estimate entirely rather than look for a "correct"
online-to-USCF formula — no such published conversion exists to port,
same reasoning that already ruled out reproducing Kenneth Regan's
unpublished intrinsic-rating parameters in v40. Migration 25 strips the
"(~X USCF)" clause from every already-cached summary.

## v40 — Replaced the inflated "estimated rating" with a self-calibrated one

Reported as grossly inflated. Root cause wasn't a bug in the arithmetic —
it was the whole approach: ACPL_RATING_ANCHORS was a fixed table
"calibrated by eye against commonly-cited ballpark ACPL ranges," a known
failure mode of naive ACPL-to-rating tables in general. In an ordinary
game between two similarly-matched humans, low ACPL is easy to rack up in
quiet positions regardless of either player's real rating — the engine
agrees with almost any reasonable developing move there. What actually
separates a 1400 from a 2200 concentrates in rare sharp/critical moments,
not evenly across every move the way a flat curve assumes. (Real academic
work on this — Kenneth Regan's Intrinsic Performance Ratings — models
skill that way properly, but its fitted parameters were never published,
so there was nothing to port the way USCF's real conversion formula
already was.)

Replaced it with per-profile self-calibration instead of a universal
guess: `game_report.estimate_performance_rating()` now fits a real linear
regression of this profile's own historical rating (`games.player_rating`
— real numbers from Lichess/chess.com's own rating systems) against
their own time-control-adjusted ACPL in their other analyzed+rated games,
then reads this game's rating off that line. Verified against the real
database: previously-cached reports that would have shown ~2000+ for
ordinary club-level ACPL now land within roughly 100 points of the
player's actual rating at the time (e.g. 1429 estimated vs. 1521 real).
Below `MIN_CALIBRATION_GAMES` (8) real data points, or with no real ACPL
variance to fit a line against, this returns `None` rather than a guess —
a profile without enough history yet simply won't show a number, same
principle `stats.compute_game_accuracy` already follows below its own
minimum sample size. A new migration (24) recomputes every already-cached
report from data already on disk, no Stockfish re-run needed.

Also ran an independent sanity check on the underlying engine numbers
themselves, not just the formula: pulled a handful of real positions from
one of the tracked player's own analyzed Lichess games and queried
Lichess's public cloud-eval API (community-contributed, often far deeper
than this app's local depth-15 pass) for the same exact FENs. Every
position that had a cached community evaluation agreed with this app's
own local Stockfish eval in both sign and rough magnitude (e.g. +9cp vs.
+18cp, +35cp vs. +15cp, 0cp vs. +3cp) — consistent with ordinary
depth-vs-depth engine variance in near-equal positions, not a systematic
discrepancy. Combined with the accuracy formula already being a verified,
direct port of Lichess's own published source, this is real evidence
(not just a code-level argument) that this app's game analysis is
consistent with what Lichess's own analysis would produce for the same
game.

## v39 — Fixed a silent multi-hour hang in "Refresh"

A refresh on a large backlog (a bulk historical import, or a long gap
since the last sync) looked hung: the status text just said "Refreshing…
this can take a few minutes" no matter how long it actually took, with no
way to tell it apart from a crash. Reported after a real refresh ran over
20 minutes with zero feedback and got closed.

Diagnosed by checking on the running background thread directly rather
than guessing: it hadn't hung at all — fetch and analysis had finished
in minutes, but it was still grinding, one at a time, through generating
puzzles for a 998-mistake backlog. Puzzle generation
(`generate_puzzle_for_mistake`/`get_top_lines`) spawns a whole new
Stockfish process per puzzle and had never been parallelized, unlike
`batch_analyze.py`'s per-game analysis, which already uses a
`ProcessPoolExecutor` for exactly this kind of independent, CPU-bound,
one-item-at-a-time work. At the observed ~22s/puzzle that projected to
nearly 6 hours for the full backlog.

Two fixes, not one, since progress reporting alone wouldn't have made a
6-hour wait acceptable, and parallelizing alone wouldn't have fixed the
next backlog that's still too big to finish while someone's watching:

- `generate_all_puzzles()` now takes a `workers` argument and parallelizes
  the same way `run_batch_analysis()` already does (a picklable top-level
  worker function that re-opens its own DB connection, since neither a
  `sqlite3.Connection` nor a `sqlite3.Row` crosses a process boundary).
  `cli.py puzzles` now defaults to `ANALYSIS_WORKERS` parallel workers —
  verified end-to-end on the real 771-puzzle backlog left over from this
  bug: 4m49s with 4 workers and zero failures, versus a projected several
  hours single-threaded. The web-triggered refresh path (and the
  per-game Celery task in `tasks.py`) still force `workers=1`, same
  reasoning `run_batch_analysis`'s web call already documented: spawning
  a process pool from a thread that isn't `__main__`-guarded is asking
  for Windows-specific trouble.
- `/api/refresh/status` now reports which phase it's in (fetching /
  analyzing / generating puzzles) and a live done/total count for
  whichever phase is running, polled from a lightweight side thread
  rather than threaded through `batch_analyze.py`/`puzzles.py`'s own
  signatures. The Settings panel shows the real phase and count instead
  of a static time estimate that was already wrong for anything beyond a
  small day-to-day sync.

## v38 — A free, local chat coach for the Coaching Report

The Coaching Report was still a one-way document — you could read its
findings and practice the highlighted positions, but not ask it anything.
Added a chat box on the report page grounded in that specific report's
own computed data (the headline, time-control/structure stats, the
highlighted positions with their real engine lines, the repertoire
checklists) — it quotes the actual numbers back rather than reasoning
about chess from scratch, and its system prompt explicitly frames drift
candidates as "worth reviewing," never as confirmed errors, the same
standard this whole project already holds its own heuristics to.

First pass used the Anthropic API — but that costs real money per
message, which isn't the right tradeoff for a personal-scale tool run
locally next to a free, local Stockfish. Switched to a locally-run
[Ollama](https://ollama.com) instead: free forever, no API key, plain
HTTP via `requests` (already a dependency — no new package needed at
all). Considered chess-specific open-source LLMs first (ChessGPT,
arXiv:2306.09200) but passed: it's a 3B-parameter model from 2023, and
its own paper found the chat-tuned variant is *less* accurate at chess
state-tracking than the un-tuned base — a poor fit against a modern
general-purpose local model, especially since this feature's grounding
design means the model mostly needs to read structured data and discuss
it coherently, not independently know chess.

The chat box only appears when Ollama is actually reachable right now —
a live check (`GET /api/tags`), not just "is a config value set", since
installed-but-not-running is exactly the case a static check would get
wrong. No chat history is persisted server-side; each browser tab keeps
its own conversation for the report currently on screen.

## v37 — Coaching Report highlights are now practicable, not just readable

A report that only describes a pattern is a step short of actually
coaching — a real coach assigns practice. Every highlighted drift
position now becomes a puzzle: click a piece, click where it goes, and
find out immediately whether it holds up, with the same grading
tolerance (within 20cp of best, or matching mate distance) every other
puzzle in this app already uses (`puzzles.check_attempt`, reused
directly rather than re-implemented). The answer stays hidden until an
attempt is submitted, unlike the report's own always-visible explanation
of the same position.

New `drift_puzzles` table, deliberately separate from the existing
`puzzles` table rather than a relaxed version of it: every row in
`puzzles` traces back to a real, objectively-bad flagged mistake
(`mistake_id NOT NULL` by design), and a drift candidate is specifically
not that. Same reasoning `opening_puzzles` already established for being
its own table. Attempt tracking is a pair of counters on the row itself
for this first cut, not the shared spaced-repetition tables (those FK
specifically to `puzzles.id`) — a real, documented scope limit, not an
oversight.

Also fixed a bug caught only by testing the "second run over an
already-analyzed batch" path live: `run_multipv_pass`'s cached-data
branch selected columns that didn't include `is_drift_candidate`, which
the fresh-computation branch never exercises — the exact kind of gap
unit tests using constructed data can't catch, only a real second
request against real stored data.

## v36 — Coaching Report: bulk-batch pattern mining for quiet strategic drift

A new feature built around a specific diagnosed problem, not a generic
"run Stockfish on everything" tool: better ACPL in rapid than in blitz/
bullet, but worse rapid results relative to bullet/blitz on the same
platforms — the opposite of what blunder-counting alone would predict.
The working theory is "quiet strategic drift": sequences of individually
fine moves (low centipawn loss) that are collectively a worse plan than
an available alternative, invisible to single-best-move analysis because
it only flags moves that are objectively bad.

**Bulk import.** Paste a multi-game PGN export for one profile; reuses
this app's existing dedup path (`db.save_games`) and PGN-splitting
(`fetchers._split_pgn_blobs`) rather than a parallel importer, then runs
the existing routine analysis pipeline (`batch_analyze.run_batch_analysis`)
on it, all in the background.

**Drift-candidate detection.** A dedicated MultiPV-4 pass (own-color
moves only, kept separate from routine analysis the same way the
existing Game Report's own MultiPV=2 enrichment already is — see that
module's docstring) flags a move as a "drift candidate" when the
position had multiple engine-close options (within ~20cp of each other)
but the played move wasn't the top choice, while still not being bad
enough to already count as an inaccuracy/mistake/blunder. Book-theory
moves are excluded (confirmed necessary live: an early pass flagged a
game's literal first move, d4 vs e4, before this exclusion existed).
Every flagged position is framed as "worth reviewing," never a verdict —
an engine's win-percentage number can't determine whether a plan was
actually wrong on its own.

**Repertoire-aware structure tagging** (`repertoire.py`, new): hand-authored
detectors — not ported from anywhere, documented plainly as heuristics —
for this player's specific repertoire and its known failure patterns:
London (symmetric/minority-attack vs. IQP structure), Sicilian Dragon/
Hyperaccelerated Dragon (opposite-castling race, dark-squared-bishop
timing, ...Rxc3 timing), King's Indian Mar del Plata (locked-center race),
and Grünfeld (central-tension judgment). Each attaches its own checklist
of known failure modes to the report, plus a rough pawn-storm tempo count
for the two opposite-wing races.

**The report itself**: a headline comparing ACPL and win-rate leaders
across time controls (the actual diagnostic signal, not just ACPL alone),
a time-control and repertoire-structure breakdown, up to five highlighted
drift positions (each with a rendered board, the move played, the
engine's top choice, and the close alternatives actually on the table),
a "what to work on" list pulled from whichever repertoire structures
showed up, and a trend note comparing this batch's drift rate against
the profile's previous report — the whole point being to watch whether
it improves over time, not a one-off score.

New schema: `game_moves.multipv_lines`/`is_drift_candidate`,
`games.repertoire_structure`, and a `coaching_reports` table recording
each generated batch for that cross-batch trend comparison.

## v35 — Brilliant-move detection now uses real Static Exchange Evaluation

The Game Report's "was this sacrifice actually sound" check
(`_is_sacrifice`, used to decide Brilliant) was a one-ply heuristic: does
the mover's piece land on a square the opponent can capture, and does the
mover have at least one piece defending it? That check couldn't tell a
real sacrifice from a square attacked twice but defended only once — it
stopped as soon as it found the *first* defender, never noticing a second
attacker was still waiting behind it — nor could it correctly bail out of
a multi-piece exchange early when continuing stopped being worth it for
either side.

Replaced it with real Static Exchange Evaluation
([chessprogramming.org](https://www.chessprogramming.org/Static_Exchange_Evaluation)),
the standard technique chess engines use for exactly this question: play
out the full likely capture sequence on a square, cheapest attacker first
each time, with either side free to stop trading the moment it's no
longer profitable for them. Implemented with python-chess's own real
`Board.push()`/legal-move generation rather than hand-rolled attacker
bitboards, so promotions, en passant, and check/pin legality (including a
piece that can't recapture because it's pinned to its own king — a case
even a typical bitboard SEE glosses over) all fall out for free, at the
cost of being slower than a bitboard version — an acceptable trade for
something that runs once per already-identified best move, not in an
engine's search loop.

Caught and fixed a real bug in my own first draft here: this project's
`PIECE_VALUES` maps King to 0 for a completely different reason (a king is
never actually the piece being *captured* in legal chess, so it's a
never-exercised placeholder) — reusing that value for "the king as an
*attacker*" during the exchange walk would have made SEE greedily
capture with the king first, backwards from correct play. Gave the king a
distinctly large value for that one purpose so it's only ever spent as a
last resort.

## v34 — The Game Report's USCF figure now uses US Chess's own real conversion formula

This project's estimated-rating-to-USCF conversion was its own guess: a
flat "-100," on the assumption that US Chess ratings run somewhat below
FIDE-ish ratings for the same real strength. That assumption was
backwards. US Chess has published several rules of thumb over the years
for converting a FIDE rating to an equivalent US Chess one, and every one
of them — old and new — runs the *other* direction, adding rather than
subtracting ("USCF = FIDE + 100" being the most commonly cited one).

Replaced the flat offset with US Chess's actual current formula, updated
2024-01-01 alongside a matching overhaul of FIDE's own rating system:
`932 + 0.564×FIDE` at or below 2000, `20 + 1.02×FIDE` above it (continuous
at the seam — both pieces give exactly 2060 at FIDE 2000)
([US Chess's own announcement](https://new.uschess.org/civicrm/mailing/view?id=4405)).
US Chess's own stated purpose for this formula is converting a *real*
FIDE tournament rating, not an ACPL-derived guess like this project's
estimated_rating — that gap was already true of the old flat offset too,
so it's not a new caveat, just an existing one this is still subject to.

A migration recomputed the USCF figure baked into every already-cached
Game Report's summary sentence (the number itself was never stored
separately — computed fresh from estimated_rating on every read — so nothing
else needed touching).

## v33 — Every eval bar now uses Stockfish's own real win-rate model

Widened the search past Lichess to the wider open-source chess world for
this one. All five eval bars in this app (Game Report, Analysis Board,
Opening Explorer, Endgame Trainer, Puzzles) filled by a fixed *linear*
cp-to-percent mapping — `(cp + 1000) / 2000`. That treats a +300
advantage as equally significant whether it's early middlegame with all
the pieces on, or a bare king-and-pawn endgame, when in practice the same
raw eval means very different practical chances depending on how much
material is left.

Stockfish's own official engine source has a real answer to this — the
win-rate model behind its own "win/draw/loss %" UCI output, fit on real
fishtest self-play statistics
([`official-stockfish/WDL_model`](https://github.com/official-stockfish/Stockfish/blob/master/src/uci.cpp),
`win_rate_model`): a material-dependent logistic curve, steeper with
fewer pieces on the board. Ported it faithfully to JS in all five pages —
including correctly un-doing the UCI "display cp" normalization the raw
formula doesn't expect (`v = cp * a / 100`, inverting Stockfish's own
`to_cp`), and using *expected score* (win% + half of draw%) rather than
raw win% for the bar fill, since the raw model treats draws as a separate
outcome and is deliberately well under 50% at a dead-equal eval — an
eval bar should read 50% there, not something lower.

This is visibly more dramatic than the old bar or Lichess's own
(unrelated) win% sigmoid used elsewhere in this app for accuracy grading
— confirmed and explicitly signed off on before shipping, since it's a
real, authoritative model calibrated for a different question ("how does
this resolve under perfect continued play") than Lichess's curve answers
("how good was this specific human move").

## v32 — Stockfish calls now have a wall-clock safety net

Every engine call in this project (routine per-move analysis, the
MultiPV=2 enrichment pass, puzzle generation) was depth-only: `go depth
N`, no time or node ceiling. A pure depth target has no wall-clock bound —
an unusually sharp position can occasionally take far longer than the
typical ~0.1-0.5s to reach the same nominal depth, and with no cap that
stalls a whole batch run behind one game. Lichess's own puzzle generator
guards against exactly this with a combined limit — depth, time, and
nodes together, whichever is hit first
([generator.py's `pair_limit`](https://github.com/ornicar/lichess-puzzler/blob/master/generator/generator.py)).

Added `STOCKFISH_MAX_SECONDS_PER_POSITION` (10s, overridable) as that same
kind of combined ceiling on all three call sites, enabled directly by
v31's move to `chess.engine.Limit`, which already accepts `depth` and
`time` together. It's a rare safety net, not a normal-case constraint —
confirmed a real position still finishes in ~0.2s, nowhere near the cap.

## v31 — Engine calls go through python-chess's own UCI wrapper, not a separate `stockfish` package

Compared this app's Stockfish plumbing against Lichess's own real puzzle
generator again ([`lichess-puzzler`](https://github.com/ornicar/lichess-puzzler/blob/master/generator/generator.py)):
it talks to the engine through `chess.engine.SimpleEngine`, python-chess's
own official UCI wrapper — not the third-party `stockfish` PyPI package
this project used to depend on for the exact same job, on top of an
already-required `chess` dependency for board/PGN handling. Switched all
three call sites (`analysis.get_engine`, `game_report`'s MultiPV=2 pass,
`puzzles.get_top_lines`) to `chess.engine`, and dropped `stockfish` from
requirements.txt entirely — one fewer dependency doing the same job.

Beyond matching Lichess's own choice, `chess.engine` is a genuinely
better fit here: `PovScore.white()`/`.pov(color)` do the side-to-move
color conversion natively (this project used to hand-roll that sign flip
in three places — exactly the kind of thing a related bug already showed
up in earlier this session), `.score(mate_score=...)` flattens a mate
score the same way the old manual mate-in arithmetic did, and `analyse()`
takes a `Board` object directly instead of round-tripping through FEN
strings on every position. Verified against the real engine (not just
mocks) via the existing integration test and by regenerating a live game
report and several puzzles through the dev server.

## v30 — Reverted v29's puzzle uniqueness gate — it didn't hold up

While exploring further Lichess comparisons, checked v29's gate against
data instead of just theory: ran it against this app's own 1,413
already-generated puzzles (the ones that were good enough to ship *before*
v29 existed). Only 67 of them — 4.7% — would still pass. That's not "a bit
stricter," that's rejecting nearly everything, including the big, obvious,
clearly-good blunder puzzles a solver would expect to see.

The root cause: Lichess's gate solves a problem this app already solves
differently. Lichess's puzzles are multi-move sequences with one canonical
solution line, so a solver finding an equally-good *different* first move
is a real problem for their format — hence refusing to generate the
puzzle at all. This app's puzzles are single-move, and its own
attempt-grading code (`puzzles.py`) already accepts any of the engine's
top 3 candidate moves as correct if it's within 20cp of the best move (or
matches its mate distance) — a fairness safeguard already built at *solve*
time. Near an equal position, that 20cp tolerance corresponds to under 2
points on this app's win% scale — nowhere close to the 35-point gap
Lichess's real 0.7-winning-chances threshold converts to. Porting Lichess's
number on top of an already-solved problem, at a much stricter bar,
rejected almost everything.

Removed `has_unique_solution`/`_line_eval_cp` and their tests entirely.
Puzzle generation is back to how it worked before v29: every flagged
mistake/blunder gets a puzzle, and the existing solve-time tolerance
handles equally-good alternatives the way it always did.

## v29 — Puzzle generation now rejects ambiguous positions

Every flagged mistake/blunder was turned into a "find the best move" puzzle
regardless of whether a second move was nearly as good — a real risk for a
puzzle format that marks anything but the engine's exact top choice wrong.
Ported the uniqueness gate from Lichess's own puzzle generator
([`is_valid_attack` in generator.py](https://github.com/ornicar/lichess-puzzler/blob/master/generator/generator.py)):
a puzzle is only generated when the best move's win% is more than 35 points
clear of the runner-up (Lichess's own 0.7-on-a-(-1..1)-scale threshold,
converted to this app's win% scale), or there's no second candidate move at
all. Positions that fail the check are now skipped and tallied separately
from actual generation failures.

This only changes puzzles generated from here on — the 1,413 existing
puzzles are left as-is rather than retroactively re-checked and pruned,
since removing them would also delete their Leitner-box practice history.
Lichess's generator has a second layer of sophistication not ported here —
verifying a whole forced multi-move sequence ("cooking"), not just the
first move — which would require puzzles to become multi-move sequences
rather than single moves. That's a bigger change, deferred for now.

## v28 — Added a real FIDE Tournament Performance Rating, by time control

You already had opponent ratings stored for 301/303 analyzed games, going
unused for anything beyond display. Added the actual FIDE Tournament
Performance Rating formula — not an approximation of it: average
opponent rating plus a score-based adjustment read from FIDE's own
official table
([Rating Regulations §8.1](https://handbook.fide.com/chapter/B022022)),
interpolating between the table's 1% steps for scores that don't land on
one exactly. A new Insights card shows this per time control, treating
all of a profile's games at each speed as one "tournament."

This is a genuinely different measurement from the existing ACPL-based
"estimated rating" on the Game Report page, not a replacement for it:
that one asks "what rating does this game's move quality resemble,"
independent of who you actually played or the result; this one asks
"what would your rating be, given who you actually played and how you
scored against them" — the real, standard definition of a performance
rating, using data already sitting in the database.

## v27 — Game-phase (opening/middlegame/endgame) detection is now Lichess's own algorithm

The old phase detector used a flat "move <= 10 is opening" cutoff, and
called it endgame only once *every* remaining piece — pawns included —
dropped under 7. That pawn-counting bug meant a real piece-down endgame
with most pawns still on the board never got tagged "endgame" at all
(confirmed against the real database: endgame-tagged moves jumped from a
handful to 31% of all moves once fixed). Ported Lichess's own Divider
algorithm instead — a positional detector computed once per game from the
actual sequence of positions, not a fixed move count:
[Divider.scala](https://github.com/lichess-org/scalachess/blob/master/core/src/main/scala/Divider.scala).
Middlegame starts at the first ply where material has thinned to 10 or
fewer majors/minors (pawns correctly excluded this time), back ranks have
emptied out, or a "mixedness" score (how contested/interpenetrated the
two sides' pieces are, scored region by region) crosses a threshold;
endgame starts at the first *later* ply with 6 or fewer majors/minors.

Phase is now computed once in `analyze_game_moves` (analysis.py) instead
of being re-derived independently in three different places (mistakes.py,
game_report.py, manual_analysis.py) from a move's own move_number/piece
count — those now just read the value computed upstream. A migration
recomputed phase for every already-analyzed game by replaying its stored
PGN (no Stockfish needed, since phase depends only on piece positions).

## v26 — Opening Explorer: filter the Lichess community reference by rating

Comparing your own moves against the *entire* Lichess player base (total
beginners through super-GMs, all lumped into one aggregate) is a lot less
useful than comparing against players near your own strength. Added a
rating filter to the community panel — confirmed against Lichess's own
published OpenAPI spec for the exact band boundaries it supports (0, 1000,
1200, 1400, 1600, 1800, 2000, 2200, 2500 — each meaning "this band and
every stronger one," not an isolated range, since that's the only
grouping the API itself offers). Selection persists across visits.
Skipped a "masters database" toggle (a second, separate Lichess dataset of
curated top-level games) as out of scope for this pass.

## v25 — Fixed the Opening Explorer's silently-broken Lichess community data

Investigated the existing Opening Explorer (Advanced features, Section 2 —
already a real feature, using Lichess's public API for community
reference stats alongside the player's own game history) and found its
"Lichess community" panel has been silently returning "unavailable" for
every position: `explorer.lichess.org` now returns a bare 401
("Authorization Required") to anonymous requests, confirmed live against
the real API and against a live Lichess forum report of the same change —
not a bug introduced by this project, but a Lichess-side policy change
this project had no way to detect other than actually hitting the API and
reading the response.

Added support for an optional personal Lichess API access token, sent as
a Bearer header — the documented way to authenticate a Lichess API
request, free to generate, no special scope needed. When the community
panel can't load because of this specifically (not a generic network
hiccup), the Explorer page now shows an in-place prompt with a link to
generate a token and a field to save it, instead of a silent, unexplained
"unavailable." Unauthenticated and authenticated results are cached
separately, so adding a token later in the same server session isn't
blocked by an already-cached failed attempt.

## v24 — Move severity/tier classification is now Lichess's own algorithm too

Following v23's accuracy port, this does the same for move-quality
classification (Inaccuracy/Mistake/Blunder, and the finer Best/Excellent/
Good bands): `mistakes.py` used flat centipawn thresholds (100/200/400cp),
which don't distinguish a real swing from one that doesn't change who's
winning — the same class of problem the missed-mate bug (v20) fixed a
cruder, narrower version of. Ported Lichess's own move-judgment algorithm
directly:
[Advice.scala](https://github.com/lichess-org/lila/blob/master/modules/tree/src/main/Advice.scala) —
win-probability-based thresholds for the ordinary case (5/10/15 percentage
points of win% lost), plus a special case for a move that specifically
creates a forced mate against the mover or loses one they already had,
graded by the eval right before/after that specific transition rather than
the win%-loss (which is already saturated for any mate-scale eval).

`eval_drop` itself is unchanged — still a plain, magnitude-capped
centipawn value for ACPL/rating purposes; only severity and tier now
derive from win%, computed separately from the stored eval_before/eval_
after. A migration backfilled every already-analyzed game's stored
severity/tier from data on disk (no Stockfish re-run): roughly half of
existing flagged mistakes changed severity, ~12% no longer qualified as a
mistake at all, and about 29% of every move's finer-grained tier changed.
Games whose enriched Game Report classification (Brilliant/Great/Miss)
depended on the old tier had that cache cleared to recompute on next
view, rather than re-running the expensive Stockfish pass for every
affected game up front.

Also fixed a second copy of the exact missed-mate bug the migration
above addresses for saved games: the Analyze board's one-off "paste a
PGN without saving" path (`manual_analysis._graded_moves`) had its own,
separate, still-uncapped `eval_before - eval_after`, never touched by the
original fix since it's a different code path.

## v23 — Accuracy is now a direct port of Lichess's own algorithm

Reported live: this project's accuracy consistently read higher than
chess.com's own reported accuracy for the same games. Investigated with
real data — matched 22 of this account's actual games against chess.com's
own API (which returns each side's accuracy for games it has Game Review
data for) — confirming the gap: our old ACPL-exponential-decay formula
read a mean of +3.9 points higher (up to +9.6 in one game).

Tried several from-scratch fixes (win-probability-weighted ACPL, a
reverse-engineered CAPS-style per-move curve, both arithmetic- and
harmonic-mean aggregation, volatility weighting) — none beat the original
formula's own closeness to chess.com's numbers when tested against the
same 22 games. chess.com's exact CAPS2 formula is undisclosed and
explicitly rating-calibrated (per their own support docs), so there's no
way to reproduce it exactly. Lichess, unlike chess.com, publishes its
real implementation — so rather than continuing to guess, this ports it
directly:
[AccuracyPercent.scala](https://github.com/lichess-org/lila/blob/master/modules/analyse/src/main/AccuracyPercent.scala),
[eval.scala](https://github.com/lichess-org/scalachess/blob/master/core/src/main/scala/eval.scala),
[Maths.scala](https://github.com/lichess-org/scalalib/blob/master/lila/src/main/scala/Maths.scala).

The ported algorithm: each move's accuracy comes from its win-percentage
loss (not raw centipawns) through Lichess's own exponential curve, and a
whole-game score is the mean of a volatility-weighted mean and a harmonic
mean of those per-move accuracies — a real blunder in a sharp, contested
position counts more than the same swing in an already-decided one, and
isn't diluted away by the rest of an otherwise-clean game the way a plain
average allows. Validated against the same 22 real games (mean diff +5.5,
in the same ballpark as the original formula — chess.com's own accuracy
differs from Lichess's own accuracy for the same game too, by design,
since they're different sites' independent methodologies).

`compute_game_acpl` (the Insights "average centipawn loss" card) is
unchanged — it's deliberately still a plain, literal average, which is
the whole point of that card; only `compute_game_accuracy` (Dashboard,
Insights, Game Report, alerts) adopts the new algorithm. The Game Report's
phase/overall accuracy now needs the *unfiltered*, both-colors move list
(the volatility window looks across the whole game), so
`generate_game_report` was restructured accordingly.

## v22 — Analyze board is now profile-aware

Reported live: a second profile's ("evelyn") recent games didn't show up
on the Analyze board at all. Cause: "My recent games" queried `/api/search`
with no `profile_id`, always returning the 8 most recent games across
*every* profile combined — a profile with less-recent activity than
another profile on the same install could get crowded out entirely, or
its games could show up mixed in with someone else's. The Analyze board
had no profile switcher of its own (a gap called out directly in
`profiles.py`'s own docstring), so there was no way to ask for one
profile's games specifically.

Added a profile dropdown to the Analyze board, reusing the Dashboard's
existing `/api/profiles` + localStorage pattern (same `profileId` key, so
picking a profile on the Dashboard carries over here too) — "My recent
games" now filters to the selected profile, and manually-saved PGNs are
tagged to it instead of always landing under whichever profile was
created first (`profiles.default_profile_id()`), the previous behavior
for a page with no switcher. `/api/analyze/pgn`'s save path validates the
given `profile_id` through the same unowned-or-mine access check every
other profile-scoped route already uses.

## v21 — Estimated rating now adjusts for time control, plus a USCF figure

The Game Report's estimated rating fed a game's raw ACPL through a single
curve regardless of time control, even though the same player's ACPL runs
measurably higher at faster time controls purely from time pressure, not
weaker play — a bullet game and a classical game of identical true
strength could show noticeably different estimates. `game_report.py` now
divides ACPL by a per-time-control factor (bullet 1.5x, blitz 1.25x,
rapid 1.1x, classical/daily unadjusted) before the rating lookup, so
faster games get credit for that extra noise; unrecognized time controls
fall back to no adjustment. Also added a rough USCF-equivalent figure
alongside the existing estimate (flat -100 offset, reflecting the
commonly-cited gap between USCF and FIDE/online ratings for comparable
players), shown as a sub-line under "Estimated rating" on the Game Report
and folded into its summary sentence. Like the rest of this feature, both
adjustments are documented as rough, by-eye approximations, not fitted
against real rating data. A migration (schema v18) recomputed every
already-cached report's rating and summary from data already on disk —
no Stockfish re-run needed.

## v20 — Fixed missed-mate blunders, Explore-board freeze, eval bar, and autoscroll

**Fixed a real bug reported live: moves that missed a faster mate showed
up as blunders losing thousands of centipawns.** `analysis.py` represents
forced mate as roughly ±`MATE_SCORE_CP` (10000) so eval comparisons don't
need special-case mate handling — but `mistakes.py` diffed `eval_before`/
`eval_after` raw, so a move that found a *slower* mate than the fastest
one available (still completely winning either way) could carry a "drop"
in the thousands of centipawns and get flagged as a blunder for a
position whose practical outcome never changed. This was the same root
cause as the accuracy-cratering bug fixed in v18, but affecting the
actual severity/tier classification pipeline, not just the aggregate
accuracy stat. Fixed by capping `eval_before`/`eval_after` to
`stats.ACCURACY_EVAL_CAP_CP` *before* differencing, applied once at the
point `eval_drop` is first computed (`stats.capped_eval_drop`) — every
downstream consumer (severity, tier, accuracy, ACPL, the Game Report)
now reads that already-capped value instead of re-deriving it. Added a
migration (schema v17) that re-derives `eval_drop`/`severity`/`tier` for
every already-analyzed game from the raw evals already on disk, no
Stockfish re-run needed — mistakes that no longer clear the inaccuracy
bar (and their generated puzzles/practice history) are removed; real
winning-to-losing blunders are untouched.

**Fixed the Explore board (click-to-move analysis on the Game Review
page) freezing at checkmate/stalemate.** Selecting the only remaining
piece at a game-over position showed a highlighted square with no legal
destinations and no explanation — clicking it again just re-selected the
same dead end, which is exactly what "freezes up... until I press back
to game" described. Now shows a clear "Game over — no legal moves" note
instead. Also added a visible "Analyzing…" state (dimmed board, cursor:
wait) while waiting on the engine after each move — previously a several-
second Stockfish call gave zero feedback, which read as another kind of
freeze.

**Fixed the eval bar rendering upside-down when viewing a game as
Black.** The bar's white-colored fill always grew from the bottom
regardless of board orientation, so on a Black-oriented board (Black's
pieces drawn at the bottom) a White advantage visually stacked against
Black's own side. The fill percentage was already correct — only the
anchor edge needed to flip with the board's orientation, so it now grows
from the top when viewing as Black.

**Eliminated the move-list's page-level autoscroll entirely.** v19
bounded `.table-scroll`'s height, but `scrollIntoView()` still walked
every scrollable ancestor including the window, so the page kept
nudging on every single move-advance. Replaced it with a scroll function
that only ever sets the `.table-scroll` container's own `scrollTop`
(measured via `getBoundingClientRect()` deltas, so it can't touch
`window.scrollY`), on both the Game Review and Analyze pages.

**The Game Review timeline now only tags your own moves.** Inaccuracy/
mistake/blunder/miss tags and row highlighting previously showed for
both players; since the page is about reviewing your own play, the
opponent's moves no longer carry a severity or tier tag.

## v19 — Fetch your own games from the Analyze board; fixed runaway move-list scroll

**"My recent games" panel on the Analyze board.** Previously the only way
in was pasting a PGN/FEN by hand, even for a game already synced from
Lichess/Chess.com. Added a panel above the paste box listing the 8 most
recent analyzed games (reusing `/api/search`'s existing sort+limit —
no new backend endpoint needed), each opening the full Game Review page
(`/game?id=…`, which already has everything the one-off paste flow does
plus persistent notes and cached analysis) instead of re-running a
one-off Stockfish pass. A "🔄 Refresh from Lichess/Chess.com" button
reuses the Dashboard's existing `/api/refresh` + `/api/refresh/status`
background fetch-and-analyze pattern (usernames are still configured on
the Dashboard, not duplicated here) — verified against the real
accounts end-to-end: 15 new games fetched, analyzed, and appearing in
the list.

**Fixed a real bug reported live: stepping through a long game's moves
scrolled the whole page far away from the board.** `.table-scroll` (the
move-list wrapper on both the Analyze board and the Game Review page)
had no height limit at all, so a long game's table just kept growing —
and `goToPly()`'s `scrollIntoView()` call on the active row had nothing
to scroll *except* the whole page, which could mean scrolling thousands
of pixels for a move near the end of a 100+ move game. Gave `.table-scroll`
a 680px max-height with its own vertical scroll (matching the board's
own footprint) and a sticky header, on both pages. Verified on a real
149-move game: the outer page now scrolls a small, roughly constant
amount regardless of which move is selected (was growing unbounded with
game length before), while the move list itself does the rest of the
scrolling internally.

## v18 — Average centipawn loss added as its own metric, by time control

Insights had accuracy by time control but not the raw ACPL it's derived
from — some players find a plain "50 centipawns lost per move" more
legible than a percentage run through an exponential decay curve.
Refactored `compute_game_accuracy()` to be `compute_game_accuracy =
f(compute_game_acpl())` instead of duplicating the capped-ACPL averaging
logic inline — `compute_game_acpl()` is now its own function (same
mate-swing capping and 2-move minimum as accuracy, so this doesn't
reintroduce either bug for the new metric), and `game_report._acpl()`'s
docstring/behavior is unchanged. Extracted the shared "group games by
time control, average a per-game Python metric" logic from
`accuracy_by_time_control()` into `_per_game_metric_by_time_control()`,
reused by the new `acpl_by_time_control()` — both now share one
implementation instead of two near-identical copies.

New "Average centipawn loss by time control" card on Insights, added to
both `/api/insights` and `/api/export/stats`. Unlike every other bar on
that page, lower is better here; the bar width is scaled against a
150cp reference (chosen from this project's own data, where real values
run 55-66cp) rather than a natural 0-100 ceiling.

## v17 — Interactive live analysis on every board; accuracy minimum-sample-size fix

**Extended the Analyze board's click-to-move + live Stockfish re-analysis
everywhere else there's a board:**
- `game.html`: the main game-review board is now fully interactive — click
  a piece at any ply to branch into your own line ("Explore"), with the
  engine re-analyzing live after each move (new eval bar). "Back to game"
  and "Undo" return to the real move sequence; any real-game navigation
  (move-list click, arrow keys, first/prev/next/last) exits exploration
  automatically so the two modes never fight over what's on the board.
  Also added a new eval bar (this page never had one) and fixed a real
  pre-existing bug found along the way: `.hidden` was used throughout via
  `classList.toggle` (`#clock-card`, `#critical-card`, `#share-card`, the
  tablebase link) but the CSS rule itself was never defined, so those
  sections were always visible regardless of the toggle.
- `explorer.html`: added a live eval bar next to the opening-database
  board — previously it showed community/personal move stats with no
  sense of whether the position was actually good. Fetches
  `/api/analyze/fen` in parallel with the existing explorer stats
  (request-id guarded the same way the existing loader already is, so
  clicking through several moves quickly can't show a stale eval).
- `puzzles.html` (main queue) and `endgame.html`: once a puzzle/position
  is solved, the board stays interactive — keep exploring with live
  engine feedback instead of the puzzle's own single graded-answer check,
  via the same Undo/Reset pattern. Puzzle Rush and the opening-puzzle
  trainer were deliberately left as pure solving flows (rush is
  speed-focused; opening-puzzle checks one specific developing move) —
  bolting free exploration onto either risked interfering with their
  correctness-checking mechanic for comparatively little benefit.

**Accuracy: fixed a minimum-sample-size gap, found while re-verifying the
v13 fix against the full dataset.** `compute_game_accuracy()` (and
`game_report._acpl()`) returned a score for as few as one analyzed move.
Confirmed against real data: two games in this project's own dataset
were won by the opponent abandoning/timing out right after the opening
("1. e4 c5") — a single book move with ~0 eval_drop scored a meaningless
100% "accuracy" for a game with no real play. Both now require at least
2 moves with a known eval, returning None (already the existing "not
enough data" convention) otherwise. Checked the full 275-analyzed-game
dataset first to confirm the threshold: nothing had exactly 2 or 3 own
moves, so this excludes only the two 1-move degenerate cases — a
genuinely short 4-move Scholar's-mate loss elsewhere in the same dataset
still scores normally.

## v16 — Rolled out the icy-blue redesign to the remaining 10 pages

Extends v15's `analyze.html` checkpoint (approved after review) to every
other page: `index`, `game`, `puzzles`, `explorer`, `endgame`, `insights`,
`clock`, `progress`, `search`, `profiles`.

All 10 get the muted icy-blue palette (both dark/default and light theme
variants) and the wide desktop layout (`.page` from its old 900-1000px
cap to 1440px). The 4 board pages (`game`, `puzzles`, `explorer`,
`endgame`) additionally get the full board treatment from v15: the board
column widened from a 420-480px cap to 680px, the real cburnett piece set
(replacing colorless system-font glyphs), and matching `--board-light`/
`--board-dark` colors. `puzzles.html` needed one extra fix along the way:
its pieces render inside a `<span class="piece">` wrapper (for a z-index
rule), and a plain inline span has no intrinsic size for the new SVG
icon's percentage-based sizing to resolve against — fixed by giving
`.square .piece` an explicit 82%×82% box.

Caught and fixed while double-checking every page: `clock.html` had its
own standalone `--tier-excellent` variable (colors a move-quality point
on its clock-usage chart) that a batch script over-generalized from the
4 board pages' variable set and silently dropped, which would have been
the third instance this app has hit of the "undefined CSS variable →
invisible UI" bug class. Caught by diffing removed variable names against
each file's own actual usage before treating the rollout as done, not
just by spot-checking a few pages.

## v15 — Analyze board redesign: real pieces, wide desktop layout, fully interactive

First checkpoint of the icy-blue redesign, built on `analyze.html` for
review before rolling out to the other 10 pages.

**Real piece graphics.** Pieces had no color rule at all — white
(hollow-glyph) and black (filled-glyph) pieces both inherited the same
single text color, differentiated only by that subtle shape, in a system
font that renders inconsistently across platforms to begin with. A first
pass at hand-drawn geometric SVG replacements didn't look right either
(no way to visually iterate on hand-authored bezier paths blind). Replaced
both with the standard "cburnett" Staunton piece set (Colin M. L. Burnett,
CC BY-SA 3.0 / GFDL — the same set Lichess and Wikipedia use by default),
pulled in as real, valid SVG path data rather than reconstructed from
memory, and referenced via `<use>` so all 12 pieces live in one `<defs>`
block instead of 12 separate assets.

**Wide desktop layout.** `.page` was capped at 920px and the board/sidebar
grid stacked the board at a 460px cap — a mobile-oriented layout that left
most of a real monitor empty. Widened to a 1440px page and a 680px board
column (Chess.com-sized), still collapsing to one column under ~1080px.

**Icy blue palette**, retuned twice: an initial pass came out too
saturated/"artificial"; repicked as a quieter, more desaturated
"overcast sky" family for both the dark (default) and light theme
variants, plus the board's own light/dark square colors (fixed regardless
of site theme, like a physical board's colors would be).

**Fully interactive board with live Stockfish re-analysis** — the biggest
functional change. Previously the Position/FEN board could only preview
one suggested engine line one ply deep; now you can click a piece, see
its legal destinations highlighted, and click one to actually play it.
Each move calls a new endpoint (`POST /api/analyze/move`, backed by
`manual_analysis.apply_move` — pure python-chess validation, not
Stockfish, so it isn't behind the analysis rate limit) that returns the
resulting FEN server-side ("server does chess logic, client only renders
a FEN it's given," same split as everywhere else in the app), then the
client automatically re-calls the existing `/api/analyze/fen` for fresh
top-line analysis of the new position. Added Undo/Reset (standard
undo/redo semantics — playing a move after undoing discards the redone
branch) and `legal_moves` to `analyze_fen`'s response (reusing
`puzzles.legal_moves_for_fen`) to power the destination highlighting.
Board orientation is fixed for the session from the pasted position's
side to move, rather than flipping every time the turn alternates.

Worth remembering for the remaining pages: an earlier color-only piece
attempt (before switching to cburnett) hit a real CSS trap — descendant
selectors like `.piece-white .piece-shape` can never match a `<use>`'s
referenced content, since it isn't a DOM descendant of the `<use>` for
selector-matching purposes (only inherited property *values* flow through
the reference). Moot now that cburnett's colors are baked into the paths,
but worth knowing before reaching for currentColor-based SVG icons again.

## v14 — Fixed Chess.com "Daily" mislabeled as "Classical"; rating chart gets a dropdown

Spotted while reviewing the v13 rating chart: a game showed up as "Chess.com
Classical" with 100% accuracy, despite the account never having played a
real-time classical game. Root cause: `fetchers._classify_chesscom_time_
control()` mapped Chess.com's `"daily"` time class (correspondence —
days per move, played over weeks) straight to `"classical"`. Those are
different formats — Lichess's own `"classical"` bucket (see
`_classify_time_control_from_clock`) is a genuine single-sitting, real-time
game — so a correspondence game was silently passing itself off as a
90-minute one everywhere time_control is grouped (Insights, Search).
`"daily"` is now kept as its own bucket; the one pre-existing mislabeled
row was backfilled. Added to the Search filter dropdown.

The v13 rating-over-time chart's (source, time control) split fixed the
"broken" mixing, but showing every line at once (7 in this account's data)
was cluttered. Replaced the always-on multi-line view with a dropdown:
one series shown at a time by default (the most-played), with an "All"
option for the previous overview. Selection persists across reloads.

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

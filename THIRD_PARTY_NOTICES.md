# Third-party notices

Chesswise itself is MIT-licensed (see [LICENSE](LICENSE)). It vendors or depends
on the following open-source work.

## Vendored into the repository

### chess.js — BSD-2-Clause
`app/static/vendor/chess.js` (version 0.13.4, unmodified) — legal-move
generation, check detection and SAN for the interactive board.
Copyright (c) 2022, Jeff Hlywa. <https://github.com/jhlywa/chess.js>
The full license text is the header of the vendored file.

### "cburnett" chess piece set — CC BY-SA 3.0 / GFDL / BSD / GPL (multi-licensed)
`app/static/js/pieces.js` — the standard Staunton piece artwork by Colin M. L.
Burnett, the same set Lichess and Wikipedia use by default.
<https://commons.wikimedia.org/wiki/Category:SVG_chess_pieces>

## Installed via pip (see requirements.txt)

- **python-chess** (GPL-3.0) — board, move and PGN handling, engine protocol.
- **FastAPI / Starlette / uvicorn** (MIT / BSD) — web server.
- **requests** (Apache-2.0), **python-dotenv** (BSD-3), and the other packages in
  `requirements.txt`.

## External services and data (not bundled)

- **Stockfish** (GPL-3.0) — installed separately; run as a subprocess.
- **Lichess** public APIs and the `lichess-org/chess-openings` dataset (CC0) —
  opening names, tablebase lookups, opening explorer, puzzle database.
- **Chess.com** public API — game history.

## Algorithms ported from open source

Documented in the source files that use them, each with a link to the original:
Lichess's accuracy and move-judgment formulas (`stats.py`, `mistakes.py`),
Stockfish's win-rate model (`static/js/ui.js`), Lichess's game-phase Divider
(`analysis.py`), and Static Exchange Evaluation (`game_report.py`).

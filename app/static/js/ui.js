// Small shared helpers for the pages that use the interactive board:
// eval bar, eval formatting, engine-line formatting, move-classification styles.

import { Chess } from '/static/vendor/chess.js';

export function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Move classifications, in the order chess.com lists them. Colors are close to
// chess.com's own so the review reads the same way at a glance.
export const CLASS_ORDER = ['brilliant', 'great', 'best', 'excellent', 'good', 'book', 'inaccuracy', 'mistake', 'miss', 'blunder'];
export const CLASS_STYLE = {
  brilliant: { label: '!!', color: '#1baca6', name: 'Brilliant', blurb: 'The best move — and tricky to find' },
  great: { label: '!', color: '#5c8bb0', name: 'Great', blurb: 'A move that changed the course of the game' },
  best: { label: '★', color: '#81b64c', name: 'Best', blurb: "The engine's top choice" },
  excellent: { label: '✓', color: '#96bc4b', name: 'Excellent', blurb: 'Almost as good as the best move' },
  good: { label: '✓', color: '#a5b98a', name: 'Good', blurb: 'A decent move, but not the best' },
  book: { label: 'B', color: '#a88865', name: 'Book', blurb: 'A conventional opening move' },
  inaccuracy: { label: '?!', color: '#f0c33d', name: 'Inaccuracy', blurb: 'A weak move' },
  mistake: { label: '?', color: '#f2a04e', name: 'Mistake', blurb: 'A bad move that worsens your position' },
  miss: { label: '✗', color: '#ee6b5b', name: 'Miss', blurb: 'A move that missed a tactical opportunity' },
  blunder: { label: '??', color: '#ca3431', name: 'Blunder', blurb: 'A very bad move that loses material or the game' },
};

export function formatEval(cp, mate) {
  if (mate != null) return `${mate > 0 ? '' : '-'}M${Math.abs(mate)}`;
  if (cp == null) return '';
  if (Math.abs(cp) >= 9000) return `${cp > 0 ? '+' : '-'}M`;
  const pawns = (cp / 100).toFixed(2);
  return cp > 0 ? `+${pawns}` : pawns;
}

function materialCount(fen) {
  const values = { p: 1, n: 3, b: 3, r: 5, q: 9 };
  let total = 0;
  for (const ch of fen.split(' ')[0]) total += values[ch.toLowerCase()] || 0;
  return total;
}

// Stockfish's own win-rate model (official-stockfish/WDL_model): the share of
// the board an eval bar fills is the position's expected score, which depends
// on how much material is left, not just the raw centipawn number.
export function expectedScorePercent(cp, fen) {
  const m = Math.max(17, Math.min(78, materialCount(fen))) / 58;
  const AS = [-72.32565836, 185.93832038, -144.58862193, 416.44950446];
  const BS = [83.86794042, -136.06112997, 69.98820887, 47.62901433];
  const a = ((AS[0] * m + AS[1]) * m + AS[2]) * m + AS[3];
  const b = ((BS[0] * m + BS[1]) * m + BS[2]) * m + BS[3];
  const v = (cp * a) / 100;
  const winRate = (x) => 1 / (1 + Math.exp((a - x) / b));
  const w = winRate(v);
  const l = winRate(-v);
  return (w + (1 - w - l) / 2) * 100;
}

// Lichess's own win% curve (used by the accuracy formula), for the eval graph.
export function winPercent(cp) {
  const capped = Math.max(-1000, Math.min(1000, cp));
  return 100 / (1 + Math.exp(-0.00368208 * capped));
}

// Plays `uciMoves` from `fen` and returns SAN strings with move numbers, e.g.
// "12. Nf3 Nc6 13. Bb5". Stops quietly at the first illegal move.
export function formatPv(fen, uciMoves, maxPlies = 10) {
  const game = new Chess();
  if (!game.load(fen)) return '';
  const parts = fen.split(' ');
  let moveNo = parseInt(parts[5], 10) || 1;
  let white = parts[1] !== 'b';
  const out = [];
  for (const uci of (uciMoves || []).slice(0, maxPlies)) {
    const m = game.move({ from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci[4] || undefined });
    if (!m) break;
    if (white) out.push(`${moveNo}.`); else if (!out.length) out.push(`${moveNo}...`);
    out.push(m.san);
    if (!white) moveNo += 1;
    white = !white;
  }
  return out.join(' ');
}

// The move (uci + san) that turns position `fenA` into `fenB`, or null. Used
// when the server hands back "the position after the opponent replied" and
// the board wants to animate and highlight that reply.
export function findMoveBetween(fenA, fenB) {
  const game = new Chess();
  if (!game.load(fenA)) return null;
  const target = fenB.split(' ').slice(0, 4).join(' ');
  for (const m of game.moves({ verbose: true })) {
    game.move(m);
    const same = game.fen().split(' ').slice(0, 4).join(' ') === target;
    game.undo();
    if (same) return { from: m.from, to: m.to, san: m.san, uci: m.from + m.to + (m.promotion || '') };
  }
  return null;
}

const EVALBAR_CSS = `
.cw-evalbar { position: relative; width: 26px; flex-shrink: 0; align-self: stretch; border-radius: 5px; overflow: hidden;
  background: #403d39; border: 1px solid var(--border, rgba(255,255,255,.14)); }
.cw-evalbar-fill { position: absolute; left: 0; right: 0; background: #f4f4f4; transition: height .3s ease; }
.cw-evalbar-label { position: absolute; left: 0; right: 0; text-align: center; font: 700 10px/1 system-ui, sans-serif;
  padding: 3px 0; font-variant-numeric: tabular-nums; white-space: nowrap; }
`;

export class EvalBar {
  constructor(container, { orientation = 'white' } = {}) {
    if (!document.getElementById('cw-evalbar-style')) {
      const style = document.createElement('style');
      style.id = 'cw-evalbar-style';
      style.textContent = EVALBAR_CSS;
      document.head.appendChild(style);
    }
    this.orientation = orientation;
    this.root = document.createElement('div');
    this.root.className = 'cw-evalbar';
    this.fill = document.createElement('div');
    this.fill.className = 'cw-evalbar-fill';
    this.label = document.createElement('span');
    this.label.className = 'cw-evalbar-label';
    this.root.append(this.fill, this.label);
    container.appendChild(this.root);
    this.whiteShare = 50;
    this._render('');
  }

  setOrientation(orientation) {
    this.orientation = orientation;
    this._render(this.label.textContent);
  }

  // `cp`/`mate` are from White's point of view (positive = White better).
  update({ cp = 0, mate = null, fen = null } = {}) {
    if (mate != null) this.whiteShare = mate > 0 ? 100 : 0;
    else this.whiteShare = fen ? expectedScorePercent(cp, fen) : winPercent(cp);
    this._render(formatEval(cp, mate));
  }

  clear() { this.whiteShare = 50; this._render(''); }

  _render(text) {
    const whiteAtBottom = this.orientation === 'white';
    this.fill.style.height = `${this.whiteShare}%`;
    this.fill.style.top = whiteAtBottom ? 'auto' : '0';
    this.fill.style.bottom = whiteAtBottom ? '0' : 'auto';
    this.label.textContent = text;
    const whiteLeads = this.whiteShare >= 50;
    // The number sits on the leading side's end of the bar, in whichever
    // color reads against that side.
    const atBottom = whiteLeads === whiteAtBottom;
    this.label.style.top = atBottom ? 'auto' : '0';
    this.label.style.bottom = atBottom ? '0' : 'auto';
    this.label.style.color = whiteLeads ? '#403d39' : '#f4f4f4';
  }
}

// Shared interactive chess board used by every page that shows a position.
//
// Legal-move generation, check detection and SAN come from chess.js
// (BSD-2-Clause, vendored in ../vendor/chess.js) so dragging, click-to-move,
// castling, en passant and promotion all work client-side with no server
// round trip. Everything else — rendering, drag and drop, arrows, badges,
// animation — is this file. Pieces are absolutely-positioned elements (not
// grid cells) so moves glide and a dragged piece can follow the pointer.

import { Chess } from '/static/vendor/chess.js';
import { PIECE_SPRITE } from '/static/js/pieces.js';

export const START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
const FILES = 'abcdefgh';

const CSS = `
.cwb { position: relative; width: 100%; aspect-ratio: 1 / 1; container-type: inline-size;
  user-select: none; -webkit-user-select: none; touch-action: none; overflow: hidden;
  border-radius: 6px; background: var(--board-dark, #7fa3bd); }
.cwb-sq { position: absolute; width: 12.5%; height: 12.5%; background: var(--board-light, #e2ebf0); }
.cwb-sq.dark { background: var(--board-dark, #7fa3bd); }
.cwb-sq.last::before, .cwb-sq.sel::before { content: ""; position: absolute; inset: 0; pointer-events: none; }
.cwb-sq.last::before { background: var(--cwb-last, rgba(255, 214, 79, 0.45)); }
.cwb-sq.sel::before { background: var(--board-selected, rgba(230, 191, 120, 0.55)); }
.cwb-sq.hint { box-shadow: inset 0 0 0 0.9cqw rgba(126, 200, 80, 0.95); }
.cwb-sq.hover { box-shadow: inset 0 0 0 0.6cqw rgba(255, 255, 255, 0.75); }
.cwb-sq.check { background-image: radial-gradient(ellipse at center, #ff0000 0%, #e70000 25%, rgba(169, 0, 0, 0) 89%, rgba(158, 0, 0, 0) 100%); }
.cwb-sq.dest::after { content: ""; position: absolute; left: 35%; top: 35%; width: 30%; height: 30%; border-radius: 50%;
  background: var(--board-dot, rgba(20, 85, 30, 0.5)); pointer-events: none; }
.cwb-sq.destcap::after { content: ""; position: absolute; inset: 0; border-radius: 50%; pointer-events: none;
  background: radial-gradient(transparent 0%, transparent 79%, var(--board-dot, rgba(20, 85, 30, 0.5)) 80%); }
.cwb-coord { position: absolute; font: 700 1.9cqw/1 system-ui, -apple-system, "Segoe UI", sans-serif; pointer-events: none; opacity: 0.85; z-index: 1; }
.cwb-coord.rank { left: 0.6cqw; top: 0.5cqw; }
.cwb-coord.file { right: 0.6cqw; bottom: 0.4cqw; }
.cwb-sq .cwb-coord { color: var(--board-dark, #7fa3bd); }
.cwb-sq.dark .cwb-coord { color: var(--board-light, #e2ebf0); }
.cwb-piece { position: absolute; width: 12.5%; height: 12.5%; z-index: 2; pointer-events: none;
  transition: left 0.16s ease, top 0.16s ease; will-change: left, top; }
.cwb-piece svg { width: 100%; height: 100%; display: block; overflow: visible; filter: drop-shadow(0 1px 1px rgba(0, 0, 0, 0.35)); }
.cwb-piece.drag { transition: none; z-index: 12; transform: scale(1.12); filter: drop-shadow(0 6px 8px rgba(0, 0, 0, 0.4)); }
.cwb.noanim .cwb-piece { transition: none; }
.cwb-shapes { position: absolute; inset: 0; width: 100%; height: 100%; z-index: 5; pointer-events: none; overflow: visible; }
.cwb-badge { position: absolute; z-index: 6; width: 5.6%; height: 5.6%; border-radius: 50%; display: flex; align-items: center;
  justify-content: center; color: #fff; font: 800 2.9cqw/1 system-ui, -apple-system, "Segoe UI", sans-serif;
  box-shadow: 0 0 0 0.35cqw rgba(255, 255, 255, 0.92), 0 0.4cqw 1cqw rgba(0, 0, 0, 0.35); pointer-events: none; letter-spacing: -0.02em; }
.cwb-promo { position: absolute; inset: 0; z-index: 20; background: rgba(0, 0, 0, 0.45); }
.cwb-promo button { position: absolute; width: 12.5%; height: 12.5%; padding: 0; border: none; cursor: pointer;
  background: #f4f6f8; border-radius: 50%; box-shadow: 0 2px 8px rgba(0, 0, 0, 0.4); }
.cwb-promo button:hover { background: #ffffff; box-shadow: 0 0 0 0.6cqw #7fbf65; }
.cwb-promo button svg { width: 100%; height: 100%; display: block; }
@keyframes cwb-shake { 0%, 100% { transform: translateX(0); } 20% { transform: translateX(-1.4cqw); } 40% { transform: translateX(1.4cqw); }
  60% { transform: translateX(-0.9cqw); } 80% { transform: translateX(0.9cqw); } }
.cwb.shake { animation: cwb-shake 0.35s ease; }
`;

function ensureAssets() {
  if (document.getElementById('cwb-assets')) return;
  const style = document.createElement('style');
  style.id = 'cwb-assets';
  style.textContent = CSS;
  document.head.appendChild(style);
  const holder = document.createElement('div');
  holder.id = 'cwb-sprite';
  holder.style.cssText = 'position:absolute;width:0;height:0;overflow:hidden';
  holder.innerHTML = PIECE_SPRITE;
  document.body.appendChild(holder);
}

export function parseFen(fen) {
  const parts = fen.trim().split(/\s+/);
  const pieces = new Map();
  parts[0].split('/').forEach((row, r) => {
    let file = 0;
    for (const ch of row) {
      if (/\d/.test(ch)) { file += parseInt(ch, 10); continue; }
      const color = ch === ch.toUpperCase() ? 'w' : 'b';
      pieces.set(FILES[file] + (8 - r), color + ch.toUpperCase());
      file += 1;
    }
  });
  return { pieces, turn: parts[1] === 'b' ? 'b' : 'w' };
}

function squareXY(sq, orientation) {
  const file = sq.charCodeAt(0) - 97;
  const rank = parseInt(sq[1], 10);
  return orientation === 'white' ? { x: file, y: 8 - rank } : { x: 7 - file, y: rank - 1 };
}

function xyToSquare(x, y, orientation) {
  const file = orientation === 'white' ? x : 7 - x;
  const rank = orientation === 'white' ? 8 - y : y + 1;
  return FILES[file] + rank;
}

function isLightSquare(sq) {
  return ((sq.charCodeAt(0) - 97) + parseInt(sq[1], 10)) % 2 === 1;
}

function svgEl(tag, attrs) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  return el;
}

function pieceSvg(code) {
  const svg = svgEl('svg', { viewBox: '0 0 45 45' });
  const use = svgEl('use', { href: `#cwb-${code}` });
  svg.appendChild(use);
  return svg;
}

export class Board {
  constructor(container, opts = {}) {
    ensureAssets();
    this.opts = Object.assign({
      orientation: 'white',
      fen: START_FEN,
      interactive: false,
      movableColor: 'turn', // 'white' | 'black' | 'both' | 'turn' (whoever is to move) | null
      showCoords: true,
      animate: true,
      userShapes: true,
      onMove: null,
      onShapes: null,
    }, opts);
    this.container = container;
    this.orientation = this.opts.orientation;
    this.interactive = this.opts.interactive;
    this.movableColor = this.opts.movableColor;
    this.pieces = new Map();
    this.game = new Chess();
    this.fen = START_FEN;
    this.turn = 'w';
    this.lastMove = null;
    this.selected = null;
    this.dests = [];
    this.hintSquares = new Set();
    this.arrows = [];
    this.circles = [];
    this.userShapes = [];
    this.badges = new Map();
    this._drag = null;
    this._shape = null;
    this._hover = null;
    this._destroyed = false;

    this.root = document.createElement('div');
    this.root.className = 'cwb';
    this.squaresLayer = document.createElement('div');
    this.squaresLayer.style.cssText = 'position:absolute;inset:0';
    this.root.appendChild(this.squaresLayer);
    this.squareEls = new Map();
    for (const f of FILES) {
      for (let r = 1; r <= 8; r++) {
        const sq = f + r;
        const el = document.createElement('div');
        el.className = 'cwb-sq ' + (isLightSquare(sq) ? 'light' : 'dark');
        el.dataset.sq = sq;
        this.squareEls.set(sq, el);
        this.squaresLayer.appendChild(el);
      }
    }
    this.piecesLayer = document.createElement('div');
    this.piecesLayer.style.cssText = 'position:absolute;inset:0;pointer-events:none';
    this.root.appendChild(this.piecesLayer);
    this.shapesSvg = svgEl('svg', { class: 'cwb-shapes', viewBox: '0 0 8 8', preserveAspectRatio: 'none' });
    this.root.appendChild(this.shapesSvg);
    this.badgeLayer = document.createElement('div');
    this.badgeLayer.style.cssText = 'position:absolute;inset:0;pointer-events:none;z-index:6';
    this.root.appendChild(this.badgeLayer);
    container.appendChild(this.root);

    this._onDown = (e) => this._pointerDown(e);
    this._onMove = (e) => this._pointerMove(e);
    this._onUp = (e) => this._pointerUp(e);
    this._onKey = (e) => { if (e.key === 'Escape') this._deselect(); };
    this.root.addEventListener('pointerdown', this._onDown);
    this.root.addEventListener('pointermove', this._onMove);
    this.root.addEventListener('pointerup', this._onUp);
    this.root.addEventListener('pointercancel', this._onUp);
    this.root.addEventListener('contextmenu', (e) => e.preventDefault());
    document.addEventListener('keydown', this._onKey);

    this._layoutSquares();
    this.setFen(this.opts.fen, { animate: false });
  }

  destroy() {
    this._destroyed = true;
    document.removeEventListener('keydown', this._onKey);
    this.root.remove();
  }

  // --- public state -------------------------------------------------------

  getFen() { return this.fen; }
  getTurn() { return this.turn === 'w' ? 'white' : 'black'; }
  getPieceAt(sq) { const p = this.pieces.get(sq); return p ? p.code : null; }

  // null while the game is on; otherwise how it ended. `winner` is the side
  // that delivered mate ('white' | 'black'), or null for a draw.
  gameOutcome() {
    if (this.game.in_checkmate()) return { kind: 'checkmate', winner: this.turn === 'w' ? 'black' : 'white' };
    if (this.game.in_stalemate()) return { kind: 'stalemate', winner: null };
    if (this.game.in_draw()) return { kind: 'draw', winner: null };
    return null;
  }

  turnLabel() {
    const side = this.turn === 'w' ? 'White' : 'Black';
    if (this.game.in_checkmate()) return `${side} is checkmated`;
    if (this.game.in_stalemate()) return 'Stalemate';
    if (this.game.in_draw()) return 'Draw';
    return `${side} to move`;
  }

  legalMovesFrom(sq) {
    return this.game.moves({ square: sq, verbose: true });
  }

  setInteractive(on, movableColor) {
    this.interactive = on;
    if (movableColor !== undefined) this.movableColor = movableColor;
    if (!on) this._deselect();
  }

  setOrientation(orientation) {
    if (orientation === this.orientation) return;
    this.orientation = orientation;
    this._layoutSquares();
    for (const [sq, p] of this.pieces) this._place(p.el, sq);
    this._layoutBadges();
    this._drawShapes();
  }

  flip() { this.setOrientation(this.orientation === 'white' ? 'black' : 'white'); }

  // --- position -----------------------------------------------------------

  setFen(fen, { lastMove = null, animate = this.opts.animate, lastMoveColor = null } = {}) {
    const parsed = parseFen(fen);
    this.fen = fen;
    this.turn = parsed.turn;
    const valid = this.game.load(fen);
    if (!valid) this.game = new Chess();
    this._reconcile(parsed.pieces, animate);
    this.lastMove = lastMove ? (Array.isArray(lastMove) ? lastMove : [lastMove.from, lastMove.to]) : null;
    this._lastMoveColor = lastMoveColor;
    this._deselect(false);
    this._paint();
  }

  // Plays a move programmatically (e.g. an engine reply) with animation.
  // `move` is a UCI string ("e2e4", "e7e8q") or a {from,to,promotion} object.
  move(move, { animate = true } = {}) {
    const m = typeof move === 'string'
      ? { from: move.slice(0, 2), to: move.slice(2, 4), promotion: move[4] || undefined }
      : move;
    const fenBefore = this.fen;
    const done = this.game.move({ from: m.from, to: m.to, promotion: m.promotion });
    if (!done) return null;
    const fenAfter = this.game.fen();
    this.setFen(fenAfter, { lastMove: [m.from, m.to], animate });
    return { from: m.from, to: m.to, promotion: done.promotion || null, san: done.san, uci: m.from + m.to + (done.promotion || ''),
      piece: done.piece, color: done.color, captured: done.captured || null, flags: done.flags, fenBefore, fenAfter };
  }

  _reconcile(newMap, animate) {
    if (!animate) this.root.classList.add('noanim');
    const next = new Map();
    const orphans = [];
    for (const [sq, p] of this.pieces) {
      if (newMap.get(sq) === p.code) next.set(sq, p); else orphans.push({ sq, code: p.code, el: p.el });
    }
    for (const [sq, code] of newMap) {
      if (next.has(sq)) continue;
      let bestIdx = -1;
      let bestDist = Infinity;
      orphans.forEach((o, i) => {
        if (o.code !== code) return;
        const d = Math.hypot(o.sq.charCodeAt(0) - sq.charCodeAt(0), parseInt(o.sq[1], 10) - parseInt(sq[1], 10));
        if (d < bestDist) { bestDist = d; bestIdx = i; }
      });
      if (bestIdx >= 0) {
        const o = orphans.splice(bestIdx, 1)[0];
        o.el.classList.remove('drag');
        this._place(o.el, sq);
        next.set(sq, { code, el: o.el });
      } else {
        const el = document.createElement('div');
        el.className = 'cwb-piece';
        el.appendChild(pieceSvg(code));
        this._place(el, sq);
        this.piecesLayer.appendChild(el);
        next.set(sq, { code, el });
      }
    }
    for (const o of orphans) o.el.remove();
    this.pieces = next;
    if (!animate) requestAnimationFrame(() => this.root.classList.remove('noanim'));
  }

  _place(el, sq) {
    const { x, y } = squareXY(sq, this.orientation);
    el.style.left = `${x * 12.5}%`;
    el.style.top = `${y * 12.5}%`;
  }

  _layoutSquares() {
    for (const [sq, el] of this.squareEls) {
      const { x, y } = squareXY(sq, this.orientation);
      el.style.left = `${x * 12.5}%`;
      el.style.top = `${y * 12.5}%`;
      el.querySelectorAll('.cwb-coord').forEach((c) => c.remove());
      if (!this.opts.showCoords) continue;
      if (x === 0) {
        const c = document.createElement('span');
        c.className = 'cwb-coord rank';
        c.textContent = sq[1];
        el.appendChild(c);
      }
      if (y === 7) {
        const c = document.createElement('span');
        c.className = 'cwb-coord file';
        c.textContent = sq[0];
        el.appendChild(c);
      }
    }
  }

  // --- highlights ---------------------------------------------------------

  _paint() {
    const checkSq = this.game.in_check() ? this._kingSquare(this.turn) : null;
    const destSquares = new Map(this.dests.map((m) => [m.to, m]));
    for (const [sq, el] of this.squareEls) {
      const isLast = !!this.lastMove && (this.lastMove[0] === sq || this.lastMove[1] === sq);
      el.classList.toggle('last', isLast);
      if (isLast && this._lastMoveColor) el.style.setProperty('--cwb-last', this._lastMoveColor);
      else el.style.removeProperty('--cwb-last');
      el.classList.toggle('sel', this.selected === sq);
      el.classList.toggle('check', checkSq === sq);
      el.classList.toggle('hint', this.hintSquares.has(sq));
      el.classList.toggle('hover', this._hover === sq);
      const d = destSquares.get(sq);
      el.classList.toggle('dest', !!d && !this.pieces.has(sq) && !(d.flags || '').includes('e'));
      el.classList.toggle('destcap', !!d && (this.pieces.has(sq) || (d.flags || '').includes('e')));
    }
  }

  _kingSquare(turn) {
    const code = turn + 'K';
    for (const [sq, p] of this.pieces) if (p.code === code) return sq;
    return null;
  }

  setLastMove(from, to, color = null) {
    this.lastMove = from && to ? [from, to] : null;
    this._lastMoveColor = color;
    this._paint();
  }

  setHint(squares) {
    this.hintSquares = new Set(squares || []);
    this._paint();
  }

  shake() {
    this.root.classList.remove('shake');
    void this.root.offsetWidth;
    this.root.classList.add('shake');
  }

  // --- badges (move-classification icons on a square) ----------------------

  setBadge(square, spec) {
    this.clearBadges();
    if (!square || !spec) return;
    const el = document.createElement('div');
    el.className = 'cwb-badge';
    el.style.background = spec.color;
    el.textContent = spec.label;
    if (spec.title) el.title = spec.title;
    this.badgeLayer.appendChild(el);
    this.badges.set(square, el);
    this._layoutBadges();
  }

  clearBadges() {
    for (const el of this.badges.values()) el.remove();
    this.badges.clear();
  }

  _layoutBadges() {
    for (const [sq, el] of this.badges) {
      const { x, y } = squareXY(sq, this.orientation);
      // Overlaps the square's top-right corner, but stays inside the board
      // so a badge on the top rank or the h-file isn't clipped.
      el.style.left = `${Math.min(x * 12.5 + 12.5 - 3.2, 100 - 5.6 - 0.6)}%`;
      el.style.top = `${Math.max(y * 12.5 - 2.4, 0.6)}%`;
    }
  }

  // --- arrows and circles ---------------------------------------------------

  setArrows(arrows) { this.arrows = arrows || []; this._drawShapes(); }
  setCircles(circles) { this.circles = circles || []; this._drawShapes(); }
  clearUserShapes() { if (this.userShapes.length) { this.userShapes = []; this._drawShapes(); } }

  _center(sq) {
    const { x, y } = squareXY(sq, this.orientation);
    return { x: x + 0.5, y: y + 0.5 };
  }

  _drawShapes() {
    const svg = this.shapesSvg;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    const all = [
      ...this.circles.map((c) => ({ ...c, kind: 'circle' })),
      ...this.arrows.map((a) => ({ ...a, kind: 'arrow' })),
      ...this.userShapes,
    ];
    for (const s of all) {
      const color = s.color || '#15781b';
      const opacity = s.opacity ?? 0.82;
      if (s.kind === 'circle' || (s.kind === 'user' && s.from === s.to)) {
        const c = this._center(s.sq || s.from);
        svg.appendChild(svgEl('circle', { cx: c.x, cy: c.y, r: 0.44, fill: 'none', stroke: color, 'stroke-width': 0.075, opacity }));
        continue;
      }
      const a = this._center(s.from);
      const b = this._center(s.to);
      const len = Math.hypot(b.x - a.x, b.y - a.y);
      if (!len) continue;
      const ux = (b.x - a.x) / len;
      const uy = (b.y - a.y) / len;
      const width = s.width || 0.15;
      const headLen = width * 2.7;
      const headHalf = width * 1.55;
      const tip = { x: b.x - ux * 0.08, y: b.y - uy * 0.08 };
      const base = { x: tip.x - ux * headLen, y: tip.y - uy * headLen };
      const start = { x: a.x + ux * 0.12, y: a.y + uy * 0.12 };
      const g = svgEl('g', { opacity });
      g.appendChild(svgEl('line', { x1: start.x, y1: start.y, x2: base.x + ux * 0.02, y2: base.y + uy * 0.02,
        stroke: color, 'stroke-width': width, 'stroke-linecap': 'round', ...(s.dashed ? { 'stroke-dasharray': '0.25 0.18' } : {}) }));
      const px = -uy;
      const py = ux;
      g.appendChild(svgEl('polygon', { fill: color, points:
        `${tip.x},${tip.y} ${base.x + px * headHalf},${base.y + py * headHalf} ${base.x - px * headHalf},${base.y - py * headHalf}` }));
      svg.appendChild(g);
    }
  }

  // --- interaction ----------------------------------------------------------

  _eventSquare(e) {
    const r = this.squaresLayer.getBoundingClientRect();
    const x = Math.floor(((e.clientX - r.left) / r.width) * 8);
    const y = Math.floor(((e.clientY - r.top) / r.height) * 8);
    if (x < 0 || x > 7 || y < 0 || y > 7) return null;
    return xyToSquare(x, y, this.orientation);
  }

  _canMovePiece(sq) {
    const p = this.pieces.get(sq);
    if (!p || !this.interactive) return false;
    const color = p.code[0];
    if (color !== this.turn) return false;
    if (this.movableColor === 'both' || this.movableColor === 'turn') return true;
    if (this.movableColor === 'white') return color === 'w';
    if (this.movableColor === 'black') return color === 'b';
    return false;
  }

  _select(sq) {
    this.selected = sq;
    this.dests = this.game.moves({ square: sq, verbose: true });
    this._paint();
  }

  _deselect(repaint = true) {
    this.selected = null;
    this.dests = [];
    this._hover = null;
    if (repaint) this._paint();
  }

  _pointerDown(e) {
    if (this._promoOpen) return;
    const sq = this._eventSquare(e);
    if (e.button === 2) {
      if (!this.opts.userShapes || !sq) return;
      this._shape = { from: sq, pointerId: e.pointerId };
      this.root.setPointerCapture(e.pointerId);
      e.preventDefault();
      return;
    }
    if (e.button !== 0) return;
    if (this.userShapes.length) this.clearUserShapes();
    if (!sq || !this.interactive) return;
    if (this.selected && this.dests.some((m) => m.to === sq)) {
      this._attemptMove(this.selected, sq);
      return;
    }
    if (this._canMovePiece(sq)) {
      const wasSelected = this.selected === sq;
      this._select(sq);
      this._drag = { from: sq, pointerId: e.pointerId, startX: e.clientX, startY: e.clientY, active: false, wasSelected };
      this.root.setPointerCapture(e.pointerId);
      e.preventDefault();
    } else {
      this._deselect();
    }
  }

  _pointerMove(e) {
    if (this._drag && e.pointerId === this._drag.pointerId) {
      const d = this._drag;
      if (!d.active && Math.hypot(e.clientX - d.startX, e.clientY - d.startY) > 4) {
        d.active = true;
        const p = this.pieces.get(d.from);
        if (p) p.el.classList.add('drag');
      }
      if (d.active) {
        const p = this.pieces.get(d.from);
        const r = this.squaresLayer.getBoundingClientRect();
        if (p) {
          p.el.style.left = `${((e.clientX - r.left) / r.width) * 100 - 6.25}%`;
          p.el.style.top = `${((e.clientY - r.top) / r.height) * 100 - 6.25}%`;
        }
        const over = this._eventSquare(e);
        const hover = over && this.dests.some((m) => m.to === over) ? over : null;
        if (hover !== this._hover) { this._hover = hover; this._paint(); }
      }
    }
  }

  _pointerUp(e) {
    if (this._shape && e.pointerId === this._shape.pointerId) {
      const from = this._shape.from;
      const to = this._eventSquare(e);
      this._shape = null;
      if (to) this._toggleUserShape(from, to);
      return;
    }
    if (!this._drag || e.pointerId !== this._drag.pointerId) return;
    const d = this._drag;
    this._drag = null;
    const p = this.pieces.get(d.from);
    if (p) p.el.classList.remove('drag');
    this._hover = null;
    if (d.active) {
      const to = this._eventSquare(e);
      if (to && to !== d.from && this.dests.some((m) => m.to === to)) {
        this._attemptMove(d.from, to);
        return;
      }
      if (p) this._place(p.el, d.from);
      this._paint();
    } else if (d.wasSelected) {
      this._deselect();
    } else {
      this._paint();
    }
  }

  _toggleUserShape(from, to) {
    const key = (s) => `${s.from}-${s.to}`;
    const shape = { kind: 'user', from, to, color: '#15781b' };
    const i = this.userShapes.findIndex((s) => key(s) === key(shape));
    if (i >= 0) this.userShapes.splice(i, 1); else this.userShapes.push(shape);
    this._drawShapes();
    if (this.opts.onShapes) this.opts.onShapes(this.userShapes);
  }

  async _attemptMove(from, to) {
    const candidates = this.dests.filter((m) => m.to === to);
    if (!candidates.length) return;
    let promotion;
    if (candidates.some((m) => m.promotion)) {
      const color = this.pieces.get(from).code[0];
      promotion = await this._askPromotion(to, color);
      if (!promotion) {
        const p = this.pieces.get(from);
        if (p) this._place(p.el, from);
        this._paint();
        return;
      }
    }
    const fenBefore = this.fen;
    const done = this.game.move({ from, to, promotion });
    if (!done) {
      const p = this.pieces.get(from);
      if (p) this._place(p.el, from);
      this._deselect();
      return;
    }
    const fenAfter = this.game.fen();
    this.setFen(fenAfter, { lastMove: [from, to], animate: this.opts.animate });
    if (this.opts.onMove) {
      this.opts.onMove({
        from, to, promotion: done.promotion || null, san: done.san, uci: from + to + (done.promotion || ''),
        piece: done.piece, color: done.color, captured: done.captured || null, flags: done.flags, fenBefore, fenAfter,
      }, this);
    }
  }

  _askPromotion(square, color) {
    return new Promise((resolve) => {
      this._promoOpen = true;
      const overlay = document.createElement('div');
      overlay.className = 'cwb-promo';
      const { x, y } = squareXY(square, this.orientation);
      const down = y === 0;
      ['q', 'n', 'r', 'b'].forEach((piece, i) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.setAttribute('aria-label', `Promote to ${piece}`);
        btn.style.left = `${x * 12.5}%`;
        btn.style.top = `${(down ? i : 7 - i) * 12.5}%`;
        btn.appendChild(pieceSvg(color + piece.toUpperCase()));
        btn.addEventListener('pointerdown', (ev) => ev.stopPropagation());
        btn.addEventListener('click', (ev) => { ev.stopPropagation(); close(piece); });
        overlay.appendChild(btn);
      });
      overlay.addEventListener('pointerdown', (ev) => { ev.stopPropagation(); });
      overlay.addEventListener('click', () => close(null));
      const close = (choice) => {
        this._promoOpen = false;
        overlay.remove();
        resolve(choice);
      };
      this.root.appendChild(overlay);
    });
  }
}

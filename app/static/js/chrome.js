// Page chrome shared by the pages built on the interactive board: header
// with the site nav, light/dark toggle, and a board-color picker. Inject into
// <header id="site-header"> with mountChrome({ title, active }).

const NAV = [
  { href: '/', label: 'Dashboard', key: 'dashboard' },
  { href: '/puzzles', label: 'Puzzles', key: 'puzzles' },
  { label: 'Train', key: 'train', items: [
    { href: '/explorer', label: 'Explorer' }, { href: '/endgame', label: 'Endgame' }, { href: '/analyze', label: 'Analyze' },
  ] },
  { label: 'Insights', key: 'insights', items: [
    { href: '/insights', label: 'Insights', key: 'insights' }, { href: '/clock', label: 'Clock' },
    { href: '/progress', label: 'Progress' }, { href: '/coaching-report', label: 'Coaching Report' },
  ] },
  { href: '/search', label: 'Search', key: 'search' },
];

export const BOARD_THEMES = {
  blue: { name: 'Steel blue', light: '#e2ebf0', dark: '#7fa3bd' },
  green: { name: 'Green', light: '#eeeed2', dark: '#769656' },
  brown: { name: 'Wood', light: '#f0d9b5', dark: '#b58863' },
  gray: { name: 'Slate', light: '#dee3e6', dark: '#8ca2ad' },
};

function safeGet(key, fallback) {
  try { return localStorage.getItem(key) || fallback; } catch (e) { return fallback; }
}
function safeSet(key, value) {
  try { localStorage.setItem(key, value); } catch (e) { /* storage blocked — preference just won't persist */ }
}

export function applyTheme(theme) {
  if (theme === 'system') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', theme);
}

export function applyBoardTheme(name) {
  const t = BOARD_THEMES[name] || BOARD_THEMES.blue;
  document.documentElement.style.setProperty('--board-light', t.light);
  document.documentElement.style.setProperty('--board-dark', t.dark);
}

// Apply saved preferences immediately (before the header exists) so there's
// no flash of the wrong theme while modules load.
applyTheme(safeGet('theme', 'system'));
applyBoardTheme(safeGet('boardTheme', 'blue'));

export function mountChrome({ title, active }) {
  const header = document.getElementById('site-header');
  if (!header) return;
  header.className = 'top';
  const nav = NAV.map((n) => {
    if (!n.items) return `<a href="${n.href}" class="${n.key === active ? 'active' : ''}">${n.label}</a>`;
    const on = n.key === active;
    return `<div class="nav-dropdown">
      <button type="button" class="nav-dropdown-toggle ${on ? 'active-section' : ''}" aria-haspopup="true" aria-expanded="false">${n.label} ▾</button>
      <div class="nav-dropdown-menu">${n.items.map((i) => `<a href="${i.href}" class="${i.key === active ? 'active' : ''}">${i.label}</a>`).join('')}</div>
    </div>`;
  }).join('');
  header.innerHTML = `<h1>${title}</h1>
    <div class="top-right">
      <nav class="top-nav">${nav}</nav>
      <a class="icon-btn" href="/profiles" title="Manage profiles" aria-label="Manage profiles">👤</a>
      <button class="icon-btn" id="board-theme-btn" title="Board colors" aria-label="Change board colors">♟</button>
      <button class="icon-btn" id="theme-toggle" title="Toggle dark mode" aria-label="Toggle dark mode">◐</button>
    </div>`;

  header.querySelector('#theme-toggle').addEventListener('click', () => {
    const current = safeGet('theme', 'system');
    const next = current === 'system' ? 'light' : current === 'light' ? 'dark' : 'system';
    safeSet('theme', next);
    applyTheme(next);
  });
  header.querySelector('#board-theme-btn').addEventListener('click', () => {
    const keys = Object.keys(BOARD_THEMES);
    const next = keys[(keys.indexOf(safeGet('boardTheme', 'blue')) + 1) % keys.length];
    safeSet('boardTheme', next);
    applyBoardTheme(next);
    header.querySelector('#board-theme-btn').title = `Board colors: ${BOARD_THEMES[next].name}`;
  });

  const closeAll = () => document.querySelectorAll('.nav-dropdown.open').forEach((d) => {
    d.classList.remove('open');
    d.querySelector('.nav-dropdown-toggle').setAttribute('aria-expanded', 'false');
  });
  header.querySelectorAll('.nav-dropdown-toggle').forEach((btn) => btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const dropdown = btn.closest('.nav-dropdown');
    const wasOpen = dropdown.classList.contains('open');
    closeAll();
    if (!wasOpen) { dropdown.classList.add('open'); btn.setAttribute('aria-expanded', 'true'); }
  }));
  document.addEventListener('click', closeAll);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeAll(); });
}

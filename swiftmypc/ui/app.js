'use strict';
/* SwiftMyPC interface. Data comes from window.pywebview.api (Python); everything that
   shows data builds DOM nodes with textContent, never innerHTML. */

// Script errors are collected here so they can be inspected while testing.
window.__errors = [];
window.addEventListener('error', (e) => window.__errors.push(String(e.message)));
window.addEventListener('unhandledrejection', (e) => window.__errors.push(String(e.reason && e.reason.message || e.reason)));

// ---------- helpers ----------

const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k === 'style') Object.assign(el.style, v);
    else if (k === 'vars') for (const [n, val] of Object.entries(v)) el.style.setProperty(n, val);
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const ico = (name, size) => window.icon(name, size);

function fmtBytes(n) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (Math.abs(n) >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (i < 2 ? Math.round(n) : n.toFixed(1)) + ' ' + units[i];
}
const pct = (v) => Math.round(v) + '%';
const plural = (n, word, many) => n + ' ' + (n === 1 ? word : (many || word + 's'));

const ACCENTS = ['#6d5cff', '#3d8bff', '#22b8cf', '#2fbf71', '#f2b33d', '#ff7a45', '#ff5d8f'];
function accentFor(name) {
  let x = 0;
  for (const ch of name.toLowerCase()) x = (x * 31 + ch.charCodeAt(0)) >>> 0;
  return ACCENTS[x % ACCENTS.length];
}
function monogram(name) {
  const base = name.replace(/\.(exe|lnk|bat|cmd|vbs)$/i, '').replace(/[^A-Za-z0-9 ]+/g, ' ').trim();
  const words = base.split(/\s+|(?=[A-Z][a-z])/).filter(Boolean);
  if (!words.length) return '?';
  return (words.length > 1 ? words[0][0] + words[1][0] : base.slice(0, 2)).toUpperCase();
}
function appIcon(name, size = 36, accent) {
  return h('span', { class: 'app-icon', style: { width: size + 'px', height: size + 'px', fontSize: Math.round(size * 0.4) + 'px' },
    vars: { '--accent': accent || accentFor(name) } }, h('span', { class: 'mono', text: monogram(name) }));
}
function iconTile(name, accent, size = 44) {
  return h('span', { class: 'app-icon', style: { width: size + 'px', height: size + 'px' }, vars: { '--accent': accent } }, ico(name, Math.round(size * 0.48)));
}
function prettyStartupName(name) {
  return name.replace(/_[0-9A-F]{12,}$/i, '').replace(/(Task)?User[\d.]+$/, '').replace(/([a-z])([A-Z])/g, '$1 $2').trim() || name;
}

let api = null;
async function call(name, ...args) {
  try {
    return await api[name](...args);
  } catch (e) {
    toast(String(e && e.message ? e.message : e), { type: 'error' });
    throw e;
  }
}

// ---------- state ----------

const state = {
  page: 'overview',
  boot: null,
  settings: {},
  snap: null,
  sort: { key: 'mem', desc: true },
  query: '',
  startup: null,
  services: null,
  svcFilter: 'review',
  cleaner: null,
  cleanerSel: {},
  update: null,
  updating: false,
};

const PAGES = {
  overview: { label: 'Overview', icon: 'home' },
  processes: { label: 'Processes', icon: 'activity', search: 'Search running apps…' },
  startup: { label: 'Startup apps', icon: 'rocket', search: 'Search startup apps…' },
  services: { label: 'Services', icon: 'cube', search: 'Search services…' },
  cleaner: { label: 'Cleaner', icon: 'broom' },
  settings: { label: 'Settings', icon: 'gear', foot: true },
};

// ---------- toasts, menus, modals ----------

function toast(message, opts = {}) {
  const type = opts.type || 'ok';
  const icons = { ok: 'check', error: 'alert', info: 'info' };
  const t = h('div', { class: 'toast ' + type, role: 'status' }, ico(icons[type], 15), h('p', { text: message }));
  if (opts.action) t.append(h('button', { class: 'text-btn', text: opts.action.label, onclick: () => { opts.action.fn(); close(); } }));
  $('#toasts').append(t);
  function close() {
    if (!t.isConnected) return;
    t.classList.add('leaving');
    setTimeout(() => t.remove(), 200);
  }
  setTimeout(close, opts.ms || (opts.action ? 7000 : 4200));
  return close;
}

let openMenuEl = null;
function closeMenu() { if (openMenuEl) { openMenuEl.remove(); openMenuEl = null; } }
function openMenu(anchor, items) {
  closeMenu();
  const menu = h('div', { class: 'menu', role: 'menu' });
  for (const it of items) {
    if (it === '-') { menu.append(h('hr')); continue; }
    menu.append(h('button', { class: it.danger ? 'danger' : '', disabled: it.disabled, role: 'menuitem',
      onclick: () => { closeMenu(); it.fn(); } }, ico(it.icon, 16), it.label));
  }
  document.body.append(menu);
  const r = anchor.getBoundingClientRect();
  const w = menu.offsetWidth, hgt = menu.offsetHeight;
  menu.style.left = Math.max(8, Math.min(r.right - w, innerWidth - w - 8)) + 'px';
  menu.style.top = (r.bottom + 6 + hgt > innerHeight ? r.top - hgt - 6 : r.bottom + 6) + 'px';
  openMenuEl = menu;
}
document.addEventListener('mousedown', (e) => { if (openMenuEl && !openMenuEl.contains(e.target)) closeMenu(); });

let modalStack = [];
function openModal({ title, subtitle, lead, leadClass, wide, body, foot, onClose }) {
  const backdrop = h('div', { class: 'backdrop' });
  const head = h('div', { class: 'modal-head' },
    lead ? h('span', { class: 'ico lead ' + (leadClass || '') }, ico(lead, 20)) : null,
    h('div', { class: 'grow' }, h('h2', { text: title }), subtitle ? h('p', { text: subtitle }) : null),
    h('button', { class: 'icon-btn', title: 'Close', onclick: () => m.close() }, ico('x', 18)));
  const bodyEl = h('div', { class: 'modal-body' }, body || null);
  const footEl = h('div', { class: 'modal-foot' }, ...(foot || []));
  const dialog = h('div', { class: 'modal' + (wide ? ' wide' : ''), role: 'dialog', 'aria-modal': 'true' }, head, bodyEl, footEl);
  backdrop.append(dialog);
  const m = {
    el: dialog, body: bodyEl, foot: footEl, busy: false,
    close() {
      if (m.busy || !backdrop.isConnected) return;
      backdrop.remove();
      modalStack = modalStack.filter((x) => x !== m);
      if (onClose) onClose();
    },
  };
  backdrop.addEventListener('mousedown', (e) => { if (e.target === backdrop) m.close(); });
  document.body.append(backdrop);
  modalStack.push(m);
  return m;
}

function confirmModal({ title, text, ok, danger, lead }) {
  return new Promise((resolve) => {
    let answered = false;
    const m = openModal({
      title, lead: lead || (danger ? 'alert' : 'info'), leadClass: danger ? 'danger' : '',
      body: h('p', { text }),
      foot: [
        h('button', { class: 'btn', text: 'Cancel', onclick: () => m.close() }),
        h('button', { class: 'btn ' + (danger ? 'btn-danger' : 'btn-primary'), text: ok, onclick: () => { answered = true; m.close(); resolve(true); } }),
      ],
      onClose: () => { if (!answered) resolve(false); },
    });
  });
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if (openMenuEl) closeMenu();
    else if (modalStack.length) modalStack[modalStack.length - 1].close();
  }
  if (e.ctrlKey && e.key.toLowerCase() === 'f') {
    e.preventDefault();
    if (!$('#search').disabled) $('#search').focus();
  }
});

// ---------- theme ----------

function effectiveDark() {
  const t = state.settings.theme || 'system';
  return t === 'dark' || (t === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
}
function applyTheme() {
  const t = state.settings.theme || 'system';
  if (t === 'system') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', t);
  const btn = $('#theme-btn');
  btn.replaceChildren(ico(effectiveDark() ? 'sun' : 'moon', 18));
  btn.title = effectiveDark() ? 'Switch to light theme' : 'Switch to dark theme';
}
async function setTheme(theme) {
  state.settings = await call('set_setting', 'theme', theme);
  applyTheme();
  if (state.page === 'settings') render();
}
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyTheme);

// ---------- shell: nav, search, page head ----------

function renderNav() {
  const main = $('#nav-main'), foot = $('#nav-foot');
  main.replaceChildren(); foot.replaceChildren();
  for (const [key, p] of Object.entries(PAGES)) {
    const btn = h('button', { class: 'nav-item' + (state.page === key ? ' active' : ''), onclick: () => go(key),
      'aria-current': state.page === key ? 'page' : null },
      ico(p.icon, 18), h('span', { class: 'label', text: p.label }), h('span', { class: 'count', id: 'count-' + key, hidden: true }));
    (p.foot ? foot : main).append(btn);
  }
  updateCounts();
}

function setCount(key, value, cls) {
  const el = document.getElementById('count-' + key);
  if (!el) return;
  el.hidden = value === null || value === undefined;
  if (!el.hidden) { el.textContent = value; el.className = 'count' + (cls ? ' ' + cls : ''); }
}
function updateCounts() {
  if (state.snap) setCount('processes', state.snap.apps.length, 'live');
  if (state.startup) setCount('startup', state.startup.filter((s) => s.enabled).length);
  if (state.services) {
    const n = state.services.filter((s) => s.level).length;
    setCount('services', n || null, n ? 'warn' : '');
  }
}

function setHead(title, sub, actions) {
  $('#page-h1').textContent = title;
  $('#page-sub').textContent = sub || '';
  $('#page-actions').replaceChildren(...(actions || []));
}

function go(page) {
  closeMenu();
  state.page = page;
  state.query = '';
  const search = $('#search');
  search.value = '';
  const placeholder = PAGES[page].search;
  search.disabled = !placeholder;
  search.placeholder = placeholder || 'Search is available on lists';
  $('#search-box').classList.toggle('disabled', !placeholder);
  renderNav();
  const content = $('#content');
  content.scrollTop = 0;
  content.classList.add('intro');
  setTimeout(() => content.classList.remove('intro'), 700);
  render();
}

function render() {
  const content = $('#content');
  ({ overview: renderOverview, processes: renderProcesses, startup: renderStartup, services: renderServices,
     cleaner: renderCleaner, settings: renderSettings })[state.page](content);
}

$('#search').addEventListener('input', (e) => {
  state.query = e.target.value.trim().toLowerCase();
  if (state.page === 'processes') updateProcessList();
  else render();
});

// ---------- overview ----------

const METRICS = [
  { key: 'cpu', title: 'CPU', icon: 'cpu', accent: '#3d8bff' },
  { key: 'mem', title: 'Memory', icon: 'memory', accent: '#6d5cff' },
  { key: 'disk', title: 'System drive', icon: 'disk', accent: '#22b8cf' },
];

function greeting() {
  const hr = new Date().getHours();
  return hr < 12 ? 'Good morning' : hr < 18 ? 'Good afternoon' : 'Good evening';
}
function uptimeText() {
  if (!state.boot) return '';
  const s = Date.now() / 1000 - state.boot.boot_time;
  const d = Math.floor(s / 86400), hr = Math.floor((s % 86400) / 3600);
  return d ? `PC up for ${d}d ${hr}h` : `PC up for ${hr}h ${Math.floor((s % 3600) / 60)}m`;
}

function renderOverview(content) {
  setHead('Overview', `${greeting()} · ${uptimeText()}`, [
    h('button', { class: 'btn', onclick: openHealthCheck }, ico('health', 17), 'Health check'),
    h('button', { class: 'btn btn-primary', onclick: openSmartOptimize }, ico('zap', 17), 'Smart Optimize'),
  ]);

  const metrics = h('div', { class: 'grid three' }, METRICS.map((m) => h('div', { class: 'card lift metric', id: 'metric-' + m.key, vars: { '--accent': m.accent } },
    h('div', { class: 'card-top' }, iconTile(m.icon, m.accent, 44),
      h('div', { class: 'card-title' }, h('h3', { text: m.title }), h('p', { class: 'sub', text: '…' }))),
    h('div', { class: 'value' }, h('strong', { class: 'num', text: '…' }), h('span', { class: 'unit' })),
    h('div', { class: 'meter' }, h('i', { style: { width: '0%' } })),
    h('div', { class: 'card-bottom' }, h('span', { class: 'status' }, h('i'), h('span', { class: 'label', text: 'Measuring…' }))))));

  const chart = h('div', { class: 'section' },
    h('div', { class: 'section-head' }, h('h2', { text: 'Last 10 minutes' }), h('span', { class: 'meta', text: 'Updates every 2 seconds' }),
      h('span', { class: 'spacer' }),
      h('div', { class: 'legend', id: 'legend' },
        h('span', { vars: { '--c': '#3d8bff' } }, h('i'), 'CPU ', h('b', { class: 'num', id: 'lg-cpu', text: '–' })),
        h('span', { vars: { '--c': '#6d5cff' } }, h('i'), 'Memory ', h('b', { class: 'num', id: 'lg-mem', text: '–' })))),
    h('div', { class: 'card plain' }, h('div', { class: 'chart', id: 'chart' })));

  const top = h('div', { class: 'section' },
    h('div', { class: 'section-head' }, h('h2', { text: 'Using the most memory' }), h('span', { class: 'spacer' }),
      h('button', { class: 'text-btn', onclick: () => go('processes') }, 'See all', ico('chevron', 14))),
    h('div', { class: 'list', id: 'top-apps' }));

  const actions = h('div', { class: 'section' },
    h('div', { class: 'section-head' }, h('h2', { text: 'Quick actions' }), h('span', { class: 'spacer' })),
    h('div', { style: { display: 'grid', gap: '12px' } },
      quickAction('End unnecessary tasks', 'Close background helpers and updaters you don’t need right now.', 'x', '#2fbf71', 'Run', endUnnecessary),
      quickAction('Clean junk files', 'Temp files, caches and crash reports, with sizes shown first.', 'broom', '#22b8cf', 'Open', () => go('cleaner')),
      quickAction('Startup apps', 'Fewer apps at sign-in means a faster boot.', 'rocket', '#ff7a45', 'Review', () => go('startup'))));

  content.replaceChildren(metrics, h('div', { style: { height: '30px' } }), chart, h('div', { class: 'split' }, top, actions));
  updateOverview();
}

function quickAction(title, text, iconName, accent, label, fn) {
  return h('div', { class: 'card lift action-card', vars: { '--accent': accent }, style: { gap: '0' } },
    h('div', { class: 'card-top', style: { alignItems: 'center' } }, iconTile(iconName, accent, 40),
      h('div', { class: 'card-title' }, h('h3', { text: title }), h('p', { text })),
      h('button', { class: 'btn-launch', onclick: (e) => fn(e.currentTarget) }, label)));
}

function setMetric(key, sub, value, unit, ratio, statusText, level) {
  const card = document.getElementById('metric-' + key);
  if (!card) return;
  $('.sub', card).textContent = sub;
  $('.value strong', card).textContent = value;
  $('.value .unit', card).textContent = unit;
  const meter = $('.meter', card);
  meter.className = 'meter' + (level === 'danger' ? ' danger' : level === 'warn' ? ' warn' : '');
  $('i', meter).style.width = Math.max(1, Math.min(100, ratio)) + '%';
  const status = $('.status', card);
  status.className = 'status' + (level ? ' warn' : ' on');
  $('.label', status).textContent = statusText;
}

function updateOverview() {
  const snap = state.snap;
  if (!snap || !snap.stats || state.page !== 'overview') return;
  const s = snap.stats;
  const lvl = (v, warn, bad) => (v >= bad ? 'danger' : v >= warn ? 'warn' : '');
  setMetric('cpu', `${snap.cores} logical cores`, pct(s.cpu), 'in use', s.cpu,
    s.cpu >= 75 ? 'Busy right now' : 'Running smoothly', lvl(s.cpu, 75, 90));
  setMetric('mem', `${fmtBytes(s.mem_used)} of ${fmtBytes(s.mem_total)}`, pct(s.mem_percent), 'in use', s.mem_percent,
    s.mem_percent >= 85 ? 'Getting full' : 'Plenty available', lvl(s.mem_percent, 85, 92));
  const freePct = 100 - s.disk_percent;
  setMetric('disk', `Drive ${s.disk_drive} · ${fmtBytes(s.disk_total)}`, fmtBytes(s.disk_free).replace(' ', ' '), 'free', s.disk_percent,
    freePct < 10 ? 'Almost full' : 'Plenty of space', freePct < 10 ? 'danger' : freePct < 20 ? 'warn' : '');
  $('#lg-cpu').textContent = pct(s.cpu);
  $('#lg-mem').textContent = pct(s.mem_percent);
  drawChart();

  const list = $('#top-apps');
  const apps = [...snap.apps].sort((a, b) => b.mem - a.mem).slice(0, 5);
  const max = apps.length ? apps[0].mem : 1;
  list.replaceChildren(...apps.map((a) => h('div', { class: 'row', style: { gridTemplateColumns: 'minmax(0,1fr) 120px' } },
    h('div', { class: 'who' }, appIcon(a.name, 32), h('div', {}, h('strong', { text: a.name }),
      h('small', { text: a.desc || plural(a.count, 'process', 'processes') }))),
    h('div', { class: 'r mini' }, h('span', { class: 'num', text: fmtBytes(a.mem) }),
      h('div', { class: 'meter', vars: { '--accent': accentFor(a.name) } }, h('i', { style: { width: (a.mem / max * 100) + '%' } }))))));
}

function drawChart() {
  const box = document.getElementById('chart');
  if (!box || !state.snap) return;
  const hist = state.snap.history;
  const W = 600, H = 100, N = 300;
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('preserveAspectRatio', 'none');
  const defs = document.createElementNS(NS, 'defs');
  svg.append(defs);
  for (const y of [0, 50, 100]) {
    const l = document.createElementNS(NS, 'line');
    Object.entries({ x1: 0, x2: W, y1: H - y, y2: H - y, class: 'grid-line', 'vector-effect': 'non-scaling-stroke' })
      .forEach(([k, v]) => l.setAttribute(k, v));
    if (y === 50) l.setAttribute('stroke-dasharray', '3 4');
    svg.append(l);
  }
  const series = [{ i: 1, color: '#6d5cff', id: 'gm' }, { i: 0, color: '#3d8bff', id: 'gc' }];
  for (const s of series) {
    const grad = document.createElementNS(NS, 'linearGradient');
    grad.id = s.id; grad.setAttribute('x1', 0); grad.setAttribute('y1', 0); grad.setAttribute('x2', 0); grad.setAttribute('y2', 1);
    for (const [off, op] of [[0, 0.28], [1, 0]]) {
      const stop = document.createElementNS(NS, 'stop');
      stop.setAttribute('offset', off); stop.setAttribute('stop-color', s.color); stop.setAttribute('stop-opacity', op);
      grad.append(stop);
    }
    defs.append(grad);
    if (hist.length < 2) continue;
    const start = N - hist.length;
    const pts = hist.map((p, k) => [((start + k) / (N - 1)) * W, H - Math.max(0, Math.min(100, p[s.i]))]);
    const line = pts.map((p, k) => (k ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
    const area = document.createElementNS(NS, 'path');
    area.setAttribute('d', `${line} L ${pts[pts.length - 1][0].toFixed(1)} ${H} L ${pts[0][0].toFixed(1)} ${H} Z`);
    area.setAttribute('fill', `url(#${s.id})`);
    const path = document.createElementNS(NS, 'path');
    Object.entries({ d: line, fill: 'none', stroke: s.color, 'stroke-width': 2, 'stroke-linejoin': 'round', 'vector-effect': 'non-scaling-stroke' })
      .forEach(([k, v]) => path.setAttribute(k, v));
    svg.append(area, path);
  }
  box.replaceChildren(svg,
    h('span', { class: 'axis', style: { position: 'absolute', left: '4px', top: '-2px' }, text: '100%' }),
    h('span', { class: 'axis', style: { position: 'absolute', left: '4px', bottom: '-2px' }, text: '10 min ago' }),
    h('span', { class: 'axis', style: { position: 'absolute', right: '4px', bottom: '-2px' }, text: 'now' }));
}

// ---------- processes ----------

function renderProcesses(content) {
  const snap = state.snap;
  setHead('Processes', snap ? `${plural(snap.apps.length, 'app')} · ${plural(snap.processes, 'process', 'processes')}` : 'Loading…', [
    h('button', { class: 'btn btn-primary', id: 'btn-unneeded', onclick: (e) => endUnnecessary(e.currentTarget) }, ico('x', 17), 'End unnecessary tasks'),
  ]);
  const group = snap ? snap.group : true;
  const toolbar = h('div', { class: 'toolbar' },
    h('div', { class: 'seg' },
      h('button', { class: group ? 'on' : '', text: 'Group by app', onclick: () => setGrouping(true) }),
      h('button', { class: group ? '' : 'on', text: 'Every process', onclick: () => setGrouping(false) })),
    h('span', { class: 'meta', text: 'Orange values are heavy. Core Windows processes are protected.' }));
  const sortBtn = (key, label, right) => h('button', { class: (state.sort.key === key ? 'sorted' : '') + (right ? ' r' : ''),
    onclick: () => { state.sort = { key, desc: state.sort.key === key ? !state.sort.desc : key !== 'name' }; renderProcesses(content); } },
    label, state.sort.key === key ? h('span', { style: { transform: state.sort.desc ? '' : 'rotate(180deg)', display: 'inline-flex' } }, ico('chevron', 12)) : null);
  const list = h('div', { class: 'list' },
    h('div', { class: 'list-head cols-proc' }, sortBtn('name', 'App'), h('div', { class: 'r' }, sortBtn('count', 'Processes', true)),
      h('div', { class: 'r' }, sortBtn('cpu', 'CPU', true)), h('div', { class: 'r' }, sortBtn('mem', 'Memory', true)), h('span')),
    h('div', { id: 'proc-rows' }, snap ? null : Array.from({ length: 8 }, () => h('div', { class: 'skeleton row-sk' }))));
  content.replaceChildren(toolbar, list);
  procRows.clear();
  updateProcessList();
}

async function setGrouping(group) {
  await call('set_grouping', group);
  if (state.snap) state.snap.group = group;
  procRows.clear();
  $('#proc-rows') && $('#proc-rows').replaceChildren(...Array.from({ length: 8 }, () => h('div', { class: 'skeleton row-sk' })));
  state.waitGroup = group;
  renderProcesses($('#content'));
}

const procRows = new Map();
function updateProcessList() {
  const box = document.getElementById('proc-rows');
  const snap = state.snap;
  if (!box || !snap || (state.waitGroup !== undefined && snap.group !== state.waitGroup)) return;
  state.waitGroup = undefined;
  if (box.querySelector('.skeleton')) box.replaceChildren();
  $('#page-sub').textContent = `${plural(snap.apps.length, 'app')} · ${plural(snap.processes, 'process', 'processes')}`;
  const q = state.query;
  const { key, desc } = state.sort;
  const apps = snap.apps.filter((a) => !q || a.name.toLowerCase().includes(q) || (a.desc || '').toLowerCase().includes(q));
  apps.sort((a, b) => {
    const va = key === 'name' ? a.name.toLowerCase() : a[key], vb = key === 'name' ? b.name.toLowerCase() : b[key];
    return (va < vb ? -1 : va > vb ? 1 : 0) * (desc ? -1 : 1);
  });
  const maxMem = Math.max(1, ...snap.apps.map((a) => a.mem));
  const seen = new Set();
  apps.forEach((a, idx) => {
    seen.add(a.key);
    let row = procRows.get(a.key);
    if (!row) {
      row = buildProcRow(a);
      procRows.set(a.key, row);
    }
    fillProcRow(row, a, maxMem, snap.group);
    if (box.children[idx] !== row) box.insertBefore(row, box.children[idx] || null);
  });
  for (const [k, row] of procRows) if (!seen.has(k)) { row.remove(); procRows.delete(k); }
  if (!apps.length) box.replaceChildren(h('div', { class: 'empty-small' }, ico('search', 26), h('h3', { text: 'No matching apps' }), h('p', { text: 'Try a different search.' })));
}

function buildProcRow(a) {
  const row = h('div', { class: 'row cols-proc' },
    h('div', { class: 'who' }, appIcon(a.name, 34), h('div', {}, h('strong'), h('small'))),
    h('div', { class: 'r num c-count' }),
    h('div', { class: 'r num c-cpu' }),
    h('div', { class: 'r mini' }, h('span', { class: 'num c-mem' }), h('div', { class: 'meter', vars: { '--accent': accentFor(a.name) } }, h('i'))),
    h('div', { class: 'actions' }));
  return row;
}

function fillProcRow(row, a, maxMem, group) {
  $('strong', row).textContent = a.name;
  $('small', row).textContent = a.desc || (a.protected ? 'Windows process' : group ? plural(a.count, 'process', 'processes') : 'Process');
  $('.c-count', row).textContent = a.count;
  const cpu = $('.c-cpu', row);
  cpu.textContent = a.cpu.toFixed(1) + '%';
  cpu.classList.toggle('hot', a.cpu >= 20 && !a.protected);
  const mem = $('.c-mem', row);
  mem.textContent = fmtBytes(a.mem);
  mem.classList.toggle('hot', a.mem >= 1024 ** 3 && !a.protected);
  $('.meter i', row).style.width = (a.mem / maxMem * 100) + '%';
  const actions = $('.actions', row);
  if (actions.dataset.protected !== String(a.protected)) {
    actions.dataset.protected = String(a.protected);
    actions.replaceChildren(a.protected
      ? h('span', { class: 'icon-btn small', title: 'Protected: part of Windows' }, ico('lock', 15))
      : h('button', { class: 'icon-btn small stop-btn', title: 'End task', onclick: () => endTask(row.dataset.key, row.dataset.name, row.dataset.count) }, ico('stop', 15)));
    if (a.protected) actions.style.opacity = '0.6';
  }
  row.dataset.key = a.key; row.dataset.name = a.name; row.dataset.count = a.count;
}

async function endTask(key, name, count) {
  const n = Number(count);
  const ok = await confirmModal({
    title: `End “${name}”?`, danger: true, ok: 'End task',
    text: (n > 1 ? `This closes all ${n} ${name} processes. ` : '') + 'Anything unsaved in it will be lost.',
  });
  if (!ok) return;
  const r = await call('end_task', key);
  if (r.error) toast(r.error, { type: 'error' });
  else if (r.denied) toast(`Ended ${r.ended}, but Windows refused ${r.denied}. Try running as administrator.`, { type: 'error', action: adminAction() });
  else toast(`Ended ${r.name}.`);
}

async function endUnnecessary(btn) {
  const original = btn ? [...btn.childNodes] : null;
  if (btn) { btn.disabled = true; btn.replaceChildren(h('span', { class: 'spinner' }), 'Working…'); }
  try {
    const r = await call('end_unnecessary');
    const admin = state.boot.admin;
    if (!r.actions.length) {
      toast('Nothing unnecessary is running right now.' + (admin ? '' : ' As administrator, updater services can be stopped too.'),
        { type: 'info', action: admin ? null : adminAction() });
    } else {
      const parts = r.actions.map((a) => a.result).filter(Boolean).join(' · ');
      toast(`Done: ${parts}. Freed about ${fmtBytes(r.freed)} of memory. They restart by themselves when needed.`, { ms: 6500 });
    }
  } finally {
    if (btn) { btn.disabled = false; btn.replaceChildren(...original); }
  }
}

function adminAction() {
  return { label: 'Run as admin', fn: restartAdmin };
}
async function restartAdmin() {
  const ok = await call('restart_admin');
  if (!ok) toast('SwiftMyPC is still running without administrator rights.', { type: 'info' });
}

// ---------- startup apps ----------

async function loadStartup() {
  state.startup = await call('startup_list');
  updateCounts();
}

function renderStartup(content) {
  const items = state.startup;
  const on = items ? items.filter((s) => s.enabled).length : 0;
  setHead('Startup apps', items ? `${on} of ${items.length} start when you sign in` : 'Loading…', [
    h('button', { class: 'btn', onclick: async () => { state.startup = null; render(); await loadStartup(); render(); } }, ico('refresh', 16), 'Refresh'),
  ]);
  if (!items) {
    content.replaceChildren(h('div', { class: 'grid' }, Array.from({ length: 6 }, () => h('div', { class: 'skeleton' }))));
    loadStartup().then(() => state.page === 'startup' && render());
    return;
  }
  const q = state.query;
  const shown = items.filter((s) => !q || s.name.toLowerCase().includes(q) || s.command.toLowerCase().includes(q))
    .sort((a, b) => (b.enabled - a.enabled) || prettyStartupName(a.name).localeCompare(prettyStartupName(b.name)));
  const banner = h('div', { class: 'banner info' }, ico('info', 18),
    h('p', {}, h('strong', { text: 'Turning an app off here is safe. ' }), 'It uses the same switch as Task Manager, and you can turn it back on any time.'));
  const grid = h('div', { class: 'grid' }, shown.map((s) => startupCard(s)));
  content.replaceChildren(banner, shown.length ? grid
    : h('div', { class: 'empty-small' }, ico('rocket', 26), h('h3', { text: q ? 'No matching startup apps' : 'Nothing starts with Windows' }),
      h('p', { text: q ? 'Try a different search.' : 'Your sign-in is as light as it gets.' })));
}

function startupCard(s) {
  const name = prettyStartupName(s.name);
  const locked = s.needs_admin && !state.boot.admin;
  const toggle = h('button', { class: 'toggle' + (s.enabled ? ' on' : ''), role: 'switch', 'aria-checked': String(s.enabled),
    title: locked ? 'Needs administrator rights' : (s.enabled ? 'Turn off' : 'Turn on'), disabled: locked,
    onclick: async () => {
      const r = await call('startup_set', s.id, !s.enabled);
      if (r.error) { toast(r.error, { type: 'error', action: adminAction() }); return; }
      s.enabled = r.enabled;
      toast(`${name} ${s.enabled ? 'will start' : 'won’t start'} with Windows.`);
      updateCounts();
      render();
    } });
  const where = s.location.startsWith('Startup folder') ? 'Startup folder' : 'Registry';
  return h('div', { class: 'card lift', vars: { '--accent': s.enabled ? accentFor(s.name) : '#a0a3b1' } },
    h('div', { class: 'card-top' }, appIcon(s.name, 44, s.enabled ? null : '#a0a3b1'),
      h('div', { class: 'card-title' }, h('h3', { text: name, title: s.name }),
        h('p', {}, h('span', { class: 'chip', text: where }), s.needs_admin ? h('span', { class: 'chip warn', text: 'All users' }) : null)),
      h('button', { class: 'icon-btn small', title: 'Show file in Explorer', onclick: async () => {
        if (!(await call('open_location', s.path))) toast('Couldn’t find that file on disk.', { type: 'error' });
      } }, ico('folder', 15))),
    h('div', { class: 'card-bottom' },
      h('span', { class: 'status' + (s.enabled ? ' on' : '') }, h('i'), s.enabled ? 'Starts with Windows' : 'Off'), toggle));
}

// ---------- services ----------

async function loadServices() {
  state.services = await call('services_list');
  updateCounts();
}

function renderServices(content) {
  const all = state.services;
  const review = all ? all.filter((s) => s.level).length : 0;
  setHead('Services', all ? `${review} worth reviewing · ${all.length} services` : 'Loading…', [
    h('button', { class: 'btn', onclick: async () => { state.services = null; render(); await loadServices(); render(); } }, ico('refresh', 16), 'Refresh'),
  ]);
  const blocks = [];
  if (!state.boot.admin) {
    blocks.push(h('div', { class: 'banner warn' }, ico('lock', 18),
      h('p', {}, h('strong', { text: 'Changing services needs administrator rights. ' }), 'You can look around; to change anything, restart as administrator.'),
      h('button', { class: 'btn', onclick: restartAdmin }, ico('power', 16), 'Restart as admin')));
  }
  const f = state.svcFilter;
  blocks.push(h('div', { class: 'toolbar' },
    h('div', { class: 'seg' }, [['review', 'Worth reviewing'], ['apps', 'App services'], ['all', 'All services']].map(([k, l]) =>
      h('button', { class: f === k ? 'on' : '', text: l, onclick: () => { state.svcFilter = k; render(); } }))),
    h('span', { class: 'meta', text: 'Manual means a service only starts when something needs it.' })));
  if (!all) {
    blocks.push(h('div', { class: 'list' }, Array.from({ length: 8 }, () => h('div', { class: 'skeleton row-sk' }))));
    content.replaceChildren(...blocks);
    loadServices().then(() => state.page === 'services' && render());
    return;
  }
  const q = state.query;
  const order = { optional: 0, review: 1, '': 2 };
  const shown = all.filter((s) => (f === 'all' || (f === 'review' ? s.level : s.source === 'App'))
      && (!q || s.display.toLowerCase().includes(q) || s.name.toLowerCase().includes(q) || s.advice.toLowerCase().includes(q)))
    .sort((a, b) => order[a.level] - order[b.level] || a.display.localeCompare(b.display));
  const list = h('div', { class: 'list' },
    h('div', { class: 'list-head cols-svc' }, h('span', { text: 'Service' }), h('span', { text: 'Starts' }), h('span', { text: 'Status' }),
      h('span', { class: 'r', text: 'Memory' }), h('span')),
    shown.map(serviceRow));
  blocks.push(shown.length ? list : h('div', { class: 'empty-small' }, ico('check', 26),
    h('h3', { text: q ? 'No matching services' : 'Nothing to review' }), h('p', { text: q ? 'Try a different search.' : 'No background services look unnecessary right now.' })));
  content.replaceChildren(...blocks);
}

function serviceRow(s) {
  const chip = s.level === 'optional' ? h('span', { class: 'chip accent', text: 'Safe to set Manual' })
    : s.level === 'review' ? h('span', { class: 'chip warn', text: 'Review' })
    : !s.can_change ? h('span', { class: 'chip' }, ico('lock', 11), 'Windows') : null;
  const running = s.status === 'running';
  return h('div', { class: 'row cols-svc', title: s.description || '' },
    h('div', { class: 'who' }, appIcon(s.display, 34, s.source === 'Windows' ? '#a0a3b1' : null),
      h('div', {}, h('strong', { text: s.display }), h('small', { text: s.advice || s.description || s.name }))),
    h('div', { style: { display: 'flex', flexDirection: 'column', gap: '3px', alignItems: 'flex-start' } }, h('span', { text: s.start }), chip),
    h('span', { class: 'status' + (running ? ' on' : '') }, h('i'), running ? 'Running' : s.status === 'stopped' ? 'Stopped' : s.status.replace('_', ' ')),
    h('span', { class: 'r num', text: s.memory ? fmtBytes(s.memory) : '–' }),
    h('div', { class: 'actions', style: { opacity: 1 } }, h('button', { class: 'icon-btn small', title: 'Actions',
      onclick: (e) => openMenu(e.currentTarget, [
        { label: 'Set to Manual', icon: 'check', disabled: !s.can_change, fn: () => serviceAction(s, 'manual') },
        { label: 'Restore original', icon: 'undo', disabled: !s.can_change, fn: () => serviceAction(s, 'restore') },
        { label: 'Stop now', icon: 'stop', disabled: !s.can_change || !running, fn: () => serviceAction(s, 'stop') },
        '-',
        { label: 'Disable', icon: 'x', danger: true, disabled: !s.can_change, fn: () => serviceAction(s, 'disabled') },
      ]) }, ico('more', 16))));
}

async function serviceAction(s, action) {
  if (!state.boot.admin) { toast('Changing services needs administrator rights.', { type: 'error', action: adminAction() }); return; }
  if (action === 'disabled' && !(await confirmModal({ title: `Disable “${s.display}”?`, danger: true, ok: 'Disable',
    text: 'A disabled service can’t start at all, even when an app needs it. If something stops working, use “Restore original”.' }))) return;
  const r = await call('service_action', [s.name], action);
  if (r.errors.length) toast(r.errors.join(' · '), { type: 'error' });
  else toast({ manual: `${s.display} now starts only when needed.`, disabled: `${s.display} is disabled.`,
    restore: `${s.display} is back to how it was.`, stop: `${s.display} was stopped.` }[action]);
  await loadServices();
  if (state.page === 'services') render();
}

// ---------- cleaner ----------

const CLEAN_ICONS = { user_temp: 'folder', win_temp: 'desktop', browser: 'globe', crash: 'alert', wu_cache: 'download', recycle: 'trash' };

async function scanCleaner() {
  state.cleaner = null;
  if (state.page === 'cleaner') render();
  const targets = await call('cleaner_scan');
  state.cleaner = targets;
  for (const t of targets) if (!(t.key in state.cleanerSel)) state.cleanerSel[t.key] = !t.recycle && (!t.needs_admin || state.boot.admin);
  if (state.page === 'cleaner') render();
}

function renderCleaner(content) {
  setHead('Cleaner', 'Free up space from files Windows and your apps don’t need', [
    h('button', { class: 'btn', onclick: scanCleaner, disabled: !state.cleaner }, ico('refresh', 16), 'Scan again'),
  ]);
  const targets = state.cleaner;
  if (!targets) {
    content.replaceChildren(h('div', {}, Array.from({ length: 6 }, () => h('div', { class: 'skeleton opt-sk' }))));
    if (!state.scanning) { state.scanning = true; scanCleaner().finally(() => { state.scanning = false; }); }
    return;
  }
  const rows = targets.map((t) => {
    const locked = t.needs_admin && !state.boot.admin;
    const checked = !locked && state.cleanerSel[t.key];
    return h('div', { class: 'option' + (checked ? ' checked' : '') + (locked ? ' disabled' : ''), role: 'checkbox', 'aria-checked': String(!!checked),
      tabindex: locked ? null : 0,
      onclick: () => { if (locked) return; state.cleanerSel[t.key] = !state.cleanerSel[t.key]; render(); },
      onkeydown: (e) => { if (!locked && (e.key === ' ' || e.key === 'Enter')) { e.preventDefault(); state.cleanerSel[t.key] = !state.cleanerSel[t.key]; render(); } } },
      h('span', { class: 'ico lead' }, ico(CLEAN_ICONS[t.key] || 'folder', 18)),
      h('div', { class: 'grow' }, h('strong', {}, t.label, ' ', locked ? h('span', { class: 'chip warn', text: 'Needs admin' }) : null,
        t.recycle ? h('span', { class: 'chip danger', text: 'Permanent' }) : null), h('small', { text: t.description })),
      h('div', { class: 'side' }, h('b', { class: 'num', text: fmtBytes(t.size) }), h('small', { text: plural(t.count, 'file') })),
      h('span', { class: 'toggle' }));
  });
  const chosen = targets.filter((t) => state.cleanerSel[t.key] && !(t.needs_admin && !state.boot.admin));
  const total = chosen.reduce((s, t) => s + t.size, 0);
  const files = chosen.reduce((s, t) => s + t.count, 0);
  const bar = h('div', { class: 'action-bar' },
    h('div', { class: 'grow' }, h('strong', { class: 'num', text: `${fmtBytes(total)} can be freed` }),
      h('small', { text: `${plural(files, 'file')} selected · files that are in use are skipped automatically` })),
    h('button', { class: 'btn btn-primary', disabled: !chosen.length, onclick: () => cleanSelected(chosen) }, ico('broom', 17), 'Clean selected'));
  content.replaceChildren(...rows, bar);
}

async function cleanSelected(chosen) {
  const ok = await confirmModal({ title: `Clean ${plural(chosen.length, 'place')}?`, ok: 'Clean now', lead: 'broom',
    text: `This deletes ${fmtBytes(chosen.reduce((s, t) => s + t.size, 0))} from: ${chosen.map((t) => t.label).join(', ')}. It can’t be undone.` });
  if (!ok) return;
  const btn = $('.action-bar .btn-primary');
  if (btn) { btn.disabled = true; btn.replaceChildren(h('span', { class: 'spinner' }), 'Cleaning…'); }
  const r = await call('cleaner_clean', chosen.map((t) => t.key));
  toast(`Freed ${fmtBytes(r.freed)} (${plural(r.deleted, 'file')} deleted${r.skipped ? `, ${r.skipped} in use were skipped` : ''}).`, { ms: 6000 });
  scanCleaner();
}

// ---------- settings ----------

const SETTINGS = [
  { key: 'alerts', icon: 'bell', title: 'Alerts', text: 'Notify me about high CPU or memory that lasts, an app hogging the CPU, or a nearly full disk.', tray: true },
  { key: 'close_to_tray', icon: 'tray', title: 'Keep running in the tray', text: 'Closing the window keeps SwiftMyPC watching in the background.', tray: true },
  { key: 'start_with_windows', icon: 'power', title: 'Start with Windows', text: 'Start quietly in the tray when you sign in.' },
  { key: 'auto_optimize', icon: 'calendar', title: 'Smart Optimize once a day', text: 'Clean junk files and close background helpers automatically when your PC isn’t busy.' },
  { key: 'auto_update', icon: 'download', title: 'Install updates automatically', text: 'Updates install by themselves while SwiftMyPC is in the tray.' },
];

function renderSettings(content) {
  const s = state.settings;
  setHead('Settings', 'Choose how SwiftMyPC works in the background', []);
  const toggles = SETTINGS.map((d) => {
    const disabled = d.tray && !s.has_tray;
    return h('div', { class: 'option' + (s[d.key] ? ' checked' : '') + (disabled ? ' disabled' : ''), role: 'switch',
      'aria-checked': String(!!s[d.key]), tabindex: disabled ? null : 0,
      onclick: () => !disabled && flipSetting(d.key),
      onkeydown: (e) => { if (!disabled && (e.key === ' ' || e.key === 'Enter')) { e.preventDefault(); flipSetting(d.key); } } },
      h('span', { class: 'ico lead' }, ico(d.icon, 18)),
      h('div', { class: 'grow' }, h('strong', { text: d.title }), h('small', { text: d.text })),
      h('span', { class: 'toggle' }));
  });
  const theme = s.theme || 'system';
  const themes = h('div', { class: 'radio-row' }, [['system', 'System', 'desktop'], ['light', 'Light', 'sun'], ['dark', 'Dark', 'moon']].map(([k, l, i]) =>
    h('div', { class: 'option' + (theme === k ? ' checked' : ''), role: 'radio', 'aria-checked': String(theme === k), tabindex: 0,
      onclick: () => setTheme(k), onkeydown: (e) => { if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); setTheme(k); } } },
      h('span', { class: 'ico lead' }, ico(i, 18)), h('div', { class: 'grow' }, h('strong', { text: l })), h('span', { class: 'radio' }))));
  const v = state.boot.version;
  const updates = h('div', { class: 'option static' },
    h('span', { class: 'ico lead' }, ico('refresh', 18)),
    h('div', { class: 'grow' }, h('strong', { text: `SwiftMyPC ${v}` }),
      h('small', { text: state.update ? `Version ${state.update.version} is available.` : 'Checks GitHub for new versions every 6 hours.' })),
    h('button', { class: 'btn', onclick: checkUpdates }, 'Check now'));
  const about = h('div', { class: 'hero-panel' },
    h('div', { class: 'logo big' }, h('i'), h('i', {}, ico('zap', 26))),
    h('div', { class: 'grow' }, h('span', { class: 'eyebrow', text: 'About' }), h('h2', { text: 'SwiftMyPC' }),
      h('p', { text: 'A small, honest Windows helper. It shows what slows your PC down and fixes only what’s safe to fix.' }),
      h('div', { class: 'chips' }, h('span', { class: 'chip accent', text: `Version ${v}` }), h('span', { class: 'chip', text: 'Free and open source' }),
        h('span', { class: 'chip', text: 'Runs 100% on your PC' }))),
    h('div', { style: { display: 'flex', flexDirection: 'column', gap: '10px' } },
      h('button', { class: 'btn btn-github', onclick: () => api.open_url('https://github.com/yassirabidy/SwiftMyPC') }, ico('github', 16), 'GitHub'),
      h('button', { class: 'btn', onclick: () => api.quit_app() }, ico('power', 16), 'Quit SwiftMyPC')));
  const section = (title, meta, ...kids) => h('div', { class: 'section' },
    h('div', { class: 'section-head' }, h('h2', { text: title }), meta ? h('span', { class: 'meta', text: meta }) : null, h('span', { class: 'spacer' })), ...kids);
  content.replaceChildren(
    about,
    section('Background', s.has_tray ? null : 'Tray features are unavailable on this PC', ...toggles),
    section('Appearance', null, themes),
    section('Updates', null, updates));
}

async function flipSetting(key) {
  state.settings = await call('set_setting', key, !state.settings[key]);
  render();
}

// ---------- smart optimize & health check ----------

const OPT_GROUPS = { files: ['Free disk space', 'broom'], apps: ['Free memory', 'memory'], services: ['Lighter background', 'cube'], startup: ['Faster startup', 'rocket'] };

async function openSmartOptimize() {
  const sel = {};
  let plan = null;
  const go_ = h('button', { class: 'btn btn-primary', disabled: true, onclick: run }, ico('zap', 17), 'Optimize now');
  const undo = h('button', { class: 'btn left', hidden: true, onclick: doUndo }, ico('undo', 16), 'Undo last changes');
  const cancel = h('button', { class: 'btn', text: 'Cancel', onclick: () => m.close() });
  const m = openModal({ title: 'Smart Optimize', subtitle: 'Applies only safe fixes. Nothing personal is deleted, and setting changes can be undone.',
    lead: 'zap', wide: true, body: h('div', {}, Array.from({ length: 4 }, () => h('div', { class: 'skeleton opt-sk' }))), foot: [undo, cancel, go_] });
  plan = await call('optimize_plan');
  if (!m.el.isConnected) return;
  undo.hidden = !plan.can_undo;
  for (const a of plan.actions) if (a.group !== 'note') sel[a.key] = true;
  draw();

  function summary() {
    const chosen = plan.actions.filter((a) => sel[a.key]);
    const disk = chosen.filter((a) => a.group === 'files').reduce((s, a) => s + a.size, 0);
    const mem = chosen.filter((a) => a.group === 'apps' || a.group === 'services').reduce((s, a) => s + a.size, 0);
    const bits = [];
    if (disk) bits.push(`${fmtBytes(disk)} of disk`);
    if (mem) bits.push(`${fmtBytes(mem)} of memory`);
    return chosen.length ? `${plural(chosen.length, 'safe fix', 'safe fixes')} selected` + (bits.length ? ` · frees about ${bits.join(' and ')}` : '') : 'Nothing selected';
  }
  function draw(results) {
    const doable = plan.actions.filter((a) => a.group !== 'note');
    const body = [];
    if (!doable.length) {
      body.push(h('div', { class: 'empty-small', style: { padding: '30px 20px' } }, ico('check', 26), h('h3', { text: 'Already in good shape' }),
        h('p', { text: 'Nothing needs optimizing right now.' })));
    } else {
      body.push(h('p', { id: 'opt-summary', text: results ? results : summary() }));
    }
    for (const [g, [label, icon]] of Object.entries(OPT_GROUPS)) {
      const items = plan.actions.filter((a) => a.group === g || (g === 'files' && a.group === 'note'));
      if (!items.length) continue;
      body.push(h('div', { class: 'group', text: label }));
      for (const a of items) {
        if (a.group === 'note') {
          body.push(h('div', { class: 'option static' }, h('span', { class: 'ico lead' }, ico('info', 18)),
            h('div', { class: 'grow' }, h('strong', { text: a.label }), h('small', { text: a.detail }))));
          continue;
        }
        const res = a.result ? h('span', { class: 'result' + (a.result.startsWith('failed') ? ' err' : '') }, '✓ ' + a.result) : null;
        body.push(h('div', { class: 'option' + (sel[a.key] ? ' checked' : '') + (results ? ' static' : ''), role: 'checkbox', 'aria-checked': String(!!sel[a.key]),
          tabindex: results ? null : 0,
          onclick: () => { if (results) return; sel[a.key] = !sel[a.key]; draw(); },
          onkeydown: (e) => { if (!results && (e.key === ' ' || e.key === 'Enter')) { e.preventDefault(); sel[a.key] = !sel[a.key]; draw(); } } },
          h('span', { class: 'ico lead' }, ico(icon, 18)),
          h('div', { class: 'grow' }, h('strong', { text: a.label }), h('small', { text: a.detail })),
          h('div', { class: 'side' }, a.size ? h('b', { class: 'num', text: fmtBytes(a.size) + (g === 'apps' || g === 'services' ? ' RAM' : '') }) : null, res),
          results ? null : h('span', { class: 'toggle' })));
      }
    }
    if (plan.admin_extra.length && !results) {
      body.push(h('div', { class: 'banner info', style: { marginTop: '8px', marginBottom: '8px' } }, ico('lock', 18),
        h('p', { text: `As administrator it can also handle: ${plan.admin_extra.join(', ')}.` }),
        h('button', { class: 'text-btn', onclick: restartAdmin }, 'Restart as admin')));
    }
    m.body.replaceChildren(...body);
    go_.disabled = !!results || !Object.values(sel).some(Boolean);
  }
  async function run() {
    const keys = Object.keys(sel).filter((k) => sel[k]);
    m.busy = true;
    go_.disabled = true; cancel.disabled = true;
    go_.replaceChildren(h('span', { class: 'spinner' }), 'Optimizing…');
    let r;
    try { r = await call('optimize_run', keys); } finally { m.busy = false; cancel.disabled = false; }
    for (const a of r.actions) { const p = plan.actions.find((x) => x.key === a.key); if (p) p.result = a.result; }
    const msg = `Done! Freed ${fmtBytes(r.freed)} of disk space` + (r.closed ? ` and closed ${plural(r.closed, 'background helper')}.` : '.');
    draw(msg);
    undo.hidden = !r.can_undo;
    go_.replaceChildren(ico('check', 17), 'Done');
    go_.disabled = false;
    go_.onclick = () => m.close();
    cancel.hidden = true;
    toast(msg);
    state.cleaner = null; state.startup = null; state.services = null;
    loadStartup(); loadServices();
  }
  async function doUndo() {
    const r = await call('optimize_undo');
    toast(r.message);
    undo.hidden = !r.can_undo;
    loadStartup(); loadServices();
  }
}

async function openHealthCheck() {
  const m = openModal({ title: 'Health check', subtitle: 'A short list of what’s actually worth fixing.', lead: 'health',
    body: h('div', { class: 'empty-small', style: { padding: '34px 20px' } }, h('span', { class: 'spinner', style: { width: '22px', height: '22px', color: 'var(--brand)' } }),
      h('p', { text: 'Checking your PC… this takes a few seconds.' })),
    foot: [h('button', { class: 'btn', text: 'Close', onclick: () => m.close() })] });
  const findings = await call('health_check');
  if (!m.el.isConnected) return;
  const icons = { ok: 'check', warn: 'alert', tip: 'sparkle' };
  const warns = findings.filter((f) => f.level === 'warn').length;
  m.body.replaceChildren(
    h('p', { text: warns ? `${plural(warns, 'thing')} worth fixing:` : 'Looking good! Nothing urgent.' }),
    ...findings.map((f) => h('div', { class: 'finding ' + f.level }, ico(icons[f.level], 16), h('p', { text: f.text }))));
  m.foot.append(h('button', { class: 'btn btn-primary', onclick: () => { m.close(); openSmartOptimize(); } }, ico('zap', 17), 'Smart Optimize'));
}

// ---------- updates, admin ----------

function renderUpdatePill() {
  const pill = $('#update-pill');
  const u = state.update;
  pill.hidden = !u;
  if (!u) return;
  if (state.updating) {
    pill.disabled = true;
    pill.replaceChildren(h('span', { class: 'spinner', style: { color: 'var(--brand)' } }),
      h('span', { class: 'grow' }, 'Downloading ', h('strong', { text: `${u.version}` }), h('span', { id: 'upd-pct', text: '' })));
  } else {
    pill.disabled = false;
    pill.replaceChildren(ico('download', 16), h('span', { class: 'grow' }, h('strong', { text: 'Update available' }), ` · ${u.version}`),
      h('span', { text: u.can_self_update ? 'Install' : 'Get it' }));
  }
}
$('#update-pill').addEventListener('click', async () => {
  if (!state.update || state.updating) return;
  const r = await call('install_update');
  if (r.opened_page) toast('Opened the download page. Install the new version to replace this one.', { type: 'info' });
  else { state.updating = true; renderUpdatePill(); }
});

async function checkUpdates() {
  const r = await call('check_update');
  if (r.source) toast('You’re running from source code. Update it with “git pull”.', { type: 'info' });
  else if (r.error) toast('Couldn’t check for updates: ' + r.error, { type: 'error' });
  else if (!r.update) toast(`You’re up to date (version ${state.boot.version}).`);
  else { state.update = r.update; renderUpdatePill(); toast(`Version ${r.update.version} is available.`, { type: 'info', action: { label: 'Install', fn: () => $('#update-pill').click() } }); }
  if (state.page === 'settings') render();
}

function renderAdmin() {
  const admin = state.boot.admin;
  const link = $('#admin-link'), badge = $('#admin-badge');
  link.hidden = admin; badge.hidden = !admin;
  if (admin) badge.replaceChildren(ico('shield', 16), 'Running as administrator');
  else link.replaceChildren(ico('power', 16), 'Run as administrator');
}
$('#admin-link').addEventListener('click', restartAdmin);
$('#theme-btn').addEventListener('click', () => setTheme(effectiveDark() ? 'light' : 'dark'));

// Events pushed from Python (core.push).
window.backend = ({ event, data }) => {
  if (event === 'settings') { state.settings = Object.assign(state.settings, data); if (state.page === 'settings') render(); }
  else if (event === 'update') { state.update = data; renderUpdatePill(); }
  else if (event === 'update_progress') { const el = document.getElementById('upd-pct'); if (el) el.textContent = ` · ${data}%`; }
  else if (event === 'update_failed') { state.updating = false; renderUpdatePill(); toast('Update failed: ' + data, { type: 'error' }); }
  else if (event === 'shown') { if (state.page === 'overview') updateOverview(); }
};

// ---------- live data ----------

let polling = false;
async function poll() {
  if (polling) return;
  polling = true;
  try {
    const snap = await api.snapshot();
    if (snap && snap.stats) {
      state.snap = snap;
      updateCounts();
      if (state.page === 'overview') updateOverview();
      else if (state.page === 'processes') updateProcessList();
    }
  } catch (e) { /* window closing */ }
  polling = false;
}

// ---------- start ----------

async function start() {
  api = window.pywebview.api;
  $('#search-ico').replaceWith(ico('search', 17));
  $('#logo-mark').append(ico('zap', 14));
  const boot = await api.bootstrap();
  state.boot = boot;
  state.settings = boot.settings;
  state.update = boot.update;
  applyTheme();
  renderAdmin();
  renderUpdatePill();
  go('overview');
  poll();
  setInterval(poll, 2000);
  setTimeout(() => { loadStartup(); loadServices(); }, 1500);
  if (boot.notice === 'installed') toast('SwiftMyPC is installed. You’ll find it on your Desktop and in the Start menu.');
  if (boot.notice === 'updated') toast(`Updated to version ${boot.version}.`);
}

if (window.pywebview && window.pywebview.api && window.pywebview.api.bootstrap) start();
else window.addEventListener('pywebviewready', start, { once: true });

// Behavioural check of the dashboard without a browser.
// Runs the dashboard's inline script against a minimal DOM stub, then exercises
// the feature rail, the collapse state machine, the topology graph and the
// planned-feature panels.
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const root = path.resolve(__dirname, '..', '..');
const htmlPath = process.argv[2] || path.join(root, 'src/kiterouter/static/dashboard.html');
const html = fs.readFileSync(htmlPath, 'utf8');
const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const code = scripts[scripts.length - 1]; // the dashboard script (script[0] is the tailwind config)

const elements = {};
const windowListeners = {};

function makeEl(tag = 'div', id = '') {
  const el = {
    tagName: tag, id, className: '', _innerHTML: '', textContent: '', dataset: {}, style: {},
    _attrs: {}, _listeners: {}, children: [],
    clientWidth: 1000, clientHeight: 460,
    get innerHTML() { return this._innerHTML; },
    set innerHTML(v) { this._innerHTML = v; },
    classList: {
      _s: new Set(),
      add(...c) { c.forEach(x => this._s.add(x)); },
      remove(...c) { c.forEach(x => this._s.delete(x)); },
      contains(c) { return this._s.has(c); },
      toggle(c, on) { (on === undefined ? !this._s.has(c) : on) ? this._s.add(c) : this._s.delete(c); },
    },
    setAttribute(k, v) { this._attrs[k] = String(v); },
    getAttribute(k) { return this._attrs[k]; },
    appendChild(child) { this.children.push(child); return child; },
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    dispatch(t, ev) { (this._listeners[t] || []).forEach(fn => fn(ev)); },
    getBoundingClientRect() { return { top: 0, right: 0, height: 0 }; },
    querySelectorAll() { return []; },
    querySelector() { return null; },
  };
  return el;
}

function reg(id, tag = 'div') { const el = makeEl(tag, id); elements[id] = el; return el; }

const navEl = reg('nav-rail', 'nav');
const appRail = reg('app-rail', 'aside');
const railToggle = reg('rail-toggle', 'button');
const railTooltip = reg('rail-tooltip');
const mainEl = reg('main', 'main');
const topoViewport = reg('topology-viewport');
const topoCanvas = reg('topology-canvas');
const topoEdges = reg('topology-edges', 'svg');
const topoNodes = reg('topology-nodes');
const topoSummary = reg('topology-summary', 'p');
topoViewport.clientWidth = 1000;
topoViewport.clientHeight = 460;

const document = {
  getElementById(id) { return elements[id] || null; },
  createElement(tag) { return makeEl(tag); },
  querySelector(sel) { return sel === 'main' ? mainEl : null; },
  querySelectorAll() { return []; },
  addEventListener() {},
};

const store = {};
const localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
};

const ctx = {
  document, localStorage,
  lucide: { createIcons() {} },
  window: {
    lucide: { createIcons() {} },
    addEventListener(t, fn) { (windowListeners[t] = windowListeners[t] || []).push(fn); },
  },
  console,
  setTimeout, clearTimeout, setInterval, clearInterval,
  Object, Array, JSON, Math, Date, String, Number, Boolean, RegExp, Error, Set, Map,
  performance: { now: () => 0 },
};
vm.createContext(ctx);
vm.runInContext(
  code + '\nglobalThis.__t = { FEATURES, FEATURE_BY_ID, renderRail, renderFeaturePlan, ' +
  'setRailCollapsed, toggleRail, storedRailCollapsed, railIsCollapsed, RAIL_STORAGE_KEY, ' +
  'renderTopology, renderTopologyGraph, topoZoom, topoFit, topoStatusOf, topoEdgePath, ' +
  'initTopologyInteractions, TOPO };',
  ctx
);
const t = ctx.__t;

let failures = [];
const check = (cond, msg) => { if (!cond) failures.push(msg); };

// ── feature registry + rail ───────────────────────────────────────────────
check(t.FEATURES.length === 57, `expected 57 features, got ${t.FEATURES.length}`);
t.renderRail();
const railHtml = navEl._innerHTML;

const buttons = [...railHtml.matchAll(/id="tab-btn-([a-z0-9-]+)"/g)].map(m => m[1]);
check(buttons.length === 57, `rail rendered ${buttons.length} buttons, expected 57`);
check(new Set(buttons).size === 57, 'duplicate rail buttons rendered');
check(t.FEATURES.filter(f => !buttons.includes(f.id)).length === 0, 'a feature has no rail button');

const labels = [...railHtml.matchAll(/class="rail-label flex-1 text-left truncate">([^<]+)<\/span>/g)].map(m => m[1]);
check(labels.length === 57, `rendered ${labels.length} labels, expected 57`);
check(t.FEATURES.filter(f => !labels.includes(f.label)).length === 0, 'a feature label is missing');

const groups = [...new Set(t.FEATURES.map(f => f.group))];
const groupHeaders = [...railHtml.matchAll(/class="rail-group[^"]*">([^<]+)<\/div>/g)].map(m => m[1]);
check(groupHeaders.length === groups.length, `rendered ${groupHeaders.length} group headers, expected ${groups.length}`);
check((railHtml.match(/rail-sep/g) || []).length === groups.length - 1, 'expected a separator between groups');
check((railHtml.match(/bg-emerald-400/g) || []).length === 5, 'expected 5 green dots for live features');
check((railHtml.match(/bg-amber-400/g) || []).length === 52, 'expected 52 amber dots for planned features');
check((railHtml.match(/data-tip="/g) || []).length === 57, 'every feature should carry a tooltip');
check(!/\$\{/.test(railHtml), 'unsubstituted placeholder left in rail markup');

// ── collapse state machine ────────────────────────────────────────────────
const ariaStatic = (html.match(/id="rail-toggle"[\s\S]{0,400}?aria-expanded="([^"]+)"/) || [])[1];
const iconStatic = (html.match(/id="rail-toggle"[\s\S]{0,400}?data-lucide="([a-z0-9-]+)"/) || [])[1];
railToggle._attrs['aria-expanded'] = ariaStatic;
railToggle._innerHTML = `<i data-lucide="${iconStatic}"></i>`;
check(ariaStatic === 'true', `rail should ship expanded, markup says aria-expanded="${ariaStatic}"`);
check(iconStatic === 'panel-left-close', `expected the collapse icon in markup, got ${iconStatic}`);
check(t.railIsCollapsed() === false, 'rail should start expanded');

t.toggleRail();
check(t.railIsCollapsed() === true, 'toggleRail() should collapse the rail');
check(appRail.classList.contains('rail-collapsed'), 'collapsed class not applied');
check(/panel-left-open/.test(railToggle._innerHTML), 'collapsed rail should offer an expand icon');
check(railToggle.getAttribute('aria-expanded') === 'false', 'aria-expanded should be false when collapsed');
check(store[t.RAIL_STORAGE_KEY] === '1', 'collapsed state was not persisted');
check(railTooltip.classList.contains('hidden'), 'tooltip should be hidden after collapsing');
t.toggleRail();
check(t.railIsCollapsed() === false && store[t.RAIL_STORAGE_KEY] === '0', 'rail should expand again');

// ── topology graph ────────────────────────────────────────────────────────
const providers = {
  cursor:        { test_results: { 'composer-2.5': { status: 'ok' } }, last_test_status: 'ok' },
  antigravity:   { test_results: { 'gemini-3.8': { status: 'error', error: 'Antigravity HTTP 403' } }, last_test_status: 'error' },
  opencode_free: { test_results: { nemotron: { status: 'ok' } } },
  glm:           { test_results: { 'glm-5.1': { status: 'error', error: 'GLM upstream HTTP 401' } } },
  kiro:          { enabled: false, test_results: {} },   // switched off in config
  cline:         { test_results: {} },                   // untested, but see recent below
};
const recent = [{ provider: 'cline', timestamp: Math.floor(Date.now() / 1000) - 60 }];

t.renderTopology(providers, recent);

const nodeHtml = topoNodes._innerHTML;
const edgeHtml = topoEdges._innerHTML;
const allPoints = [...nodeHtml.matchAll(/left:([\d.]+)px; top:([\d.]+)px/g)].map(m => [Number(m[1]), Number(m[2])]);
// The hub is rendered with the nodes, so it is one of the matched positions.
const providerPoints = allPoints.filter(([x, y]) => !(x === t.TOPO.cx && y === t.TOPO.cy));
const paths = [...edgeHtml.matchAll(/<path /g)].length;

const summary = topoSummary.innerText || '';
const counts = summary.match(/(\d+) active · (\d+) error · (\d+) configured/);
check(counts !== null, `summary has an unexpected shape: "${summary}"`);
check(counts && Number(counts[1]) >= 2, 'the two ok providers should count as active');
check(counts && Number(counts[2]) >= 2, 'the two failing providers should count as error');
check(counts && Number(counts[3]) === providerPoints.length, 'configured count should match rendered nodes');

// status classification
check(t.topoStatusOf(providers.cursor) === 'active', 'cursor should classify as active');
check(t.topoStatusOf(providers.antigravity) === 'error', 'antigravity should classify as error');
check(t.topoStatusOf({ test_results: {} }) === 'untested', 'an untested provider should classify as untested');
check(t.topoStatusOf({ last_test_status: 'ok' }) === 'active', 'last_test_status ok should win');

// node + edge counts
check(/KiteRouter/.test(nodeHtml), 'hub node missing');
check(paths === providerPoints.length, `${paths} edges for ${providerPoints.length} provider nodes`);
// the union of known + configured providers is rendered, not just the test input
check(providerPoints.length >= Object.keys(providers).length, 'fewer nodes than providers given');

// layout sanity
check(new Set(allPoints.map(p => p.join(','))).size === allPoints.length, 'two nodes share a position');
check(allPoints.every(([x, y]) => x > 0 && y > 0 && x < t.TOPO.width && y < t.TOPO.height), 'a node sits outside the canvas');
check(providerPoints.every(([x, y]) => Math.hypot(x - t.TOPO.cx, y - t.TOPO.cy) > 80), 'a node overlaps the central hub');

// edge/node colouring per status
check(/stroke="#3f3f46"/.test(edgeHtml), 'untested edges should use the muted colour');
check(/stroke="#27272a"/.test(edgeHtml), 'disabled edges should be dimmed further');
check(/stroke="#fb7185"/.test(edgeHtml), 'error edges should use the error colour');
check(/bg-rose-400/.test(nodeHtml) && /bg-emerald-400/.test(nodeHtml), 'nodes should carry status dots');

// tooltips carry the real reason
check(/Antigravity HTTP 403/.test(nodeHtml), 'error detail missing from a node tooltip');
check(/disabled in config/.test(nodeHtml), 'a disabled provider should say so');
check(/recent traffic/.test(nodeHtml), 'a provider with recent traffic should say so');

// ── zoom / pan ────────────────────────────────────────────────────────────
t.initTopologyInteractions();
check(windowListeners['resize'], 'no resize handler registered');
t.TOPO.userAdjusted = false;
t.topoZoom(1);
const zoomedIn = t.TOPO.scale;
check(zoomedIn > 0, 'zoom in produced a non-positive scale');
check(t.TOPO.userAdjusted === true, 'zooming should mark the view as user-adjusted');
t.topoZoom(-1);
check(t.TOPO.scale < zoomedIn, 'zoom out should reduce the scale');
for (let i = 0; i < 40; i++) t.topoZoom(1);
check(t.TOPO.scale <= 2.5, `scale exceeded its maximum: ${t.TOPO.scale}`);
for (let i = 0; i < 60; i++) t.topoZoom(-1);
check(t.TOPO.scale >= 0.35, `scale fell below its minimum: ${t.TOPO.scale}`);

t.topoFit();
check(t.TOPO.userAdjusted === false, 'topoFit should clear the user-adjusted flag');
check(t.TOPO.x === 0 && t.TOPO.y === 0, 'topoFit should recentre the canvas');
const expectedFit = Math.min(1000 / t.TOPO.width, 460 / t.TOPO.height) * 0.96;
check(Math.abs(t.TOPO.scale - expectedFit) < 0.001, `topoFit computed ${t.TOPO.scale}, expected ${expectedFit}`);
check(/scale\(/.test(topoCanvas.style.transform), 'canvas transform was not applied');

// ── planned panels ────────────────────────────────────────────────────────
const planned = t.FEATURES.filter(f => f.status === 'planned');
for (const f of planned) {
  const sec = t.renderFeaturePlan(f.id);
  check(sec !== null, `no panel generated for planned feature ${f.id}`);
  if (!sec) continue;
  const h = sec._innerHTML;
  check(h.includes(f.label), `panel for ${f.id} is missing its label`);
  check(/planned/.test(h), `panel for ${f.id} has no planned badge`);
  check(h.includes(f.summary.slice(0, 30)), `panel for ${f.id} is missing its summary`);
  check(h.includes(f.reference.slice(0, 30)), `panel for ${f.id} is missing its reference`);
  const boxes = (h.match(/rounded border border-zinc-600/g) || []).length;
  check(boxes === f.checklist.length, `${f.id}: ${boxes} checklist boxes for ${f.checklist.length} items`);
  check(!/\$\{/.test(h), `${f.id}: unsubstituted placeholder in panel`);
}
for (const f of t.FEATURES.filter(x => x.status === 'live')) {
  check(t.renderFeaturePlan(f.id) === null, `live feature ${f.id} generated a stub panel`);
}

console.log(`features: ${t.FEATURES.length} | rail buttons: ${buttons.length} | labels: ${labels.length} | groups: ${groupHeaders.length}`);
console.log(`topology: ${paths} edges, ${providerPoints.length} provider nodes | scale ${t.TOPO.scale.toFixed(3)}`);
console.log(failures.length ? 'FAILURES:\n - ' + failures.join('\n - ') : 'ALL CHECKS PASSED');
process.exit(failures.length ? 1 : 0);

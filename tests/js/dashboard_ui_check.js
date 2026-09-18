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

function attrToDatasetKey(attr) {
  return attr.replace(/^data-/, '').replace(/-([a-z])/g, (_, c) => c.toUpperCase());
}

function matchesSel(el, sel) {
  let m = sel.match(/^\.([a-zA-Z0-9_-]+)(?:\[([a-z-]+)(?:="([^"]*)")?\])?$/);
  if (m) {
    if (!((' ' + (el.className || '') + ' ').includes(' ' + m[1] + ' '))) return false;
    if (!m[2]) return true;
    const key = attrToDatasetKey(m[2]);
    const val = (el.dataset && el.dataset[key] !== undefined) ? String(el.dataset[key]) : el._attrs[m[2]];
    return val !== undefined && (m[3] === undefined || val === m[3]);
  }
  m = sel.match(/^\[([a-z-]+)(?:="([^"]*)")?\]$/);
  if (m) {
    const key = attrToDatasetKey(m[1]);
    const val = (el.dataset && el.dataset[key] !== undefined) ? String(el.dataset[key]) : el._attrs[m[1]];
    return val !== undefined && (m[2] === undefined || val === m[2]);
  }
  m = sel.match(/^#([a-zA-Z0-9_-]+)$/);
  if (m) return el.id === m[1];
  return false;
}

function serializeEl(el) {
  const attrs = [];
  if (el.id) attrs.push(`id="${el.id}"`);
  if (el.className) attrs.push(`class="${el.className}"`);
  for (const [k, v] of Object.entries(el.dataset || {})) {
    const attr = 'data-' + k.replace(/[A-Z]/g, c => '-' + c.toLowerCase());
    attrs.push(`${attr}="${v}"`);
  }
  for (const [k, v] of Object.entries(el._attrs || {})) {
    attrs.push(`${k}="${v}"`);
  }
  const kids = (el.children || []).map(serializeEl).join('');
  return `<${el.tagName}${attrs.length ? ' ' + attrs.join(' ') : ''}>${el._innerHTML || ''}${kids}</${el.tagName}>`;
}

function makeEl(tag = 'div', id = '') {
  const el = {
    tagName: tag, id, className: '', _innerHTML: '', textContent: '', dataset: {}, style: {},
    _attrs: {}, _listeners: {}, children: [],
    clientWidth: 1000, clientHeight: 460,
    get innerHTML() {
      if (this.children.length) return (this._innerHTML || '') + this.children.map(serializeEl).join('');
      return this._innerHTML;
    },
    set innerHTML(v) {
      this._innerHTML = v;
      if (typeof v === 'string') {
        if (v.includes('data-group-grid') &&
            !this.children.some(c => c._attrs && ('data-group-grid' in c._attrs))) {
          const grid = makeEl('div');
          grid._attrs['data-group-grid'] = '';
          this.children.push(grid);
        }
        if (v.includes('data-provider-card')) {
          const grid = this.children.find(c => c._attrs && ('data-group-grid' in c._attrs));
          if (grid) {
            const ids = [...v.matchAll(/data-provider-card="([a-z0-9_-]+)"/g)].map(m => m[1]);
            for (const cardId of ids) {
              if (grid.children.some(c => c.dataset.providerCard === cardId)) continue;
              const card = makeEl('div');
              card.dataset.providerCard = cardId;
              grid.children.push(card);
            }
          }
        }
      }
    },
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
    querySelectorAll(sel) {
      const out = [];
      const walk = (node) => {
        for (const child of (node.children || [])) {
          if (matchesSel(child, sel)) out.push(child);
          walk(child);
        }
      };
      walk(this);
      return out;
    },
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
  };
  return el;
}

function reg(id, tag = 'div') { const el = makeEl(tag, id); elements[id] = el; return el; }

const navEl = reg('nav-rail', 'nav');
const provContainer = reg('providers-container');
const provChips = reg('providers-chips');
const provCountLine = reg('providers-count-line');
const pvViewAll = reg('pv-view-all', 'button');
const pvViewConfigured = reg('pv-view-configured', 'button');
const pvViewCompact = reg('pv-view-compact', 'button');
const setupBody = reg('setup-body');
const setupToggle = reg('setup-toggle', 'button');
reg('setup-toggle-icon', 'i');
const appRail = reg('app-rail', 'aside');
const railToggle = reg('rail-toggle', 'button');
const railTooltip = reg('rail-tooltip');
const mainEl = reg('main', 'main');
const topoViewport = reg('topology-viewport');
const topoCanvas = reg('topology-canvas');
const topoEdges = reg('topology-edges', 'svg');
const topoNodes = reg('topology-nodes');
const topoSummary = reg('topology-summary', 'p');
const healthStats = reg('health-store-stats');
const healthBody = reg('health-connections-body', 'tbody');
const healthRecent = reg('health-recent-list');
const healthSummary = reg('health-summary', 'p');
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
  'initTopologyInteractions, TOPO, renderHealthStats, renderHealthConnections, ' +
  'renderHealthRecent, formatMs, formatBytes, nodeSettingsHtml, NODE_API_TYPES, renderCatalog, ' +
  'renderLogs, renderLogStats, logToCurl, PROVIDER_METADATA, PROVIDER_CATEGORIES, ' +
  'PROVIDER_CATEGORY_BY_ID, PROVIDERS_FILTER, providerCategoryOf, providerCardStatus, ' +
  'providerIsConfigured, renderProviders, onProvidersSearch, onProvidersModelSearch, ' +
  'setProvidersView, setProvidersCategory, applyProvidersFilter, toggleProviderConfig, ' +
  'toggleSetupSection, windowLabel, renderStatRows, formatUsd, switchTab, loadQuotaPage };',
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
const liveCount = t.FEATURES.filter(f => f.status === 'live').length;
const plannedCount = t.FEATURES.filter(f => f.status === 'planned').length;
check(liveCount + plannedCount === 57, 'every feature must be live or planned');
check((railHtml.match(/bg-emerald-400/g) || []).length === liveCount,
  `expected ${liveCount} green dots for live features`);
check((railHtml.match(/bg-amber-400/g) || []).length === plannedCount,
  `expected ${plannedCount} amber dots for planned features`);
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

// ── live traffic vs recorded tests ────────────────────────────────────────
// A real request is stronger evidence than a test, and fresher when it happened
// after the newest recorded test.
const nowSec = Math.floor(Date.now() / 1000);
const liveProviders = {
  // tested ok long ago, failed live just now -> must read as error
  cursor: {
    test_results: { 'composer-2.5': { status: 'ok', tested_at: nowSec - 3600 } },
    last_test_status: 'ok',
  },
  // tested failed long ago, succeeded live just now -> must read as active
  glm: {
    test_results: { 'glm-5.1': { status: 'error', error: 'GLM upstream HTTP 401', tested_at: nowSec - 3600 } },
    last_test_status: 'error',
  },
  // untested, but serving live traffic
  kiro: { test_results: {} },
  // tested more recently than its live traffic -> the test should win
  copilot: {
    test_results: { 'gh/gpt-4o': { status: 'error', error: 'Copilot upstream HTTP 400', tested_at: nowSec - 10 } },
    last_test_status: 'error',
  },
};
const liveHealth = {
  cursor:  { status: 'error', model: 'auto', latency_ms: 90, error: 'cursor HTTP 401', at: nowSec },
  glm:     { status: 'ok', model: 'glm-5.1', latency_ms: 640, at: nowSec },
  kiro:    { status: 'ok', model: 'kr/sonnet', latency_ms: 1200, at: nowSec },
  copilot: { status: 'ok', model: 'gh/gpt-4o', latency_ms: 300, at: nowSec - 120 },
};

t.renderTopology(liveProviders, [], liveHealth);
const liveNodes = topoNodes._innerHTML;
const liveSummary = topoSummary.innerText || '';

// Split into per-node blocks — a fixed-size character window is unreliable
// because each node's markup (and its tooltip) varies in length.
const nodeBlocks = liveNodes.split('<div class="topo-node').slice(1);
const blockFor = (label) => nodeBlocks.find(b => b.includes(label)) || '';

check(nodeBlocks.length === 23, `expected 23 node blocks, got ${nodeBlocks.length}`);
check(/live traffic/.test(liveSummary), `summary should report live freshness, got "${liveSummary}"`);
check(!/now ago/.test(liveNodes), 'tooltips should not say "now ago"');
check(/just now/.test(liveNodes), 'recent live traffic should read "just now"');

// fresh live failure replaces an older ok test -> red
const cursorBlock = blockFor('Cursor IDE');
check(cursorBlock !== '', 'cursor node not found');
check(/bg-rose-400/.test(cursorBlock), 'a provider failing live traffic should be red');
check(/live failed/.test(cursorBlock), 'the live failure should be described');
check(/cursor HTTP 401/.test(cursorBlock), 'the live error text should reach the tooltip');

// fresh live success replaces an older failed test -> green
const glmBlock = blockFor('Zhipu GLM');
check(glmBlock !== '', 'glm node not found');
check(/bg-emerald-400/.test(glmBlock), 'a provider succeeding live should be green');
check(/live OK 640ms/.test(glmBlock), 'the live success should be described with its latency');

// a more recent test still wins over older live traffic
const copilotBlock = blockFor('GitHub Copilot');
check(copilotBlock !== '', 'copilot node not found');
check(/bg-rose-400/.test(copilotBlock), 'the fresher test failure should keep the node red');
check(/Copilot upstream HTTP 400/.test(copilotBlock), 'the test error text should be shown');
check(/last live traffic/.test(copilotBlock), 'older live traffic should be mentioned as history');

// untested provider with live traffic is active, not "untested"
check(/bg-emerald-400/.test(blockFor('Kiro AI')), 'a provider serving live traffic should be green');

// with no live data at all, behaviour falls back to test results
t.renderTopology(liveProviders, [], {});
check(/no live traffic yet/.test(topoSummary.innerText || ''), 'summary should say when there is no live traffic');

// ── health page ───────────────────────────────────────────────────────────
check(t.formatMs(null) === '—', 'a missing latency should render as a dash, not NaN');
check(t.formatMs(950) === '950ms', `formatMs(950) = ${t.formatMs(950)}`);
check(t.formatMs(12274) === '12.3s', `formatMs(12274) = ${t.formatMs(12274)}`);
check(t.formatBytes(0) === '0 B' && t.formatBytes(2048) === '2.0 KB', 'byte formatting');
check(t.formatBytes(189 * 1048576) === '189.0 MB', `large sizes: ${t.formatBytes(189 * 1048576)}`);

const nowHealth = Math.floor(Date.now() / 1000);
const healthRows = [
  {
    provider: 'antigravity',
    connection: '799e2aa3b3',
    checks: 20,
    ok_count: 19,
    last_at: nowHealth - 30,
    avg_latency_ms: 12274,
    avg_ttft_ms: 9132,
    min_latency_ms: 8000,
    max_latency_ms: 28000,
    ok_rate_pct: 95.0,
    latest: { ok: 1, at: nowHealth - 30, error: null, model: 'gemini-3.8-flash-high' },
  },
  {
    provider: 'cline',
    connection: 'b0cb9d0141',
    checks: 8,
    ok_count: 0,
    last_at: nowHealth - 600,
    avg_latency_ms: 90,
    avg_ttft_ms: null,
    min_latency_ms: 70,
    max_latency_ms: 200,
    ok_rate_pct: 0.0,
    latest: { ok: 0, at: nowHealth - 600, error: 'Cline upstream HTTP 401', model: 'cline-free/…' },
  },
];

t.renderHealthConnections(healthRows, new Set(['cline|b0cb9d0141']));
const healthHtml = healthBody._innerHTML;

check(healthHtml.split('<tr').length - 1 === 2, `expected 2 rows, got ${healthHtml.split('<tr').length - 1}`);
check(/antigravity/.test(healthHtml) && /cline/.test(healthHtml), 'both connections should render');
check(/bg-emerald-400/.test(healthHtml) && /bg-rose-400/.test(healthHtml), 'status dots should reflect ok/fail');
check(/95%/.test(healthHtml) && /0%/.test(healthHtml), 'ok rate should be shown');
check(/12.3s/.test(healthHtml), 'average latency should be humanised');
check(/9.1s/.test(healthHtml), 'TTFT should be shown');
check(/needs action/.test(healthHtml), 'a terminal failure should be flagged for the operator');
check(!/undefined|NaN|null/.test(healthHtml), 'no raw null/undefined should leak into the table');
// the connection id is truncated but the full key stays in the title
check(/title="cline\|b0cb9d0141"/.test(healthHtml), 'the full connection key should be recoverable');
// a missing ttft must not render as a fake number
check(healthHtml.includes('—'), 'a missing TTFT should render as a dash');

t.renderHealthConnections([], new Set());
check(/No probes recorded yet/.test(healthBody._innerHTML), 'an empty table should explain itself');

t.renderHealthStats(
  { size_bytes: 61440, wal_bytes: 0, health_checks: 132, last_vacuum: nowHealth - 3600, retention_days: { health_checks: 30 } },
  { enabled: true, interval_seconds: 60 }
);
const statsHtml = healthStats._innerHTML;
check(/60\.0 KB/.test(statsHtml), 'database size should be formatted');
check(/132/.test(statsHtml), 'probe count should be shown');
check(/30 days/.test(statsHtml), 'retention should be visible');
check(/probing every 60s/.test(statsHtml), 'cadence should be visible');

// a WAL worth worrying about should say so
t.renderHealthStats({ size_bytes: 1, wal_bytes: 185 * 1048576, health_checks: 1, retention_days: { health_checks: 30 } }, {});
check(/checkpointing runs on a schedule/.test(healthStats._innerHTML), 'a large WAL should be called out');

t.renderHealthRecent([
  { provider: 'cursor', model: 'auto', ok: 1, latency_ms: 2068, ttft_ms: 1500, at: nowHealth - 5 },
  { provider: 'glm', model: 'glm-5.3', ok: 0, latency_ms: 80, ttft_ms: null, at: nowHealth - 500, error: 'HTTP 401' },
]);
const recentHtml = healthRecent._innerHTML;
check(/ok/.test(recentHtml) && /fail/.test(recentHtml), 'recent probes should show pass and fail');
check(/ttft/.test(recentHtml), 'recent probes should show TTFT');
check(!/NaN/.test(recentHtml), 'no NaN in the recent list');

t.renderHealthRecent([]);
check(/Nothing probed yet/.test(healthRecent._innerHTML), 'empty recent list should explain itself');

// ── request log ───────────────────────────────────────────────────────────
const logsStats = reg('logs-stats');
const logsBody = reg('logs-body', 'tbody');

t.renderLogStats({ total: 12, ok: 10, failed: 2, ok_rate_pct: 83.3, avg_latency_ms: 1831, avg_ttft_ms: 1690 });
const logsStatsHtml = logsStats._innerHTML;
check(/83\.3%/.test(logsStatsHtml), 'success rate should be shown');
check(/1\.8s/.test(logsStatsHtml), 'average latency should be humanised');
check(/1\.7s/.test(logsStatsHtml), 'average TTFT should be shown');
check(/2 failed/.test(logsStatsHtml), 'failures should be counted');

const nowLogs = Math.floor(Date.now() / 1000);
t.renderLogs([
  { id: 2, at: nowLogs - 30, provider: 'copilot', model: 'gh/gpt-4o', status: 'ok',
    ttft_ms: 1690, latency_ms: 1831, tokens_in: 7, tokens_out: 3, error: null },
  { id: 1, at: nowLogs - 600, provider: 'glm', model: 'glm-5.1', status: 'error',
    ttft_ms: null, latency_ms: 90, tokens_in: 5, tokens_out: 0, error: 'HTTP 401 unauthorized' },
]);
const logsHtml = logsBody._innerHTML;
check(logsHtml.split('<tr').length - 1 === 2, `expected 2 log rows, got ${logsHtml.split('<tr').length - 1}`);
check(/copilot/.test(logsHtml) && /glm-5\.1/.test(logsHtml), 'both requests should render');
check(/openLog\(2\)/.test(logsHtml), 'a row should open its detail');
check(/HTTP 401/.test(logsHtml), 'the upstream error should be visible in the list');
check(!/undefined|NaN/.test(logsHtml), 'no undefined/NaN in the log rows');
// a missing TTFT must render as a dash, not a fake number
check(/—/.test(logsHtml), 'a missing TTFT should render as a dash');

t.renderLogs([]);
check(/No requests match/.test(logsBody._innerHTML), 'an empty log should explain itself');

// curl replay is built from the stored body
const curl = t.logToCurl(
  { id: 5, model: 'copilot/gh/gpt-4o', prompt_preview: 'hi' },
  { request: { model: 'copilot/gh/gpt-4o', messages: [{ role: 'user', content: 'hi' }], stream: false } }
);
check(/^curl -s http:\/\/127\.0\.0\.1:3001\/v1\/chat\/completions/.test(curl), 'curl should target the local gateway');
check(/"messages":\[\{"role":"user"/.test(curl), 'the original messages should be replayed');
// and it must still work when the body has expired
const curlNoBody = t.logToCurl({ id: 6, model: 'glm/glm-5.1', prompt_preview: 'p' }, null);
check(/glm\/glm-5\.1/.test(curlNoBody), 'an expired body should fall back to the preview');

// ── model catalogs ────────────────────────────────────────────────────────
const catalogSummary = reg('catalog-summary', 'p');
const catalogList = reg('catalog-list');

t.renderCatalog({
  providers: {
    groq: { models: 12, manual: 2, synced_at: Math.floor(Date.now() / 1000) - 3600 },
    openrouter: { models: 400, manual: 0, synced_at: Math.floor(Date.now() / 1000) - 60 },
    stale: { models: 3, manual: 0, synced_at: null },
  },
  total_models: 415,
  refresh_hours: 24,
  config_kb: 205,
  test_results_kept: 286,
});

const catalogHtml = catalogList._innerHTML;
const catalogText = catalogSummary.innerText || '';

check(/415 models across 3 providers/.test(catalogText), `summary: ${catalogText}`);
check(/refreshes every 24h/.test(catalogText), 'the refresh cadence should be visible');
check(/config 205 KB/.test(catalogText), 'config size should be visible');
check(/286 results kept/.test(catalogText), 'the config bound should be visible');
check(/never synced/.test(catalogHtml), 'a catalog that never synced must say so');
check(/2 pinned/.test(catalogHtml), 'pinned models should be distinguished from discovered ones');
check(/1h ago/.test(catalogHtml), 'freshness should be humanised');
// busiest provider first
check(catalogHtml.indexOf('openrouter') < catalogHtml.indexOf('groq'), 'catalogs should sort by size');

t.renderCatalog({ providers: {}, total_models: 0 });
check(/No catalogs yet/.test(catalogSummary.innerText || ''), 'an empty catalog set should explain itself');
check(/Nothing discovered yet/.test(catalogList._innerHTML), 'empty list should explain itself');

// ── declarative provider nodes ────────────────────────────────────────────
check(t.NODE_API_TYPES.length === 4, 'four wire formats should be offered');

// a code adapter must not get a node block
check(t.nodeSettingsHtml({ id: 'cursor' }, { token: 'x' }) === '',
  'a non-node provider should render no node settings');
check(t.nodeSettingsHtml({ id: 'al' }, {}) === '', 'a provider with no kind renders no node settings');

const nodeMarkup = t.nodeSettingsHtml({ id: 'deepseek' }, {
  kind: 'node',
  prefix: 'ds',
  api_type: 'openai-compatible',
  base_url: 'https://api.deepseek.com/v1',
  chat_path: '/chat/completions',
  models_path: '/models',
  auth: 'bearer',
  custom_headers: { 'X-Trace': '1' },
  verified_at: Math.floor(Date.now() / 1000) - 120,
  api_key: 'sk-secret-value',
});

// Attributes are HTML-escaped, so decode what the browser would see before asserting.
const decodeAttr = (s) => s
  .replace(/&quot;/g, '"').replace(/&#39;/g, "'")
  .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
const decodedNode = decodeAttr(nodeMarkup);

check(/data-provider="deepseek"/.test(nodeMarkup), 'fields must be attributable to their provider');
check(/data-node-field="prefix"/.test(nodeMarkup) && /data-node-field="api_type"/.test(nodeMarkup),
  'prefix and wire format must be editable');
check(/data-node-field="base_url"/.test(nodeMarkup), 'base_url must be editable');
check(/value="https:\/\/api\.deepseek\.com\/v1"/.test(nodeMarkup),
  'non-secret values should be shown, not blanked');
check(/selected/.test(nodeMarkup), 'the current wire format should be preselected');
check(/"X-Trace":"1"/.test(decodedNode), 'custom headers should round-trip as JSON');
check(/verified/.test(nodeMarkup), 'a verified node should say so');
check(!/sk-secret-value/.test(decodedNode), 'the credential must never be rendered');
// escaping must actually happen, or a header containing a quote would break the attribute
check(/&quot;/.test(nodeMarkup), 'attribute values must be HTML-escaped');

const unverifiedMarkup = t.nodeSettingsHtml({ id: 'newbie' }, { kind: 'node', base_url: 'https://x/v1' });
check(/unverified/.test(unverifiedMarkup), 'a fresh node must read as unverified');
check(/Test node/.test(unverifiedMarkup), 'an unverified node should offer a test');

// the add-node form exists and posts the declarative fields
check(/id="newnode-base-url"/.test(html) && /id="newnode-api-type"/.test(html), 'add-node form missing');
check(/addProviderNode\(true\)/.test(html), 'the form should be able to add-and-test');

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

// ── provider categories + grouped cards ───────────────────────────────────
const metaIds = t.PROVIDER_METADATA.map(m => m.id);
check(new Set(metaIds).size === metaIds.length, 'duplicate PROVIDER_METADATA ids');
const catIds = new Set(t.PROVIDER_CATEGORIES.map(c => c.id));
check(catIds.size === 5, `expected 5 categories, got ${catIds.size}`);
const resolved = metaIds.map(id => {
  const p = t.PROVIDER_METADATA.find(m => m.id === id);
  return [id, t.providerCategoryOf(p, {})];
});
check(resolved.every(([, c]) => catIds.has(c)), 'a metadata provider resolved to an unknown category');

check(t.providerCategoryOf({ id: 'x', fields: [] }, { kind: 'node' }) === 'nodes', 'kind=node must win');
check(t.providerCategoryOf({ id: 'x', dynamic: true }, {}) === 'imported', 'a dynamic provider is imported');
check(t.providerCategoryOf({ id: 'x', fields: [], dynamic: true }, { source: 'omniroute' }) === 'imported', 'an imported source wins for dynamic providers');
check(t.providerCategoryOf({ id: 'cursor', fields: [{ key: 'token' }] }, { source: 'omniroute' }) === 'oauth', 'a mapped provider keeps its kind regardless of import source');
check(t.providerCategoryOf({ id: 'cursor', fields: [] }, { source: 'cline-cli' }) === 'oauth',
  'a local cline-cli session must not count as an import');
check(t.providerCategoryOf({ id: 'brand-new', fields: [{ key: 'api_key' }] }, {}) === 'apikey', 'unknown api_key provider falls back to apikey');

// status tones mirror the topology vocabulary
const toneOf = (p, saved, avail = []) => t.providerCardStatus(p, saved, avail).tone;
check(toneOf({ id: 'a' }, { enabled: false }) === 'zinc', 'disabled must read as zinc');
check(toneOf({ id: 'a' }, { test_results: { m: { status: 'ok' } }, last_test_status: 'ok' }) === 'emerald', 'ok tests must read as emerald');
check(toneOf({ id: 'a' }, { last_test_status: 'error', last_error: 'boom' }) === 'rose', 'failed tests must read as rose');
check(toneOf({ id: 'a', fields: [{ key: 'api_key' }] }, {}, ['a']) === 'amber', 'untested config must read as amber');
check(toneOf({ id: 'a', fields: [] }, {}) === 'emerald', 'keyless providers read as emerald');
check(toneOf({ id: 'a', fields: [{ key: 'api_key' }] }, {}) === 'zinc', 'unconfigured reads as zinc');

// renderProviders builds grouped cards; filtering only hides them, because
// saveAllProviders scans every input[id^="input-"] in the document.
const fixture = {
  cursor: { token: '«redacted:tok»', machine_id: '«redacted:m»', test_results: { 'composer-2.5': { status: 'ok' } }, last_test_status: 'ok', models: ['composer-2.5'] },
  glm: { api_key: '«redacted:k»', last_test_status: 'error', last_error: 'GLM upstream HTTP 401', models: [] },
  kiro: { enabled: false, token: '«redacted:k»', models: [] },
  orcarouter: { api_key: '«redacted:o»', source: 'omniroute', models: ['orca/auto'] },
};
provContainer.children = [];
provContainer._innerHTML = '';
t.renderProviders(fixture, ['cursor', 'glm']);
// Group markup accumulates on each group element; the container itself only
// carries the chips markup, so collect both for the content assertions.
const provHtml = provContainer.children.map(g => g.innerHTML || '').join('\n') + (provChips.innerHTML || '');
const groupCount = provContainer.children.filter(g => (g.className || '').includes('provider-group')).length;
check(groupCount >= 3, `expected grouped sections, got ${groupCount}`);
check(/OAuth & subscription/.test(provHtml) && /API key providers/.test(provHtml), 'category headings missing');
check(/data-provider-card="cursor"/.test(provHtml), 'cursor card missing');
check(/input-cursor-token/.test(provHtml) && /input-glm-api_key/.test(provHtml), 'credential inputs must keep their ids');
check(/config-cursor/.test(provHtml) && /models-list-cursor/.test(provHtml), 'configure area and model list missing');
check(/title="GLM upstream HTTP 401"/.test(provHtml), 'the upstream error must reach the card tooltip');
check(!/\$\{/.test(provHtml), 'unsubstituted placeholder in provider cards');

// applyProvidersFilter hides cards in the live stub DOM. Rebuild a minimal set.
const mkCard = (id, name, cat, configured, models) => {
  const c = makeEl('div');
  c.dataset.providerCard = id;
  c.dataset.name = name.toLowerCase();
  c.dataset.category = cat;
  c.dataset.configured = configured;
  c.dataset.models = models;
  return c;
};
const g1 = makeEl('section'); g1.className = 'provider-group';
g1.children = [mkCard('cursor', 'cursor ide cursor', 'oauth', '1', 'composer-2.5'), mkCard('copilot', 'github copilot copilot', 'oauth', '0', '')];
const g2 = makeEl('section'); g2.className = 'provider-group';
g2.children = [mkCard('glm', 'zhipu glm glm', 'apikey', '1', 'glm-5.1')];
provContainer.children = [g1, g2];
t.PROVIDERS_FILTER.query = ''; t.PROVIDERS_FILTER.modelQuery = ''; t.PROVIDERS_FILTER.view = 'all'; t.PROVIDERS_FILTER.category = null;
t.applyProvidersFilter();
check(g1.children.every(c => !c.classList.contains('hidden')), 'unfiltered cards must all show');
t.onProvidersSearch('glm');
check(g1.children.every(c => c.classList.contains('hidden')) && !g2.children[0].classList.contains('hidden'), 'search must narrow to glm');
t.onProvidersSearch(''); t.onProvidersModelSearch('composer');
check(!g1.children[0].classList.contains('hidden') && g1.children[1].classList.contains('hidden'), 'model search must match cached models');
t.onProvidersModelSearch(''); t.setProvidersView('configured');
check(g1.children[1].classList.contains('hidden') && !g1.children[0].classList.contains('hidden'), 'configured view must hide the unconfigured card');
t.setProvidersView('all'); t.setProvidersCategory('apikey');
check(g1.children.every(c => c.classList.contains('hidden')) && !g2.children[0].classList.contains('hidden'), 'category chips must scope the grid');
t.setProvidersCategory(null);
check(g1.children.every(c => !c.classList.contains('hidden')), 'clearing the chip must restore the grid');
check(provCountLine.innerText === '3 provider(s)', `count line: ${provCountLine.innerText}`);
t.setProvidersView('compact');
check(provContainer.classList.contains('providers-compact'), 'compact view must set the density class');
t.setProvidersView('all');

// ── usage / tokens / provider-stats / costs tables ─────────────────────
check(t.windowLabel(1) === 'last 24h', 'windowLabel(1)');
check(t.windowLabel(30) === 'last 30 days', 'windowLabel(30)');
check(t.formatUsd(null) === 'unknown', 'a null cost must render as unknown, not $0');
check(t.formatUsd(0.004) === '$0.0040', `formatUsd(0.004) = ${t.formatUsd(0.004)}`);
check(typeof t.switchTab === 'function', 'switchTab must exist');

const statTbody = reg('stat-tbody', 'tbody');
t.renderStatRows('stat-tbody', [{ get: r => r.key }, { get: r => r.requests }], [
  { key: 'alpha', requests: 3 },
]);
check(/alpha/.test(statTbody._innerHTML), 'renderStatRows should render rows');
t.renderStatRows('stat-tbody', [{ get: r => r.key }, { get: r => r.requests }], [], 'Nothing here');
check(/Nothing here/.test(statTbody._innerHTML), 'an empty table should explain itself');
const nullTbody = reg('null-tbody', 'tbody');
t.renderStatRows('null-tbody', [{ get: r => r.cost }, { get: r => r.x }], [{ cost: null, x: '' }]);
check(/—/.test(nullTbody._innerHTML), 'null cells must render as a dash');
check(!/null|undefined/.test(nullTbody._innerHTML), 'no raw null/undefined should leak');
check(/id="tab-costs"/.test(html), 'costs tab section missing from markup');
check(/loadCostsPage/.test(html), 'costs loader missing from markup');
check(/id="tab-quota"/.test(html), 'quota tab section missing from markup');
check(typeof t.loadQuotaPage === 'function', 'quota loader must exist');

console.log(`features: ${t.FEATURES.length} | rail buttons: ${buttons.length} | labels: ${labels.length} | groups: ${groupHeaders.length}`);
console.log(`topology: ${paths} edges, ${providerPoints.length} provider nodes | scale ${t.TOPO.scale.toFixed(3)}`);
console.log(failures.length ? 'FAILURES:\n - ' + failures.join('\n - ') : 'ALL CHECKS PASSED');
process.exit(failures.length ? 1 : 0);

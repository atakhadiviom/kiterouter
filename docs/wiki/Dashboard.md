# Dashboard

Zero build — one static file (`src/kiterouter/static/dashboard.html`) served at `/dashboard`. No npm, no bundler; changes go live on reload.

## Feature rail (navigation)

The left rail carries every feature KiteRouter intends to have, not just the ones that work today. It mirrors the page set of OmniRoute 3.8.50 (55 pages) plus KiteRouter's own `overview` and `connect` — **57 entries**, grouped by area, with a collapsible **icon + label** layout: it is expanded by default and collapses to icons only.

- **Collapse toggle** — the round button on the rail's right edge switches between labels and icons only. The choice is remembered per browser (`localStorage`), and `aria-expanded` tracks it.
- Labels are hidden while collapsed, so **tooltips appear only when collapsed** (with labels visible they would be noise). Hover or keyboard-focus for the feature name, its group, and `planned` when it is not built yet.
- **Green dot** = implemented. **Amber dot** = planned only. The rail never implies a feature exists when it does not.
- The rail is **generated from a single `FEATURES` registry** in the dashboard script — adding a feature is one entry, and the rail, labels, group headers, tooltips, page title and stub panel all follow from it. There is deliberately no hand-written button list to drift out of sync.
- Labels cost width but **no vertical space**, which is what a 57-entry rail needs — it already scrolls. Collapsing trades discoverability back for ~170px of content width.

### Opening a planned feature

Planned entries open a **plan + status panel** stating plainly that it is not implemented, then showing what it will do, the OmniRoute behaviour it is based on, and its definition of done as a checklist. Panels are created on first visit rather than shipping 47 empty sections.

### Built today (12)

0a. **Usage** — requests and tokens aggregated from the recorded request history, over a selectable window (24h / 7d / 30d). A **By provider** table shows request count, OK rate, tokens in/out and RTK savings, plus a **per day** breakdown. Token figures are real upstream `usage` when the provider sent one; requests with no usage block are flagged as estimates.
0b. **Tokens** — cards for input, output, **cache read**, **cache write**, **reasoning**, RTK savings, total and request count. These are the values upstreams actually reported: cache and reasoning stay zero for providers that do not send them, and rows built from the chars÷4 fallback are labelled estimates rather than read as measured.
0c. **Provider stats** — per provider: request count, OK rate, **p50/p95 latency and p50/p95 TTFT**, and the last error. Percentiles are computed from the recorded samples (linear interpolation), and TTFT percentiles only use rows that actually reported a first-token time, so a provider that never reports one shows `—` rather than a fabricated number.
0d. **Costs** — spend per provider, model or key over a selectable window, split into **billed** vs **estimated** with an explicit **unknown** count and an `as of` stamp. Cost is recorded only where an upstream reported it — until any provider reports cost this page honestly reports unknowns rather than figures, and a NULL is never rendered as $0.
0e. **Quota** — what providers reported, per provider/connection: remaining %, exhausted state, reset time and exhausted-until, plus the unknown list. A provider that reports nothing reads `unknown`, never 100%. A bare exhaustion with no reset (today: antigravity's 429) is surfaced so you see it, and the router skips an exhausted provider only while a future reset is known.
0f. **Logs** — durable request history from SQLite, not the 100-entry ring buffer the overview uses. Filter by provider, model, status and window (24h / 7d / 30d / all); the header shows matched, success rate, **average latency and average TTFT**. Clicking a row opens its detail: timings, RTK tokens saved, the upstream error verbatim, prompt and response, and a **Copy as cURL** replay built from the stored body. When a body has passed its shorter retention the drawer says so — `expired — bodies are kept for 3 days` — instead of failing. `Load older` pages with a cursor, so rows arriving mid-page cannot cause a skip or a repeat.

0. **Health** — per-connection probe history, backed by SQLite. A **Per connection** table shows last-probe age, OK rate, latency as min / avg / max, average TTFT, and the last result — with a `needs action` badge when a failure is terminal (a revoked or expired credential) rather than transient. Below it, the newest probes with their TTFT, and four cards for the database: size, **WAL size**, probes stored and retention, and when the last vacuum ran. A large WAL is called out explicitly. **Probe now** runs a sweep, which is the same action as the Providers tab.
1. **Provider Topology + Recent Requests (Overview)**
   - **Provider Topology** — a hub-and-spoke graph with KiteRouter at the centre and every provider it knows (the union of built-in adapters and configured ones) placed in rings around it, joined by curved spokes. Zoom in / zoom out / fit controls sit bottom-left, and the graph can be dragged to pan. Layout is deterministic, so nodes never jump between polls; the auto-fit runs once and is not re-applied on the 5-second refresh, and a resize will not override a view you have positioned yourself.
     - Status colours: **green** active (a model really passed), **amber** recent (traffic in the last 10 minutes), **red** error, **grey** untested, and dimmer grey for disabled in config. Hovering a node gives the detail — models that passed, or the actual upstream error text.
     - Status dots are derived from real completion results (`test_results` / `last_test_status`), never from configuration presence alone.
   - **Recent Requests** — Model (status dot + name, colour-coded in/out token counts), Provider, In / Out (`73.8K↑ 310↓`), Latency, and a relative timestamp. Nodes and rows are honest: a failed request shows a red dot carrying the upstream reason on hover.
     - Filter pills (`All` / `OK` / `Errors`), `Clear History`, and row clicks opening the **Request Inspector Modal** (prompt context preview, response preview, upstream error diagnostics, token savings counter, and 1-click **Copy as cURL**).
2. **Providers** — a filter toolbar over provider cards grouped by kind (**OAuth & subscription**, **API key providers**, **Free & keyless**, **Imported connections**, **Provider nodes**), each with a configured/total count pill and a per-group **Test All**. Cards cover the **union** of built-in metadata + config providers, so imported providers always show up. Each card: logo tile, honest status line (Online / Error / Configured-untested / Not configured, derived from real tests), enable toggle, Fetch / Models / Test buttons, and a **Configure** expander holding the credential fields (blank = unchanged), the model list and any node settings.
   - The toolbar has a provider search, a model search (matches cached models), an **All / Configured / Compact** view toggle, count chips per category (click to scope the grid), and the Auto-Fetch / Test-All actions. Filtering only hides cards — every credential input stays in the DOM so Save Settings still sees them.
   - **Setup & imports** sits collapsed below the grid and holds the previous top-of-page panels: credential imports, **Model catalogs** (what has actually been discovered, per provider: model count, how many are `manual` (pinned, so a sync will never remove them), and freshness (`synced 3h ago` / `never synced`), busiest first; the summary also shows the config's own size and how many results it is carrying, because that number is what makes saves slow when it grows), **Add a provider node** (a form for describing a provider instead of shipping code for it; **Add and test** saves it and immediately runs a real completion), **Background health probing** (enable/disable toggle, cadence selector, **Probe Now**, and the last sweep's per-provider results; off by default), and the **Cline connection** (live credential state from `/api/cline/auth/status`; **Re-authenticate Cline** starts a device authorization at `authkit.cline.bot/device`).
   - **Node settings** appear on any `kind: node` card: prefix, wire format, base URL, chat path, models path, auth style and custom headers, shown with their **real values** (they are not secrets, so they are not blanked like credentials), plus a **Test node** button and a `verified 3m ago` / `unverified — test it` badge. Editing a path here is how a 404 is fixed.
3. **Combos** — visual management of multi-model routing chains with priority fallback, round-robin load balancing, or random pooling. Allows creating, editing, testing, and 1-click importing from 9Router.
4. **Connect Tools** — copyable drop-in setup guides and configurations for Cursor IDE, Claude Code CLI, Cline & Roo Code, OpenCode CLI, Continue.dev, Aider CLI, and Python OpenAI SDK.
5. **Playground** — model picker populated from imported/fetched models (including all defined combos and smart `auto`) with a provider filter; value is sent verbatim (prefix intact); output streams live and upstream errors render as errors.

### Planned (45)

Auto-Combo, Routing, Conductor, Resilience, Limits, Endpoint, API Endpoints, Analytics, Activity, Audit, Runtime, System, Cache, Compression, Context, Conversations, Memory, Translator, Discovery, Free Tiers, Free Rankings, Media Providers, API Manager, A2A Protocol, ACP Agents, Agent Skills, CLI Agents, CLI Code, Cloud Agents, MCP Server, Omni Skills, Search Tools, Tools, Batch, Changelog, Chaos, Gamification, Leaderboard, Onboarding, Plugins, Profile, Radar, Relay, Settings, Webhooks.

The build order, the measured reasoning behind it, and the retention decisions are recorded in the plan at `~/.commandcode/plans/kiterouter-apply-measured-needs.md`.

`tests/test_dashboard_nav.py` guards the rail and registry offline: entry count, unique ids, required fields, live entries having a real section, planned entries *not* having one, every planned feature documenting itself, coverage of every OmniRoute page, the collapse toggle and its remembered state, labels and group headers being registry-driven, and every icon name being one validated against lucide.

`tests/test_dashboard_js.py` runs `tests/js/dashboard_ui_check.js` under node (skipped if node is missing). Because the dashboard is vanilla JS with no bundler, its behaviour is checked by running the inline script against a DOM stub: rail rendering and label/group counts, the collapse state machine, topology status classification, ring layout (no overlapping or out-of-bounds nodes, hub kept clear), edge/node colouring per status, tooltip content, and zoom clamping plus fit maths.

## Header Features
- **Online Radar**: Real-time periodic latency ping to `127.0.0.1:3001` with status indicator.
- **Backup**: One-click download of clean `kiterouter-backup.json` configuration file.

## Rail footer actions

Below the feature list the rail carries three actions — **Update**, **Health** (`/health`) and **Backup config** — plus the gateway status dot. They follow the rail's collapse behaviour: icon + label when expanded, icon only when collapsed.

**Update** shows how far behind `origin/main` the checkout is — `Update · 3 behind` (amber) or `Up to date` — and mentions uncommitted local changes instead when there are no commits waiting. Hovering gives the SHA pair, the age of the last remote check, any fetch error, and a warning that a fast-forward pull fails if local changes conflict.

When the rail is **collapsed the label is hidden**, so a status dot carries the state: amber for commits waiting, grey for local changes only, hidden when clean. Clicking asks for confirmation (it replaces the running process), then updates and reloads the gateway in place. The page polls `/health` until the server answers again and only then reloads itself, giving up with `Reload manually` rather than spinning forever. When there is nothing to pull the click is treated as a fresh check against the remote.

## Import section

**Import credentials from other proxies & apps** — explicit buttons for OmniRoute, 9Router, and local-app sources. Results state what was imported **and what was skipped with the reason**.

## Status honesty rules encoded in the UI

- "working" only after a successful real completion through the gateway;
- upstream error text shown verbatim in red;
- untested models stay marked untested/unknown;
- token savings figures render as estimates.

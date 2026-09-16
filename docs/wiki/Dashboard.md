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

Planned entries open a **plan + status panel** stating plainly that it is not implemented, then showing what it will do, the OmniRoute behaviour it is based on, and its definition of done as a checklist. Panels are created on first visit rather than shipping 52 empty sections.

### Built today (5)

1. **Provider Topology (Overview)** — overview with Active / Recent / Error states plus a **Recent Requests** table (Model, In/Out tokens, Latency, Status, When) with:
   - Filter pills (`All`, `200 OK`, `Errors`)
   - `Clear History` button
   - Interactive row clicks opening the **Request Inspector Modal** (prompt context preview, response preview, upstream error diagnostics, token savings counter, and 1-click **Copy as cURL**).
2. **Providers** — dynamic cards for the **union** of built-in metadata + config providers, so imported providers always show up. Each card: enable toggle, credential fields (blank = unchanged), live test button, model fetch button, per-model test status. Two panels sit above the cards:
   - **Background health probing** — enable/disable toggle, cadence selector, **Probe Now**, and the last sweep's per-provider results (status, model, latency, upstream error, how long ago). Off by default.
   - **Cline connection** — live credential state from `/api/cline/auth/status`: whether credentials are present, whether the access token is expired, the account and its source, whether an OpenRouter fallback exists, whether a local Cline session was found, and any real re-auth reason. **Re-authenticate Cline** starts a device authorization and shows the code to enter at `authkit.cline.bot/device`; the page polls until approval and then reloads state.
3. **Combos** — visual management of multi-model routing chains with priority fallback, round-robin load balancing, or random pooling. Allows creating, editing, testing, and 1-click importing from 9Router.
4. **Connect Tools** — copyable drop-in setup guides and configurations for Cursor IDE, Claude Code CLI, Cline & Roo Code, OpenCode CLI, Continue.dev, Aider CLI, and Python OpenAI SDK.
5. **Playground** — model picker populated from imported/fetched models (including all defined combos and smart `auto`) with a provider filter; value is sent verbatim (prefix intact); output streams live and upstream errors render as errors.

### Planned (52)

Auto-Combo, Routing, Conductor, Resilience, Limits, Quota, Endpoint, API Endpoints, Analytics, Activity, Audit, Costs, Health, Logs, Provider Stats, Runtime, System, Tokens, Usage, Cache, Compression, Context, Conversations, Memory, Translator, Discovery, Free Tiers, Free Rankings, Media Providers, API Manager, A2A Protocol, ACP Agents, Agent Skills, CLI Agents, CLI Code, Cloud Agents, MCP Server, Omni Skills, Search Tools, Tools, Batch, Changelog, Chaos, Gamification, Leaderboard, Onboarding, Plugins, Profile, Radar, Relay, Settings, Webhooks.

The order they get built in, the reasons, and the one open question (whether to add a local SQLite store for history) are recorded in the plan at `~/.commandcode/plans/kiterouter-omniroute-parity-nav.md`.

`tests/test_dashboard_nav.py` guards the rail and registry offline: entry count, unique ids, required fields, live entries having a real section, planned entries *not* having one, every planned feature documenting itself, coverage of every OmniRoute page, the collapse toggle and its remembered state, labels and group headers being registry-driven, and every icon name being one validated against lucide.

## Header Features
- **Online Radar**: Real-time periodic latency ping to `127.0.0.1:3001` with status indicator.
- **Backup**: One-click download of clean `kiterouter-backup.json` configuration file.

## Import section

**Import credentials from other proxies & apps** — explicit buttons for OmniRoute, 9Router, and local-app sources. Results state what was imported **and what was skipped with the reason**.

## Status honesty rules encoded in the UI

- "working" only after a successful real completion through the gateway;
- upstream error text shown verbatim in red;
- untested models stay marked untested/unknown;
- token savings figures render as estimates.

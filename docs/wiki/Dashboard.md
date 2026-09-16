# Dashboard

Zero build — one static file (`src/kiterouter/static/dashboard.html`) served at `/dashboard`. No npm, no bundler; changes go live on reload.

## Tabs

1. **Provider Topology (Overview)** — overview with Active / Recent / Error states plus a **Recent Requests** table (Model, In/Out tokens, Latency, Status, When) with:
   - Filter pills (`All`, `200 OK`, `Errors`)
   - `Clear History` button
   - Interactive row clicks opening the **Request Inspector Modal** (prompt context preview, response preview, upstream error diagnostics, token savings counter, and 1-click **Copy as cURL**).
2. **Providers** — dynamic cards for the **union** of built-in metadata + config providers, so imported providers always show up. Each card: enable toggle, credential fields (blank = unchanged), live test button, model fetch button, per-model test status.
3. **Combos** — visual management of multi-model routing chains with priority fallback, round-robin load balancing, or random pooling. Allows creating, editing, testing, and 1-click importing from 9Router.
4. **Connect Tools** — copyable drop-in setup guides and configurations for Cursor IDE, Claude Code CLI, Cline & Roo Code, OpenCode CLI, Continue.dev, Aider CLI, and Python OpenAI SDK.
5. **Playground** — model picker populated from imported/fetched models (including all defined combos and smart `auto`) with a provider filter; value is sent verbatim (prefix intact); output streams live and upstream errors render as errors.

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

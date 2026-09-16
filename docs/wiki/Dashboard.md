# Dashboard

Zero build — one static file (`src/kiterouter/static/dashboard.html`) served at `/dashboard`. No npm, no bundler; changes go live on reload.

## Tabs

1. **Provider Topology (first page)** — overview with Active / Recent / Error states plus a **Recent Requests** table (Model, In/Out tokens, When) fed by `GET /api/recent-requests`, polling ~3s. It only lists observed requests.
2. **Providers** — dynamic cards for the **union** of built-in metadata + config providers, so imported providers always show up. Each card: enable toggle, credential fields (blank = unchanged), live test button, model fetch button, per-model test status.
3. **Playground** — model picker populated from imported/fetched models with a provider filter; value is sent verbatim (prefix intact); output streams live and upstream errors render as errors.

## Import section

**Import credentials from other proxies & apps** — explicit buttons for OmniRoute, 9Router, and local-app sources. Results state what was imported **and what was skipped with the reason**.

## Status honesty rules encoded in the UI

- "working" only after a successful real completion through the gateway;
- upstream error text shown verbatim in red;
- untested models stay marked untested/unknown;
- token savings figures render as estimates.

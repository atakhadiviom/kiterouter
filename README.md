# KiteRouter 🪁

**Zero-build AI gateway for coding CLIs.** One Python process on `127.0.0.1:3001` that connects Claude Code, Cursor, Antigravity, Cline, OpenCode, Codex, and Hermes to many AI providers — with automatic fallback, RTK token compression, a live dashboard, and honest per-model status.

> Docs: see the [repo wiki](https://github.com/atakhadiviom/kiterouter/wiki) for full documentation (kept up to date with every feature change).

## Quick start

```bash
cd kiterouter
uv run uvicorn kiterouter.server:app --host 127.0.0.1 --port 3001
# Dashboard: http://127.0.0.1:3001/dashboard
# Health:    http://127.0.0.1:3001/health
```

KiteRouter's port is hard-locked to **3001** in the code so it can never collide with OmniRoute (port 20128), even while importing credentials from it.

## Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/chat/completions` | POST | OpenAI-compatible completions (prefix-routed, e.g. `cursor/claude-3-5-sonnet` or combos e.g. `combo/Own`) |
| `/v1/messages` | POST | Anthropic-compatible messages |
| `/v1/models`, `/api/v1/models` | GET | All models from all configured providers + combos |
| `/dashboard` | GET | Zero-build dashboard (collapsible feature rail, Provider Topology, Recent Requests, Providers, Combos, Playground) |
| `/api/config` | GET/POST | Read (secrets redacted) / save configuration |
| `/api/combos` | GET/POST | List and create multi-model routing combos |
| `/api/combos/{name}` | PUT/DELETE | Update or delete a model combo |
| `/api/combos/import-9router` | POST | Import and translate combos from 9Router SQLite DB |
| `/api/test-combo` | POST | Test combo execution and failover latency |
| `/api/sync-source` | POST | Import credentials from OmniRoute or 9Router — **only credentials that actually decrypt & validate are imported** |
| `/api/fetch-token` | POST | Auto-discover tokens from local apps (Cursor state.vscdb, gh auth, claude.json, …) |
| `/api/fetch-models`, `/api/fetch-all-models` | POST | Fetch live model catalogs per provider / all providers |
| `/api/test-provider`, `/api/test-model`, `/api/test-all-models` | POST | **Real** live completion tests — honest pass/fail, errors surfaced as errors |
| `/api/recent-requests` | GET | Recent request log for the dashboard |
| `/api/logs`, `/api/logs/{id}` | GET | Durable request history from SQLite, with body artifacts |
| `/api/usage`, `/api/tokens` | GET | Recorded request/token totals over a window, grouped by provider, model or combo |
| `/api/provider-stats` | GET | Per-provider volume, success rate and p50/p95 latency **and TTFT** |
| `/api/store` | GET | Database and WAL size, retention, last vacuum |
| `/health` | GET | Health check |

## Providers

Built-in adapters: `cursor` (uses `agent.v1.AgentService/Run` Connect-RPC over HTTP/2, auto-discovers agent host via `GetServerConfig`, CLI-impersonation to avoid outdated-version errors), `antigravity` (auto Google OAuth refresh, Cloud Code Assist format, honest GCP quota reporting), `opencode_free` (public zero-auth with `Bearer public` and desktop headers, dynamic catalog), `opencode_go` (OpenCode Go subscription routing over `zen/go/v1` with reasoning streaming), `cline` (dual routing to official Cline API or OpenRouter with token refresh), `claude`, `codex`, `glm`, `minimax`, `kiro`, `copilot`, `vertex`, `custom`, `command_code`. Additional providers arrive via import from OmniRoute/9Router and render automatically in the dashboard.

Prefix routing: send `"model": "<provider>/<model-id>"` (aliases, e.g. `cx/…` codex, `cmd/…` command_code, `cc/…` claude, `gh/…` copilot, `kr/…` kiro).

## Combos & Multi-Model Routing

KiteRouter Combos group multiple AI models into a unified virtual model with automatic failover, load balancing, or random pooling:

- **Strategies**:
  - `fallback` (Priority Fallback): Evaluates models sequentially (`m1 → m2 → m3`), instantly failing over to the next candidate on HTTP error (401, 403, 429, 5xx) or timeout.
  - `round-robin` (Load Balancing): Distributes incoming prompts sequentially across candidate models with automatic failover.
  - `random` (Random Pool): Shuffles candidates randomly with automatic failover.
- **Client Invocations**: Use `combo/<name>` or `<name>` directly in any OpenAI-compatible client.
- **Management & Discovery**: Visual chain builder on the Dashboard (`/dashboard`), 1-click import from 9Router, and model dropdown in the Test Playground.

## Zero-config smart routing (`auto`)

Point any AI tool at `http://127.0.0.1:3001/v1` and request `model: "auto"` or `model: "default"`. KiteRouter automatically selects the highest-priority working provider and falls over transparently if rate limits, auth expired, or quota exhaustion occur.

## Connect developer tools in 1 click

The dashboard's **Connect Tools** tab provides copyable drop-in configurations for:
- **Cursor IDE** (OpenAI compatible override)
- **Claude Code CLI** (`ANTHROPIC_BASE_URL="http://127.0.0.1:3001/v1"`)
- **Cline & Roo Code**
- **OpenCode CLI** (`~/.config/opencode/opencode.json`)
- **Continue.dev** (`~/.continue/config.json`)
- **Aider CLI** (`OPENAI_API_BASE="http://127.0.0.1:3001/v1"`)
- **Python OpenAI SDK**

## Live request inspector

Clicking any row in the Recent Requests table opens a comprehensive Request Inspector modal featuring:
- Exact prompt & response previews
- Token metrics (prompt tokens, completion tokens, RTK tokens saved)
- Upstream error diagnostics
- One-click **Copy as cURL** command to replay requests in the terminal

## Feature rail

The dashboard's left rail gives every intended capability a home — 57 entries mirroring OmniRoute's dashboard page set, generated from a single `FEATURES` registry. It shows **icon + label** by default and collapses to icons only via the toggle on its edge, remembering the choice. A **green dot** means implemented and an **amber dot** means planned. Clicking a planned entry opens a plan + status panel describing what it will do, the upstream behaviour it is based on, and its definition of done — the UI never presents an unbuilt feature as working. See the [Dashboard wiki page](https://github.com/atakhadiviom/kiterouter/wiki/Dashboard).

## Import policy (only what works)

Credentials imported from OmniRoute (`~/.omniroute/storage.sqlite`, read-only SQLite) or 9Router (`~/.9router/db/data.sqlite`) are decrypted locally with OmniRoute's own `STORAGE_ENCRYPTION_KEY` (AES-256-GCM, `enc:v1:` envelopes) and validated before import:

- Undecryptable or wrong-typed credentials are **skipped entirely** and reported with a reason — never transmitted upstream.
- Sync responses include per-provider `skipped` reasons ("encrypted-import-unsupported", "invalid-type").
- An empty sync reports "no usable credentials imported" — never fake success.
- Existing working credentials are never overwritten by a broken import.

## Honest status

Model/provider status in the dashboard comes **only from real completions**. A configured key that hasn't been tested shows "untested"; upstream errors show as red errors, never "READY".

## RTK token compression

`enable_rtk: true` strips terminal bloat, ANSI codes, and oversized tool-result bodies from request context, reducing token usage per turn.

## Tests

```bash
uv run pytest tests/ -q
```

## Notes

Private research/personal project. All credentials stay masked in API responses (`[REDACTED]`) and are never logged, printed, or persisted beyond the local config file.

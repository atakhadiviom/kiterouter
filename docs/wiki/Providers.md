# Providers

## Built-in adapters

| Provider id | Alias | Auth | Notes |
|---|---|---|---|
| `cursor` | — | session token from Cursor IDE (`state.vscdb`) / `cursor-agent` | Connect-RPC `agent.v1.AgentService/Run` over HTTP/2 with CLI impersonation; live status see [[Troubleshooting]] |
| `antigravity` | — | Google OAuth token + refresh token | Auto OAuth refresh against `oauth2.googleapis.com`; translates OpenAI format to Cloud Code Assist `contents`/`request`; honest subscription/quota reporting |
| `opencode_free` | — | none (public) | Uses `Bearer public` and desktop client headers; supports `nemotron-3-ultra-free`, `mimo-v2.5-free`, `ling-3.0-flash-fin-free`, `muse-spark-1.3-contributor-free`, `muse-spark-1.2-contributor-free` (auto-routed via OpenAI Responses API `/zen/v1/responses`); dynamic catalog via `zen/v1/models` |
| `opencode_go` | — | `api_key` (`OPENCODE_API_KEY`) | `https://opencode.ai/zen/go/v1/chat/completions` and `/responses`; requires session IDs; dynamic model catalog (37+ models) via `zen/go/v1/models` |
| `cline` | — | Cline OAuth session (CLI/extension) or API key | `https://api.cline.bot/api/v1`; proactive token refresh, local-session discovery, and OpenRouter fallback when Cline auth is refused (see below) |
| `claude` | `cc` | `~/.claude.json` OAuth | Anthropic native |
| `codex` | `cx` | OAuth token / `OPENAI_API_KEY` | ChatGPT/Codex backend |
| `glm` | — | `api_key` | Zhipu GLM |
| `minimax` | — | `api_key` (`MINIMAX_API_KEY`) | |
| `kiro` | `kr` | token | |
| `copilot` | `gh` | `gh auth token` / Copilot hosts.json | |
| `vertex` | — | `VERTEX_API_KEY` | |
| `custom` | — | any | OpenAI-compatible base URL |
| `command_code` | `cmd` | API key | `commandcode.ai` Claude/GPT models |

## Model catalogs (discovered, with provenance)

Provider model lists change constantly, so they are **discovered and stored** rather than hardcoded. Catalogs live in `kiterouter.db`, not `config.json` — config is rewritten wholesale on every save, and accumulating hundreds of model entries there made every save slower and the file larger (205 KB at its worst).

Every entry carries a **source**:

- **`manual`** — a model pinned in config. **Never** removed or reclassified by a sync; absent upstream does not mean the operator was wrong.
- **`discovery`** — an observation. It expires once the provider stops listing it (`catalog_unseen_days`, default 14).

Refreshed daily (`catalog_refresh_hours`, default 24) and on demand from the dashboard or `POST /api/models/refresh`. **A sync shows up on the next request, not the next restart** — the router reads the catalog through a live accessor, so `/v1/models` grows as soon as the catalog does. Observed here: 107 → 676 models while the process kept running.

Freshness is surfaced per provider ("synced 3h ago", or "never synced") because a stale catalog otherwise shows up later as a confusing 400 from a chat endpoint rather than as an obviously old list.

Config is kept bounded in exchange: per-model results older than `retention_test_results_days` (30) are dropped and each provider is capped at `max_test_results_per_provider` (60), applied whenever results are written. The full history lives in the store, so nothing is lost — trimming the config took it from **205 KB to 100 KB**.

## Provider nodes (providers as data)

Most providers change constantly — a new model id, a renamed path, a different host. KiteRouter therefore lets a provider be **described**, not compiled. An entry with `kind: "node"` is driven entirely by config:

```json
"deepseek": {
  "kind": "node",
  "enabled": true,
  "prefix": "ds",
  "api_type": "openai-compatible",
  "base_url": "https://api.deepseek.com/v1",
  "chat_path": "/chat/completions",
  "models_path": "/models",
  "auth": "bearer",
  "custom_headers": {},
  "api_key": "…"
}
```

- **`api_type`** — `openai-compatible` (default), `openai-responses`, `anthropic`, `gemini`. Each selects the request/response translation, so an Anthropic- or Gemini-shaped endpoint works without a bespoke adapter.
- **`auth`** — `bearer`, `x-api-key`, or `none`. `custom_headers` are merged in and win over the defaults.
- **`prefix`** — lets you route with a short id (`ds/…`) in addition to the provider id itself. A prefix that would shadow an existing alias is **refused**, because that would silently reroute another provider.
- **`chat_path` / `models_path`** — joined to `base_url`; either may be a full URL. A Gemini path may contain `{model}`, which is substituted per request.
- **Seeding** — well-known ids (groq, openrouter, deepseek, opencode) get a sensible `base_url` and starter model list, but every field stays overridable. That is the point: the imported providers currently failing with HTTP 404/400 fail on baked-in assumptions, and each is now a form edit.

**Saving takes effect on the next request — no restart.** Saving config already rebuilds the router, so a node added or corrected is immediately routable.

### Verification, not assumption

A node is **unverified** until it answers a real completion; a plausible-looking `base_url` proves nothing. `POST /api/providers/node/validate` fetches the catalog and runs a real streaming completion, then records `verified_at` (only on success) or `last_error` (on failure). The dashboard shows `verified 3m ago` or `unverified — test it`.

### What stays as code

Providers needing real protocol work keep hand-written adapters — Cursor's Connect-RPC `agent.v1.AgentService/Run`, Antigravity's Cloud Code Assist, Codex, Copilot. Those are not expressible as paths and headers, and OmniRoute keeps code adapters alongside its nodes for the same reason.

## Routing

Prefix the model with the provider id:

```
"model": "cursor/claude-3-5-sonnet"
"model": "cc/claude-opus-4-7"
"model": "gh/gpt-4o"
```

Model IDs pass through **verbatim** (slash-containing IDs like `anthropic/claude-3.5-sonnet` work via the `openrouter` prefix).

## Auto-discovery of tokens

`POST /api/fetch-token` pulls credentials from local stores, read-only:

- Cursor: `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` (`cursorAuth/accessToken`), `~/.cursor/auth.json`
- Cline: `~/.cline/data/settings/providers.json` (Cline CLI session) plus VS Code/Cursor extension storage
- Copilot: `gh auth token`
- Claude: `~/.claude.json`
- OmniRoute/9Router: their SQLite DBs (see [[Credential Import (OmniRoute and 9Router)]])

## Cline authentication

Cline issues short-lived (~1 hour) WorkOS access tokens plus a long-lived refresh token. KiteRouter handles that lifecycle explicitly:

1. **Local-session discovery** — the live Cline CLI session is preferred over credentials imported from another router's database. The Cline refresh token does not rotate, so sharing it is safe; but a *stale copy* refreshes with `400 invalid_grant`, which is indistinguishable from a revoked account at call time.
2. **Expiry-aware refresh** — the access token is refreshed ahead of expiry (5-minute buffer), so an expired token never costs a request. `expiresAt` is normalized from every encoding Cline and its neighbours emit (ISO-8601 with or without milliseconds, or epoch milliseconds).
3. **Recovery on refusal** — a rejected refresh token triggers one harvest of the newest local session and a single retry.
4. **Honest re-auth state** — when the Cline account itself is no longer linked upstream (`userInfo.accounts` is `null`), the adapter reports `reauth_required` with the real upstream reason and persists it, rather than presenting a valid-looking token as healthy.
5. **Interactive re-auth** — `POST /api/cline/auth/start` begins a WorkOS device authorization; approve the code at `https://authkit.cline.bot/device` and `POST /api/cline/auth/poll` persists the resulting session. Exposed in the dashboard as **Re-authenticate Cline**.
6. **OpenRouter fallback** — when Cline refuses the request and an OpenRouter key is configured, the request is served through OpenRouter instead of failing. The model is mapped **only on an exact catalog match** (`cline-free/deepseek-v4.1-flash` → `deepseek/deepseek-v4.1-flash`, or an already vendor-prefixed id passes through). When no equivalent exists the answer is an explicit error naming the model, not a silent substitution to a different — possibly far more expensive — model. Set `openrouter_model` on the provider block to force a specific target.

## Imported providers

Providers synced from OmniRoute/9Router that have **no built-in adapter** (e.g. `groq`, `openrouter`, `agentrouter`, `qwen_cloud_token_plan`) still appear in the dashboard and accept routing via the `custom`-style passthrough, but full first-class adapters are a separate work item — check live test status in the dashboard before relying on them.

Note that an imported provider with no model list probes as **unavailable — no models configured** rather than being sent a request for a placeholder model id.

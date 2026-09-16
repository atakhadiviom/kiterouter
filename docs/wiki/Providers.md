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

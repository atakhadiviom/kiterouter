# Providers

## Built-in adapters

| Provider id | Alias | Auth | Notes |
|---|---|---|---|
| `cursor` | — | session token from Cursor IDE (`state.vscdb`) / `cursor-agent` | Connect-RPC `agent.v1.AgentService/Run` over HTTP/2 with CLI impersonation; live status see [[Troubleshooting]] |
| `antigravity` | — | Google OAuth token + refresh token | Auto OAuth refresh against `oauth2.googleapis.com`; translates OpenAI format to Cloud Code Assist `contents`/`request`; honest subscription/quota reporting |
| `opencode_free` | — | none (public) | Uses `Bearer public` and desktop client headers; supports `nemotron-3-ultra-free`, `mimo-v2.5-free`, `ling-3.0-flash-fin-free`; dynamic catalog via `zen/v1/models` |
| `opencode_go` | — | `api_key` (`OPENCODE_API_KEY`) | `https://opencode.ai/zen/go/v1/chat/completions`; requires session IDs; dynamic model catalog (37+ models) via `zen/go/v1/models` |
| `cline` | — | API key / extension token | Dual routing to `https://api.cline.bot` (for extension JWTs) or OpenRouter; token refresh support; honest upstream auth reporting |
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
- Copilot: `gh auth token`
- Claude: `~/.claude.json`
- OmniRoute/9Router: their SQLite DBs (see [[Credential Import (OmniRoute and 9Router)]])

## Imported providers

Providers synced from OmniRoute/9Router that have **no built-in adapter** (e.g. `groq`, `openrouter`, `agentrouter`, `qwen_cloud_token_plan`) still appear in the dashboard and accept routing via the `custom`-style passthrough, but full first-class adapters are a separate work item — check live test status in the dashboard before relying on them.

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
| `/v1/chat/completions` | POST | OpenAI-compatible completions (prefix-routed, e.g. `cursor/claude-3-5-sonnet`) |
| `/v1/messages` | POST | Anthropic-compatible messages |
| `/v1/models`, `/api/v1/models` | GET | All models from all configured providers |
| `/dashboard` | GET | Zero-build dashboard (Provider Topology, Recent Requests, Providers, Playground) |
| `/api/config` | GET/POST | Read (secrets redacted) / save configuration |
| `/api/sync-source` | POST | Import credentials from OmniRoute or 9Router — **only credentials that actually decrypt & validate are imported** |
| `/api/fetch-token` | POST | Auto-discover tokens from local apps (Cursor state.vscdb, gh auth, claude.json, …) |
| `/api/fetch-models`, `/api/fetch-all-models` | POST | Fetch live model catalogs per provider / all providers |
| `/api/test-provider`, `/api/test-model`, `/api/test-all-models` | POST | **Real** live completion tests — honest pass/fail, errors surfaced as errors |
| `/api/recent-requests` | GET | Recent request log for the dashboard |
| `/health` | GET | Health check |

## Providers

Built-in adapters: `cursor`, `antigravity`, `opencode_free`, `opencode_go`, `cline`, `claude`, `codex`, `glm`, `minimax`, `kiro`, `copilot`, `vertex`, `custom`, `command_code`. Additional providers arrive via import from OmniRoute/9Router and render automatically in the dashboard.

Prefix routing: send `"model": "<provider>/<model-id>"` (aliases, e.g. `cx/…` codex, `cmd/…` command_code, `cc/…` claude, `gh/…` copilot, `kr/…` kiro).

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

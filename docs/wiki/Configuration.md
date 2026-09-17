# Configuration

Config lives at `~/.kiterouter/config.json` and is editable from the dashboard (Providers page) or `POST /api/config`.

## Top-level keys

| Key | Default | Meaning |
|---|---|---|
| `port` | `3001` | **Hard-locked** to 3001 in `KiteConfig.save()` — syncs can never change it, protecting OmniRoute's port 20128 |
| `enable_rtk` | `true` | RTK prompt compression (strips terminal bloat, ANSI, oversized tool results) |
| `max_tool_chars` | `12000` | Cap on tool-result body size before truncation |
| `enable_prober` | `false` | Background health prober. Off by default; enable from the dashboard or `POST /api/prober` |
| `prober_interval_seconds` | `900` | Delay between probe sweeps (minimum 60) |
| `prober_delay_seconds` | `3.0` | Pause between connections within one sweep — keeps probing from looking like a burst |
| `prober_timeout_seconds` | `90` | Per-probe timeout. Some providers are genuinely slow (Cursor's `auto` averages 56s), so this is a knob rather than a constant |
| `prober` | `{}` | Prober state (last sweep, per-connection results). Optional `prober.models` overrides which models are probed, e.g. `{"models": {"cline": ["cline-free/solar-pro4"]}}` |
| `retention_health_checks_days` | `30` | Probe history kept in `kiterouter.db` |
| `retention_requests_days` | `30` | Request log retention (used from Wave 3) |
| `retention_bodies_days` | `3` | Request/response artifact retention (used from Wave 3) |
| `retention_usage_days` | `365` | Usage and quota rollups (used from Waves 2 and 4) |
| `store_maintenance_seconds` | `900` | How often prune + WAL checkpoint (+ weekly vacuum) runs |
| `retention_test_results_days` | `30` | Per-model results kept **in config.json**, which is rewritten wholesale on save. The full history is in the store |
| `max_test_results_per_provider` | `60` | Hard cap per provider, applied as a backstop when results are written |
| `catalog_refresh_hours` | `24` | How often provider model catalogs are re-discovered |
| `catalog_unseen_days` | `14` | A discovered model unseen for this long is expired. **`manual` entries are exempt** |
| `providers` | `{}` | Per-provider credential blocks (below) |
| `combos` | `{}` | Named multi-model routing chains with strategy and candidate models |

## Provider blocks

Each provider entry accepts:

- `enabled` (bool)
- `token` / `access_token` / `refresh_token` — OAuth/session tokens (Cursor, Antigravity, Kiro, Codex)
- `api_key` — API-key providers (GLM, MiniMax, Vertex, Custom, Command Code)
- `base_url` / `endpoint` — optional override
- `machine_id` — stable per-token machine/session identity (anti-ban)
- `source` — where it came from (`local`, `cursor-ide`, `omniroute`, `9router`)

Cline adds two keys of its own:

- `expires_at` — access-token expiry (epoch seconds); tokens are refreshed ahead of it
- `openrouter_model` — optional; forces the OpenRouter model used when Cline auth is refused, instead of resolving one from OpenRouter's live catalog

### Declarative provider nodes

An entry with `kind: "node"` is driven entirely by config rather than by a built-in adapter, so a changed endpoint or model id needs no code change. See [[Providers]] for the full description.

| Key | Meaning |
|---|---|
| `kind` | `node` marks a declarative provider |
| `prefix` | Short routing alias (`ds/…`). Refused if it would shadow an existing alias |
| `api_type` | `openai-compatible` (default) · `openai-responses` · `anthropic` · `gemini` |
| `base_url` | Upstream root; `chat_path` and `models_path` join to it |
| `chat_path` | Defaults per `api_type`; may contain `{model}`; may be a full URL |
| `models_path` | Catalog endpoint, used by model fetches and node validation |
| `auth` | `bearer` · `x-api-key` · `none` |
| `custom_headers` | Object merged into request headers; wins over defaults |
| `verified_at` | Set **only** by a passing real completion, via node validation |
| `last_error` | Why the last validation failed, when it did |

Example (values are yours, never commit real keys):

```json
{
  "port": 3001,
  "enable_rtk": true,
  "providers": {
    "cursor": { "enabled": true, "token": "…", "machine_id": "…" },
    "glm":    { "enabled": true, "api_key": "…" }
  }
}
```

## Secrets handling

- `GET /api/config` returns `[REDACTED]` for every secret field.
- Secrets are never printed to logs, the dashboard, or git.

## Environment aliases used by adapters

`CURSOR_TOKEN`, `CURSOR_MACHINE_ID`, `OPENAI_API_KEY`, `MINIMAX_API_KEY`, `VERTEX_API_KEY` — set as fallbacks when config lacks a key.

Related pages: [[Credential Import (OmniRoute and 9Router)]], [[Providers]]

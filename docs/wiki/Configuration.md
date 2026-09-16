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
| `prober_delay_seconds` | `3.0` | Pause between providers within one sweep — keeps probing from looking like a burst |
| `prober` | `{}` | Prober state (last sweep, per-provider results). Optional `prober.models` overrides which models are probed, e.g. `{"models": {"cline": ["cline-free/solar-pro4"]}}` |
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

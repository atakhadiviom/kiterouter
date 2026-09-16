# Configuration

Config lives at `~/.kiterouter/config.json` and is editable from the dashboard (Providers page) or `POST /api/config`.

## Top-level keys

| Key | Default | Meaning |
|---|---|---|
| `port` | `3001` | **Hard-locked** to 3001 in `KiteConfig.save()` — syncs can never change it, protecting OmniRoute's port 20128 |
| `enable_rtk` | `true` | RTK prompt compression (strips terminal bloat, ANSI, oversized tool results) |
| `max_tool_chars` | — | Cap on tool-result body size before truncation |
| `providers` | `{}` | Per-provider credential blocks (below) |

## Provider blocks

Each provider entry accepts:

- `enabled` (bool)
- `token` / `access_token` / `refresh_token` — OAuth/session tokens (Cursor, Antigravity, Kiro, Codex)
- `api_key` — API-key providers (GLM, MiniMax, Vertex, Custom, Command Code)
- `base_url` / `endpoint` — optional override
- `machine_id` — stable per-token machine/session identity (anti-ban)
- `source` — where it came from (`local`, `cursor-ide`, `omniroute`, `9router`)

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

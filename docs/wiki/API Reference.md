# API Reference

Base: `http://127.0.0.1:3001`

## Inference

### `POST /v1/chat/completions`

OpenAI-compatible. Route with a provider prefix or a combo identifier:

```json
{ "model": "cursor/claude-3-5-sonnet", "messages": [{"role":"user","content":"hi"}], "stream": true }
```

Combos can be requested with or without the `combo/` prefix:

```json
{ "model": "combo/Own", "messages": [{"role":"user","content":"hi"}] }
{ "model": "coding-power", "messages": [{"role":"user","content":"hi"}] }
```

Streaming uses standard SSE (`data: …` chunks, `data: [DONE]`). When routing through a combo, upstream errors trigger automatic fallback to subsequent models in the chain before client streaming begins.

### `POST /v1/messages`

Anthropic-compatible.

### `GET /v1/models` and `GET /api/v1/models`

All models across all configured providers, with `owned_by` = provider id, plus all virtual model combos with `owned_by: "kiterouter-combo"` and metadata (`strategy`, `models`).

## Combos API

### `GET /api/combos`
Lists all configured model combos with their routing strategy (`fallback`, `round-robin`, `random`), model sequence, and description.

### `POST /api/combos`
Create a new combo:
```json
{
  "name": "coding-power",
  "strategy": "round-robin",
  "models": ["opencode_go/deepseek-flash", "opencode_free/nemotron-3-ultra-free"],
  "description": "Load-balanced coding combo"
}
```

### `PUT /api/combos/{name}`
Update an existing combo's models, strategy, or description.

### `DELETE /api/combos/{name}`
Delete a combo by name.

### `POST /api/combos/import-9router`
Extracts and translates combos stored in 9Router's SQLite database (`~/.9router/db/data.sqlite`), mapping short prefixes (e.g. `cu/`, `oc/`, `ag/`) to KiteRouter provider models.

### `POST /api/test-combo`
Runs a live end-to-end completion against the combo chain and returns the responding model, response snippet, and latency in milliseconds.

## Dashboard / management

### `GET /api/config` — redated config (secrets `[REDACTED]`)
### `POST /api/config` — save; re-initializes the router; port stays 3001

### `POST /api/sync-source`

```json
{ "source": "omniroute" }   // or "9router"
```

Response includes `imported_providers`, `skipped` (per-provider reason), `skipped_count`. Providers that can't decrypt are **skipped**, not imported. See [[Credential Import (OmniRoute and 9Router)]].

### `POST /api/fetch-token`

```json
{ "provider": "cursor" }    // or null for all discoverable
```

### `POST /api/fetch-models`

```json
{ "provider": "opencode_free" }
```

### `POST /api/fetch-all-models`

Fetches catalogs for every enabled provider.

### `POST /api/test-provider` / `POST /api/test-model` / `POST /api/test-all-models`

Run **real** completions. Results are persisted and surfaced as pass/fail with the actual upstream error text — the dashboard never shows "working" from configuration presence alone.

### `GET /api/recent-requests`
Feeds the dashboard's Recent Requests table (model, tokens in/out, latency, status, prompt preview, response preview, tokens saved).

### `DELETE /api/recent-requests`
Clears request execution logs from memory and disk.

### `GET /api/stats`
Returns aggregated operational telemetry: total requests, success rate percentage, tokens in/out, tokens saved via RTK compression, and average latency.

### `GET /api/config/backup`
Downloads a clean JSON backup file containing all configured providers, models, and combo chains.

### `POST /api/config/restore`
Restores providers, combos, and gateway settings from a JSON payload.

### `GET /health`

`{"status": "ok", ...}`

## Error conventions

All upstream failures are surfaced verbatim as error text in the SSE/response — KiteRouter never translates a 401 into a normal-looking completion.

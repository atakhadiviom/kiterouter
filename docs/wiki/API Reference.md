# API Reference

Base: `http://127.0.0.1:3001`

## Inference

### `POST /v1/chat/completions`

OpenAI-compatible. Route with a provider prefix:

```json
{ "model": "cursor/claude-3-5-sonnet", "messages": [{"role":"user","content":"hi"}], "stream": true }
```

Streaming uses standard SSE (`data: …` chunks, `data: [DONE]`).

### `POST /v1/messages`

Anthropic-compatible.

### `GET /v1/models` and `GET /api/v1/models`

All models across all configured providers, with `owned_by` = provider id.

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

Feeds the dashboard's Recent Requests table (model, tokens in/out, latency, status).

### `GET /health`

`{"status": "ok", ...}`

## Error conventions

All upstream failures are surfaced verbatim as error text in the SSE/response — KiteRouter never translates a 401 into a normal-looking completion.

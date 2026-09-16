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

## Background health prober

### `GET /api/prober`

Current prober state: `enabled`, `running`, `interval_seconds`, `delay_seconds`, `last_run`, `last_duration_ms`, `next_run`, `last_error`, plus `providers` (per-provider `status` / `model` / `latency_ms` / `error` / `reason` / `probed_at`) and `summary` (`ok` / `error` / `unavailable`).

### `POST /api/prober`

```json
{ "enabled": true, "interval_seconds": 900, "delay_seconds": 3.0 }
```

All fields optional. Starts or stops the background loop and persists the settings. The interval is clamped to a 60-second minimum.

### `POST /api/prober/run`

Runs one probe sweep immediately (synchronously) and returns the same payload as `GET /api/prober`.

Probing is deliberately gentle: providers are probed **sequentially** with a pause between them, unavailable providers cost no request, and nothing is retried in a burst. Results are written to the same per-provider `test_results` store as on-demand tests, so status is uniform across the dashboard.

## Cline re-authentication

### `GET /api/cline/auth/status`

Honest, secret-free credential state: `has_access_token`, `has_refresh_token`, `expires_at`, `expires_in_seconds`, `expired`, `email`, `source`, `reauth_required`, `last_auth_error`, `openrouter_fallback`, `last_fallback_reason`, `last_fallback_model`, `local_session_available`, `local_session_source`.

### `POST /api/cline/auth/start`

Begins a WorkOS device authorization. Returns `flow_id`, `user_code`, `verification_uri`, `verification_uri_complete`, `interval`. The `device_code` is kept server-side and never returned.

### `POST /api/cline/auth/poll`

`?flow_id=…` — polls a pending flow once.

- `{"status": "pending"}` while the user has not approved yet (respect `interval`);
- `{"status": "ok", "email": …, "expires_at": …}` once approved and the session is persisted;
- `{"status": "warning"}` when the login succeeded but the account has no linked Cline workspace — upstream calls will still be refused, and saying so is the honest answer;
- `410` if the flow expired (start again), `404` for an unknown flow.

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

`{"status": "ok", ...}` plus `provider_health` (last real outcome per provider, see below) and `prober` state.

`provider_health` is passive: every logged request records `{status, model, latency_ms, at, error?}` for its provider. The dashboard prefers it over a recorded test whenever it is fresher, so the topology reflects reality between test runs. It is kept in memory with a throttled write to `~/.kiterouter/live_health.json` — deliberately not the main config, which is large and rewritten wholesale.

## Provider nodes

### `GET /api/providers/nodes`

Every declarative node with its derived URLs and verification state: `id`, `enabled`, `api_type`, `base_url`, `chat_url`, `models_url`, `auth`, `has_credential`, `custom_headers`, `verified_at`, `last_error`. Never includes the credential itself.

### `POST /api/providers/node/validate`

```json
{ "provider": "deepseek", "model": "deepseek-chat" }
```

Fetches the node's model catalog and runs a **real streaming completion** against it (reusing the prober's `probe_stream`), returning `available`, `model_count`, `models`, and `completion` (`ok`, `model`, `latency_ms`, `ttft_ms`, `error`).

Records the outcome on the node: only a genuine pass sets `verified_at`; a failure records `last_error` and leaves `verified_at` null. An error body counts as a failure — a 200 carrying `HTTP 401` is not a working provider. Returns `400` for a provider that is not a node.

## Health history and storage

### `GET /api/health/connections?days=7`

Per-connection health: `checks`, `ok_count`, `ok_rate_pct`, `last_at`, `min/avg/max_latency_ms`, `avg_ttft_ms`, and the newest `latest` row (including its error text).

A **connection** is identified by provider plus a short hash of its credential material. Swapping a credential produces a *new* connection, so a replacement key does not inherit the previous key's failures — the masking problem per-connection health exists to avoid.

### `GET /api/health/history?limit=100`

Raw probe rows, newest first: `at`, `provider`, `connection`, `model`, `ok`, `latency_ms`, `ttft_ms`, `error`.

`ttft_ms` is measured from a real streaming completion, timed at the first chunk carrying content. Where an adapter buffers the whole body before yielding (Cursor reads `resp.content`), TTFT ends up equal to latency — which is honest, because that is what a client experiences.

### `GET /api/store` and `POST /api/store/maintain`

`/api/store` reports `schema_version`, row count, database and **WAL** size, `last_vacuum`, the path, retention, and the maintenance interval. The WAL is reported because an unmanaged one is how the neighbouring OmniRoute installation reached 185 MB.

`POST /api/store/maintain` prunes past retention, truncates the WAL, and vacuums when the weekly interval has elapsed. A background task does the same on `store_maintenance_seconds` (default 900s), independently of the prober — history stays bounded whether or not background probing is enabled.

Retention is declared once in config: `retention_health_checks_days` and `retention_requests_days` (30), `retention_bodies_days` (3), `retention_usage_days` (365).

### `GET /api/prober` (per-connection)

Alongside the schedule, the prober now reports `connections` (per `provider|connection`), `backoff`, `needs_action`, and `timeout_seconds`. `backoff` holds the consecutive `failures`, `next_at`, `last_error` and `needs_action` per connection: a failing connection is skipped until its backoff expires instead of being retried every sweep, and a terminal failure (401/403/`invalid_grant`/expired/revoked) starts at a six-hour backoff and is flagged for the operator rather than retried.


### `GET /api/update/status?fetch=`

How the checkout compares to its upstream: `behind`, `ahead`, `dirty`, `changed_files`, `branch`, `upstream`, `local_sha`, `remote_sha`, `fetched_at`, `fetch_error`, `can_update`.

`fetch=true` performs a `git fetch`, but no more often than a 300-second TTL (and the response is cached) so the dashboard's poll cannot hammer the remote. `fetched_at` always reports the real age of the comparison, and `fetch_error` is surfaced rather than swallowed.

### `POST /api/update`

```json
{ "restart": true }
```

Fetches, fast-forwards with `git pull --ff-only`, runs `uv sync`, import-checks the new code, then reloads.

- Returns `up_to_date`, `updated` or `error` with a human-readable `message`.
- Refuses when the local branch is **ahead** of upstream, and when `git pull` fails (with the git output included).
- **Never reloads into code that fails to import** — it reports the error instead of taking the gateway down.
- Reloading uses `os.execv`, replacing the process in place so the **PID is preserved** and no supervisor is needed. The previous implementation sent `SIGHUP`, which nothing handles (uvicorn traps only SIGINT/SIGTERM), so it killed the daemon while reporting a successful hot-reload.

## Error conventions

All upstream failures are surfaced verbatim as error text in the SSE/response — KiteRouter never translates a 401 into a normal-looking completion.

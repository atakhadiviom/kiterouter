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

## Self-update

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

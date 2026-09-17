# Troubleshooting

Check live status first: dashboard → Providers → run a **test model** on the failing provider. The error text is the real upstream message; match it below.

| Symptom | Likely cause | Action |
|---|---|---|
| `API key missing. Set X_API_KEY` | No key in config/env for that provider | Add the key in Providers card (or env var) |
| HTTP 401 after import | Key could be valid-but-rejected, or an OAuth token used as API key | Check auth type vs adapter; re-auth the source app; see [[Credential Import (OmniRoute and 9Router)]] |
| `Out of usage` / limit message from Cursor | Token is valid; account reached free/pro usage quota | Expected from Cursor when monthly quota is exhausted; switch model or increase account limits |
| "outdated version" from Cursor | Resolved in 0.1.0 via `agent.v1.AgentService/Run` connect-proto over HTTP/2 | Make sure KiteRouter is updated to the latest revision; stale `AiService/StreamChat` is no longer used |
| Antigravity HTTP 401 / 400 | Expired OAuth access token or schema mismatch | Resolved in 0.1.0: auto-refreshes Google OAuth tokens and wraps messages into Gemini `contents`/`request` format |
| Antigravity HTTP 403 `Cloud Code Private API has not been used` | Dummy GCP project (e.g. `reference-airline-kmj57`) or `x-goog-user-project` header sent on consumer tier | Resolved: KiteRouter uses `aicode-consumers` project context, queries `loadCodeAssist`, and avoids injecting consumer project into GCP service-usage billing headers |
| OpenCode Free HTTP 500 on `muse-spark` | Model requires OpenAI Responses API instead of Chat Completions | Resolved: KiteRouter routes `muse-spark-*` models to `/zen/v1/responses`, formats payload to Responses format, and sets low effort + 4096 tokens budget |
| Empty content with `finish_reason: stop` | Models that answer in `reasoning_content` (DeepSeek-style) were dropped by the **non-streaming** aggregation, which read only `content` | Fixed: aggregation shares `translate.extract_delta_text` with the streaming path. Affected any non-streaming client, and any combo whose first candidate was such a model |
| A request never returns | An upstream holding the connection open with SSE comments (`: keep-alive`) never trips a per-chunk read timeout, because comments are data | Fixed: `providers/streaming.py` watches for real progress and aborts with an honest "stalled" message rather than waiting forever. Applied to every passthrough adapter |
| `OpenCode Free Error: HTTP 403 - can only be used from within OpenCode` | The free tier refuses non-OpenCode clients | Expected, and flagged `needs action` by the prober — no amount of retrying fixes it |
| Update shows `Restart to enable` | The dashboard is served from disk on every request, but **endpoint changes only load on restart** — so a freshly loaded page can meet an older process with no `/api/update` route (HTTP 404) | Restart KiteRouter to load the new code. This is not a GitHub/credential problem |
| Update shows `Update check failed` | The gateway itself could not be reached, or it returned an error. GitHub credentials are **not** involved at this point | Check the gateway is up; `gh auth status` and a manual `git fetch` confirm remote access separately |
| `kiterouter update` reported hot-reload but the gateway died | Historical bug: it sent `SIGHUP`, which nothing handles (uvicorn traps only SIGINT/SIGTERM), so the process was killed | Fixed — the gateway now reloads with `os.execv`, preserving its PID. Update to the latest revision |
| Cline HTTP 401 | Not necessarily an expired token. A refresh token copied from another router's DB fails with `400 invalid_grant`, and a token can be perfectly valid while the linked Cline account is no longer accepted upstream (`userInfo.accounts` is `null`) | Resolved: the adapter discovers the live local Cline session, refreshes ahead of expiry, retries once after re-harvesting, and reports `reauth_required` with the real reason. Check `GET /api/cline/auth/status`, then use **Re-authenticate Cline** on the dashboard if the account itself is unlinked |
| Cline works but is slow / returns an OpenRouter error | Cline auth was refused and the request was served through the OpenRouter fallback | `last_fallback_reason` and `last_fallback_model` in `GET /api/cline/auth/status` name both. An OpenRouter `402` means that account is out of credits, and a model with no exact OpenRouter equivalent errors explicitly — set `openrouter_model` to choose the target |
| Probe shows `unavailable — no models configured` | Imported provider has no model list, so there is nothing real to probe | Fetch its catalog (`POST /api/fetch-models`) or set `prober.models` for it |
| OpenCode Free HTTP 401 | Missing public Bearer auth or client headers | Resolved: uses `Authorization: Bearer public` and `x-opencode-client: desktop` headers |
| OpenCode Go empty error | Dead endpoint or reasoning tokens unmapped | Resolved: routes to `https://opencode.ai/zen/go/v1/chat/completions` with required session IDs and aggregates reasoning content |
| HTTP 404 (e.g. Kiro) | Endpoint path/host changed upstream | Verify the endpoint against the vendor's current docs before changing code |
| Empty content, no error | Upstream accepted the request but model output mapping failed | Enable debug logs for that adapter; check model-id mapping decisions |
| Sync imported fewer providers than the source has | That's the skip policy — reasons are in the sync response `skipped` field | Fix the reason (rotate key in OmniRoute, re-auth) and re-sync |
| KiteRouter not running on 3001 | Process died | Relaunch; check `lsof -nP -iTCP:3001 -sTCP:LISTEN` |
| Port became 20128 | Should be impossible — hard guard; if you ever see it | `KiteConfig.save()` re-forces 3001; check tests `test_config_save_guards_port` |

## Never do

- Don't restart OmniRoute to fix KiteRouter.
- Don't delete-then-reimport credentials when a fix is available — imports never overwrite a working credential.
- Don't trust model *list* success as working status — run real completions.

## Diagnostics

```bash
curl http://127.0.0.1:3001/health
curl -s http://127.0.0.1:3001/api/config | python3 -m json.tool   # secrets redacted
uv run pytest tests/ -q
```

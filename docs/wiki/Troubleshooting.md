# Troubleshooting

Check live status first: dashboard → Providers → run a **test model** on the failing provider. The error text is the real upstream message; match it below.

| Symptom | Likely cause | Action |
|---|---|---|
| `API key missing. Set X_API_KEY` | No key in config/env for that provider | Add the key in Providers card (or env var) |
| HTTP 401 after import | Key could be valid-but-rejected, or an OAuth token used as API key | Check auth type vs adapter; re-auth the source app; see [[Credential Import (OmniRoute and 9Router)]] |
| `Out of usage` / limit message from Cursor | Token is valid; account reached free/pro usage quota | Expected from Cursor when monthly quota is exhausted; switch model or increase account limits |
| "outdated version" from Cursor | Resolved in 0.1.0 via `agent.v1.AgentService/Run` connect-proto over HTTP/2 | Make sure KiteRouter is updated to the latest revision; stale `AiService/StreamChat` is no longer used |
| Antigravity HTTP 401 / 400 | Expired OAuth access token or schema mismatch | Resolved in 0.1.0: auto-refreshes Google OAuth tokens and wraps messages into Gemini `contents`/`request` format |
| Antigravity HTTP 403 `SUBSCRIPTION_REQUIRED` | Google Cloud Code Private API not enabled or individual tier discontinued | Enable Cloud Code Private API on your GCP project or migrate to supported enterprise/Antigravity plan |
| Cline HTTP 401 | Expired/revoked extension token (`invalid_grant`) | Re-authenticate in the Cline VSCode/Cursor extension and re-import credentials |
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

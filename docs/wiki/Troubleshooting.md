# Troubleshooting

Check live status first: dashboard → Providers → run a **test model** on the failing provider. The error text is the real upstream message; match it below.

| Symptom | Likely cause | Action |
|---|---|---|
| `API key missing. Set X_API_KEY` | No key in config/env for that provider | Add the key in Providers card (or env var) |
| HTTP 401 after import | Key could be valid-but-rejected, or an OAuth token used as API key | Check auth type vs adapter; re-auth the source app; see [[Credential Import (OmniRoute and 9Router)]] |
| HTTP 415 / "outdated version" from Cursor | Wrong endpoint or stale client identity | Cursor's `AiService/StreamChat` path is deprecating; the current working path is `agent.v1.AgentService/Run` on the api5 agent URL with CLI-type headers — verify adapter matches |
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

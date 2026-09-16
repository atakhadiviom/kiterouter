# Home

**KiteRouter** — zero-build AI gateway for coding CLIs. Runs on `127.0.0.1:3001`.

Connect Claude Code, Cursor, Cline, OpenCode, Codex, Antigravity, and Hermes to many AI providers with automatic fallback, RTK token compression, a live dashboard, and honest per-model status.

## Pages

- [[Installation]]
- [[Configuration]]
- [[Providers]]
- [[API Reference]]
- [[Dashboard]]
- [[Credential Import (OmniRoute and 9Router)]]
- [[Playground and Live Testing]]
- [[Architecture]]
- [[Troubleshooting]]

## Quick start

```bash
cd kiterouter
uv run uvicorn kiterouter.server:app --host 127.0.0.1 --port 3001
```

- Dashboard: http://127.0.0.1:3001/dashboard
- Health: http://127.0.0.1:3001/health

The port is hard-locked to **3001** in code — it can never become 20128 (OmniRoute's port), even when importing credentials from OmniRoute.

## Design principles

1. **Zero build** — pure Python + a single static HTML dashboard. No npm, no bundler.
2. **Honest status** — pass/fail comes only from real completions; upstream errors are shown as errors.
3. **Import only what works** — credentials that can't be decrypted or validated are skipped with a reported reason, never sent upstream.
4. **Coexist with OmniRoute** — never bind to port 20128, never restart the other proxy.
5. **Secrets stay local** — tokens are masked (`[REDACTED]`) in every API response and never logged.

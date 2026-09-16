# Architecture

## Layout

```
src/kiterouter/
├── server.py          FastAPI app — all /v1/* and /api/* endpoints
├── router.py          ProviderRouter — prefix dispatch, model aggregation
├── config.py          KiteConfig — persistence, port-3001 hard guard
├── token_fetcher.py   Local-app + OmniRoute/9Router credential import (skip-policy)
├── crypto_helper.py   OmniRoute enc:v1: AES-GCM local decryption
├── cline_auth.py      Interactive Cline re-auth (WorkOS device authorization)
├── prober.py          Background health prober (honest, sequential, gentle)
├── compressor.py      RTK prompt compression
├── cli.py             start/stop/status/update process manager
├── static/dashboard.html   zero-build dashboard (single file)
└── providers/         adapter per provider, BaseProvider base (SSE helpers)
tests/                 pytest suite (port guard, import policy, routing, prober…)
```

## Request flow

1. `/v1/chat/completions` → parse `"model"` → split on first `/` → provider prefix (aliases resolved).
2. `ProviderRouter` picks the adapter; unknown model → passthrough to the provider verbatim.
3. Adapter maps the OpenAI payload upstream, streams Connect-RPC/SSE back, normalized to OpenAI SSE chunks.
4. Each request is logged (model, provider, tokens, latency, status) to `GET /api/recent-requests`.

## Anti-ban posture

- Session/machine identity derived once per token and reused (no rotation = no fingerprint noise).
- No third-party SDK header leakage.
- 429/403 fail-over immediately; never retried in a burst.

## Concurrency model

Async (httpx/asyncio) per request, single uvicorn process on 127.0.0.1:3001. OmniRoute (port 20128) is a separate process this project never touches except read-only SQLite.

An optional background health prober task lives in the same event loop (see [[API Reference]] and [[Configuration]]). It is **off by default**, probes providers strictly one after another with a pause between them, and never retries in a burst — the same anti-ban posture as request routing.

## Credential lifecycle

Adapters own their own refresh, writing rotated tokens back through `persist_provider_tokens()`:

1. Resolve credentials from config, then from the freshest local app session (`token_fetcher.py`).
2. Refresh **before** expiry where the upstream publishes one (Cline, Antigravity, Codex), so an expired token never costs a request.
3. On an auth failure, refresh once and retry once; if the upstream still refuses, surface the real reason.

Cline is the worked example: a stale refresh token copied from another router's database fails with `400 invalid_grant` and looks identical to a revoked account at call time, so the adapter harvests the live local Cline session and retries — and reports `reauth_required` honestly when the account itself is no longer linked upstream.

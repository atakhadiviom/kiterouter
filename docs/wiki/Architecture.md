# Architecture

## Layout

```
src/kiterouter/
├── server.py          FastAPI app — all /v1/* and /api/* endpoints
├── router.py          ProviderRouter — prefix dispatch, model aggregation
├── config.py          KiteConfig — persistence, port-3001 hard guard
├── token_fetcher.py   Local-app + OmniRoute/9Router credential import (skip-policy)
├── crypto_helper.py   OmniRoute enc:v1: AES-GCM local decryption
├── static/dashboard.html   zero-build dashboard (single file)
└── providers/         adapter per provider, BaseProvider base (SSE helpers)
tests/                 pytest suite (port guard, import policy, routing…)
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

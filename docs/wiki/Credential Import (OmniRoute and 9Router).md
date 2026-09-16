# Credential Import (OmniRoute and 9Router)

**Policy: only import what actually works.**

## Sources (read-only, never locked)

- OmniRoute: `~/.omniroute/storage.sqlite` (read-only URI mode) — table `provider_connections`
- 9Router: `~/.9router/db/data.sqlite` (read-only) — table `providerConnections`

## OmniRoute `enc:v1:` decryption

OmniRoute encrypts `api_key` / `access_token` / `refresh_token` fields as `enc:v1:<iv_hex>:<ct_hex>:<tag_hex>` (AES-256-GCM). KiteRouter decrypts locally (in `src/kiterouter/crypto_helper.py`) using OmniRoute's own `STORAGE_ENCRYPTION_KEY` from `~/.omniroute/.env`, with Node-compatible scrypt key derivation (static salt `omniroute-field-encryption-v1`, N=16384, r=8, p=1). Plaintext never leaves the machine and is written only to KiteRouter's local config.

## What gets imported

- Every secret must decrypt to usable plaintext and be structurally valid (string, non-empty, no `enc:` residue) — or the **whole provider row is skipped** with a reason: `encrypted-import-unsupported` / `invalid-type`.
- Existing working credentials are **not clobbered** by a broken import.
- Sync response carries `imported_providers` + `skipped {provider: reason}`; a sync with zero usable imports reports "no usable credentials imported".

## Port safety

Sync loops explicitly skip `port`, `host`, `enable_rtk`, `max_tool_chars`, and `KiteConfig.save()` re-forces port 3001 — regression-tested (`test_sync_source_does_not_alter_port`, `test_config_save_guards_port`).

## Model fetching

`/api/fetch-models` pulls live catalogs per provider. Fetched models are **listed, not verified** — use `/api/test-all-models` for real pass/fail before trusting them.

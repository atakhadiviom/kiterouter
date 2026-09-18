"""KiteRouter FastAPI server: dual protocol (OpenAI + Anthropic) and RTK compression."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from kiterouter import catalog, cline_auth, updater
from kiterouter.compressor import compress_messages
from kiterouter.config import KiteConfig
from kiterouter.live_health import LiveHealth
from kiterouter.prober import HealthProber, connection_id, probe_stream
from kiterouter.providers.node import NodeProvider
from kiterouter.providers.translate import extract_delta_text
from kiterouter.quota import exhausted_until, quota_from_response
from kiterouter.router import ProviderRouter
from kiterouter.store import Store
from kiterouter.token_fetcher import TokenFetcher, normalize_expires_at
from kiterouter.usage import UsageCollector

logger = logging.getLogger("kiterouter.server")


async def _store_maintenance_loop() -> None:
    """Prune, checkpoint and (weekly) vacuum on a schedule.

    Runs independently of the prober: history must stay bounded whether or not
    anyone enabled background probing. Sleeps first so startup is not delayed.
    """
    while True:
        await asyncio.sleep(max(60, int(config.store_maintenance_seconds)))
        try:
            result = await asyncio.to_thread(
                store.maintain,
                config.retention_days(),
                7,
                None,
                config.retention_bodies_days,
            )
            if result.get("pruned") or result.get("vacuumed"):
                logger.info("Store maintenance: %s", result)
        except Exception as e:
            logger.debug("Store maintenance failed: %s", e)


async def _catalog_refresh_loop() -> None:
    """Refresh model catalogs on a schedule — provider lists change constantly.

    Sleeps first so startup is never delayed by network work.
    """
    while True:
        await asyncio.sleep(max(900, int(config.catalog_refresh_hours) * 3600))
        try:
            result = await catalog.sync_all(router, store)
            logger.info(
                "Catalog refresh: %s/%s providers", result.get("refreshed"), result.get("providers")
            )
            store.expire_models(config.catalog_unseen_days)
        except Exception as e:
            logger.debug("Catalog refresh failed: %s", e)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if config.enable_prober:
        prober.interval_seconds = config.prober_interval_seconds
        prober.delay_seconds = config.prober_delay_seconds
        prober.start()
    maintenance = asyncio.create_task(_store_maintenance_loop())
    catalogs = asyncio.create_task(_catalog_refresh_loop())
    try:
        yield
    finally:
        maintenance.cancel()
        catalogs.cancel()
        for task in (maintenance, catalogs):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await prober.stop()
        live_health.flush(force=True)
        await asyncio.to_thread(store.close)


app = FastAPI(title="KiteRouter", version="0.1.0", lifespan=lifespan)

config = KiteConfig.load()
STATIC_DIR = Path(__file__).parent / "static"
CONFIG_DIR = Path.home() / ".kiterouter"
REQUEST_LOG_FILE = CONFIG_DIR / "request_log.json"
LIVE_HEALTH_FILE = CONFIG_DIR / "live_health.json"
STORE_FILE = CONFIG_DIR / "kiterouter.db"
BODY_DIR = CONFIG_DIR / "bodies"
_recent_requests: List[Dict[str, Any]] = []
live_health = LiveHealth(LIVE_HEALTH_FILE)
store = Store(STORE_FILE)


def build_router() -> ProviderRouter:
    """Construct a router with the discovered catalog wired in.

    Every rebuild goes through here: a router without its catalog would hide
    freshly synced models until the next restart.
    """
    instance = ProviderRouter(config=config)
    instance.set_catalog(store.all_models)
    instance.quota_lookup = lambda name: store.quota_for(name)
    return instance


router = build_router()


def _config_size_bytes() -> int:
    try:
        from kiterouter.config import CONFIG_FILE

        return CONFIG_FILE.stat().st_size if CONFIG_FILE.exists() else 0
    except Exception:
        return 0


def _test_results_count() -> int:
    """How many per-model results config is still carrying.

    Surfaced so the bound is visible: config is rewritten wholesale on every
    save, so this number is what makes saves slow when it grows.
    """
    total = 0
    for conf in (config.providers or {}).values():
        if isinstance(conf, dict) and isinstance(conf.get("test_results"), dict):
            total += len(conf["test_results"])
    return total


def _load_request_logs() -> None:
    global _recent_requests
    if REQUEST_LOG_FILE.exists():
        try:
            with open(REQUEST_LOG_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    _recent_requests = data[:100]
        except Exception:
            _recent_requests = []


def derive_session_key(messages: List[Dict[str, Any]]) -> Optional[str]:
    """A stable key for a conversation, derived from its opening turn.

    A heuristic: the first user message identifies the session, so follow-up
    turns of the same conversation group together. Conversation tracking (Phase
    C) refines this; recording it now means the history is already grouped when
    that lands.
    """
    for message in messages or []:
        if isinstance(message, dict) and message.get("role") == "user":
            text = str(message.get("content") or "").strip()
            if text:
                return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return None


def derive_api_key_id(request: Optional[Request] = None) -> Optional[str]:
    """Attribute spend without gating anyone.

    Short hash of the caller's presented credential when there is one, else
    None ("unattributed"). No auth enforcement — that belongs to the API-keys
    tranche, not this pass.
    """
    credential: Optional[str] = None
    if request is not None:
        try:
            credential = request.headers.get("authorization") or request.headers.get("x-api-key")
        except Exception:
            credential = None
    if not credential or not str(credential).strip():
        return None
    return hashlib.sha256(str(credential).strip().encode("utf-8")).hexdigest()[:12]


def write_request_body(body: Optional[Dict[str, Any]]) -> Optional[str]:
    """Persist a request/response body as a capped file.

    Bodies are the disk-heavy part of history, so they live as files with their
    own shorter retention rather than as database rows — the layout OmniRoute
    uses, and the reason it can keep 90 days of logs without the database
    exploding.
    """
    if not body or not config.save_bodies:
        return None
    try:
        BODY_DIR.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(body, default=str)
        if len(payload) > int(config.max_body_bytes):
            payload = payload[: int(config.max_body_bytes)] + '"…truncated by KiteRouter"}'
        name = f"{int(time.time())}-{uuid.uuid4().hex[:8]}.json"
        day_dir = BODY_DIR / time.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / name
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)
        return str(path)
    except Exception as e:
        logger.debug("Could not write request body: %s", e)
        return None


def record_request_log(
    model: str,
    provider: str,
    tokens_in: int,
    tokens_out: int,
    status: str,
    latency_ms: int,
    error: Optional[str] = None,
    prompt_preview: Optional[str] = None,
    response_preview: Optional[str] = None,
    tokens_saved: int = 0,
    ttft_ms: Optional[int] = None,
    connection: Optional[str] = None,
    session_key: Optional[str] = None,
    combo: Optional[str] = None,
    body: Optional[Dict[str, Any]] = None,
    tokens_cache_read: int = 0,
    tokens_cache_write: int = 0,
    tokens_reasoning: int = 0,
    tokens_is_estimate: int = 0,
    cost_usd: Optional[float] = None,
    cost_is_estimate: int = 0,
    api_key_id: Optional[str] = None,
) -> Dict[str, Any]:
    global _recent_requests
    entry = {
        "id": f"req_{uuid.uuid4().hex[:8]}",
        "timestamp": time.time(),
        "model": model,
        "provider": provider,
        "tokens_in": max(1, int(tokens_in)),
        "tokens_out": max(0, int(tokens_out)),
        "status": status,
        "latency_ms": int(latency_ms),
        "ttft_ms": int(ttft_ms) if ttft_ms is not None else None,
        "error": error,
        "prompt_preview": (prompt_preview or "")[:400],
        "response_preview": (response_preview or "")[:400],
        "tokens_saved": max(0, int(tokens_saved)),
    }
    _recent_requests.insert(0, entry)
    _recent_requests = _recent_requests[:100]
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(REQUEST_LOG_FILE, "w") as f:
            json.dump(_recent_requests, f)
    except Exception as e:
        logger.debug(f"Could not write request log: {e}")

    # Durable history. The ring buffer above still feeds the topology, which only
    # ever looks at the newest few entries.
    try:
        row_id = store.record_request(
            provider=provider,
            model=model,
            status=status,
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
            connection=connection,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tokens_cache_read=tokens_cache_read,
            tokens_cache_write=tokens_cache_write,
            tokens_reasoning=tokens_reasoning,
            tokens_is_estimate=tokens_is_estimate,
            rtk_saved=tokens_saved,
            combo=combo,
            session_key=session_key,
            api_key_id=api_key_id,
            error=error,
            prompt_preview=prompt_preview,
            response_preview=response_preview,
            body_path=write_request_body(body),
            cost_usd=cost_usd,
            cost_is_estimate=cost_is_estimate,
        )
        entry["store_id"] = row_id
    except Exception as e:
        logger.debug("Could not persist request: %s", e)

    # Passive health: every real request is evidence, so the topology reflects
    # reality between tests rather than a frozen snapshot.
    live_health.record(provider, status, model=model, latency_ms=latency_ms, error=error)

    # Quota observation: the response text is already in hand here, so every
    # provider gets header-free exhaustion detection uniformly with no adapter
    # edit. Headers are not visible at this layer, so header-derived quota only
    # applies where a caller passes them (currently none) — error-text
    # exhaustion is what this funnel can actually see.
    try:
        snapshot = quota_from_response(provider, headers=None, body_text=error if status == "error" else response_preview)
        if snapshot is not None:
            store.record_quota(
                provider=provider,
                connection=connection,
                remaining_pct=snapshot.get("remaining_pct"),
                is_exhausted=bool(snapshot.get("is_exhausted")),
                resets_at=snapshot.get("resets_at"),
                source=snapshot.get("source"),
            )
    except Exception as e:
        logger.debug("Could not record quota: %s", e)
    return entry


_load_request_logs()



def is_secret_key(key: str) -> bool:
    """Return True if the key name implies sensitive/credential content."""
    k = key.lower()
    return any(
        s in k
        for s in (
            "token",
            "secret",
            "password",
            "api_key",
            "access_token",
            "refresh_token",
            "bearer",
            "auth",
        )
    ) or k.endswith("_key") or k == "key"


def mask_secret_value(raw: Any) -> Any:
    if not isinstance(raw, str) or not raw:
        return raw
    return f"...{raw[-4:]}" if len(raw) > 4 else "******"


def is_masked_placeholder(val: Any) -> bool:
    if not isinstance(val, str):
        return False
    stripped = val.strip()
    if not stripped:
        return False
    return (
        stripped.startswith("...")
        or stripped.startswith("•••")
        or stripped.startswith("***")
        or set(stripped) <= {"*", "•", "."}
    )


def redact_secrets_recursive(obj: Any) -> Any:
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if is_secret_key(k) and isinstance(v, str) and v:
                result[k] = mask_secret_value(v)
            elif isinstance(v, (dict, list)):
                result[k] = redact_secrets_recursive(v)
            else:
                result[k] = v
        return result
    elif isinstance(obj, list):
        return [redact_secrets_recursive(item) for item in obj]
    return obj


def update_dict_preserving_placeholders(target: Dict[str, Any], updates: Dict[str, Any]) -> None:
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(target.get(k), dict):
            update_dict_preserving_placeholders(target[k], v)
        elif is_masked_placeholder(v):
            continue
        else:
            target[k] = v


class ChatMessage(BaseModel):
    role: str
    content: Any


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[Dict[str, Any]]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None
    stream: Optional[bool] = True


class AnthropicMessageRequest(BaseModel):
    model: str
    messages: List[Dict[str, Any]]
    system: Optional[str] = None
    max_tokens: Optional[int] = 4096
    temperature: Optional[float] = 0.7
    stream: Optional[bool] = True


@app.get("/health")
async def health_check():
    available_providers = await router.get_available_providers()
    return {
        "status": "healthy",
        "service": "kiterouter",
        "rtk_enabled": config.enable_rtk,
        "available_providers": available_providers,
        "metrics": router.metrics,
        "provider_health": live_health.snapshot(),
        "prober": {
            "enabled": prober.enabled,
            "running": prober.running,
            "interval_seconds": prober.interval_seconds,
            "last_run": prober.last_run,
        },
    }


@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    html_path = STATIC_DIR / "dashboard.html"
    if html_path.exists():
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>KiteRouter is running</h1>"


@app.get("/v1/models")
@app.get("/api/v1/models")
async def list_models():
    return {"object": "list", "data": router.get_all_models()}


@app.get("/api/config")
async def get_config():
    """Return provider configurations with recursively masked secret keys and model combos."""
    masked_providers = redact_secrets_recursive(config.providers)
    available = await router.get_available_providers()
    return {
        "port": config.port,
        "enable_rtk": config.enable_rtk,
        "enable_prober": config.enable_prober,
        "prober_interval_seconds": config.prober_interval_seconds,
        "providers": masked_providers,
        "combos": config.combos,
        "available_providers": available,
    }


class UpdateConfigRequest(BaseModel):
    providers: Dict[str, Any]
    enable_rtk: Optional[bool] = None


@app.post("/api/config")
async def update_config(req: UpdateConfigRequest):
    """Update and persist provider configuration."""
    global router
    for p_name, p_updates in req.providers.items():
        if p_name not in config.providers:
            config.providers[p_name] = {}
        if isinstance(p_updates, dict):
            update_dict_preserving_placeholders(config.providers[p_name], p_updates)

    if req.enable_rtk is not None:
        config.enable_rtk = req.enable_rtk

    config.save()
    # Re-initialize router with updated config
    router = build_router()
    available = await router.get_available_providers()
    return {"status": "saved", "available_providers": available}


class TestProviderRequest(BaseModel):
    provider: str
    model: Optional[str] = None


class SyncSourceRequest(BaseModel):
    source: str  # "9router" or "omniroute"


@app.post("/api/sync-source")
async def sync_source_endpoint(req: SyncSourceRequest):
    """Explicitly sync all credentials and active connections from 9Router or OmniRoute."""
    from kiterouter.token_fetcher import TokenFetcher

    if req.source == "9router":
        creds_map = TokenFetcher.fetch_from_9router()
        skipped: dict = getattr(creds_map, "skipped", {}) or {}
    elif req.source == "omniroute":
        creds_map = TokenFetcher.fetch_from_omniroute()
        skipped = getattr(creds_map, "skipped", {}) or {}
    else:
        return JSONResponse({"status": "error", "message": f"Unsupported source: {req.source}"}, status_code=400)

    if not creds_map:
        msg = f"No active credentials found in {req.source}"
        if skipped:
            msg += f" — skipped {len(skipped)} unusable (cannot decrypt / wrong type)"
        return JSONResponse({
            "status": "warning",
            "message": msg,
            "imported_count": 0,
            "skipped": skipped,
        })

    imported = []
    for p_name, creds in creds_map.items():
        if p_name in ("port", "host", "enable_rtk", "max_tool_chars"):
            continue
        if p_name not in config.providers:
            config.providers[p_name] = {}
        for k, v in creds.items():
            config.providers[p_name][k] = v
        imported.append(p_name)

    # If syncing from 9Router, also import configured model combos
    imported_combos = []
    if req.source == "9router":
        discovered_combos = TokenFetcher.fetch_combos_from_9router()
        for c_name, c_data in discovered_combos.items():
            config.set_combo(c_name, c_data)
            imported_combos.append(c_name)

    # Invariant: KiteRouter port must NEVER be altered by sync
    config.port = 3001
    config.save()
    global router
    router = build_router()
    return {
        "status": "success",
        "source": req.source,
        "imported_count": len(imported),
        "imported_providers": imported,
        "imported_combos": imported_combos,
        "skipped": skipped,
        "skipped_count": len(skipped),
    }


class FetchModelsRequest(BaseModel):
    provider: str


class FetchTokenRequest(BaseModel):
    provider: Optional[str] = None  # None means all discoverable


@app.post("/api/fetch-token")
async def fetch_token_endpoint(req: FetchTokenRequest):
    """Auto-import tokens and credentials from local IDEs, CLIs, and OmniRoute."""
    from kiterouter.token_fetcher import TokenFetcher

    if req.provider:
        creds = TokenFetcher.fetch_for_provider(req.provider)
        if not creds:
            return JSONResponse(
                {"status": "error", "message": f"No local credentials found for {req.provider}"},
                status_code=404,
            )
        # Update config in memory and save
        p_name = req.provider.lower().replace("-", "_")
        if p_name not in config.providers:
            config.providers[p_name] = {}
        for k, v in creds.items():
            config.providers[p_name][k] = v
        config.save()
        global router
        router = build_router()
        return {
            "status": "success",
            "provider": req.provider,
            "source": creds.get("source", "local"),
            "email": creds.get("email"),
            "has_token": bool(creds.get("token")),
            "has_api_key": bool(creds.get("api_key")),
        }
    else:
        # Import all found
        found = TokenFetcher.fetch_all()
        imported = []
        for p_name, creds in found.items():
            if p_name not in config.providers:
                config.providers[p_name] = {}
            for k, v in creds.items():
                config.providers[p_name][k] = v
            imported.append(p_name)
        config.save()
        router = build_router()
        return {
            "status": "success",
            "imported_count": len(imported),
            "imported_providers": imported,
        }


@app.post("/api/fetch-models")
async def fetch_models(req: FetchModelsRequest):
    """Dynamically fetch and refresh available models for a provider."""
    target_provider = router.providers.get(req.provider)
    if not target_provider:
        return JSONResponse(
            {"status": "error", "message": f"Unknown provider {req.provider}"},
            status_code=400,
        )

    try:
        models = await target_provider.fetch_models()
        return {
            "status": "success",
            "provider": req.provider,
            "count": len(models),
            "models": models,
        }
    except Exception as e:
        return {
            "status": "error",
            "provider": req.provider,
            "error": str(e),
            "models": target_provider.get_models(),
        }


@app.post("/api/fetch-all-models")
async def fetch_all_models():
    """Dynamically fetch and refresh available models across all configured providers."""
    results: Dict[str, Any] = {}
    total_models = 0
    for provider_id, provider in router.providers.items():
        try:
            models = await provider.fetch_models()
            results[provider_id] = {
                "status": "success",
                "count": len(models),
                "models": models,
            }
            total_models += len(models)
        except Exception as e:
            fallback = provider.get_models()
            results[provider_id] = {
                "status": "error",
                "error": str(e),
                "count": len(fallback),
                "models": fallback,
            }
            total_models += len(fallback)
    return {
        "status": "success",
        "total_models": total_models,
        "providers": results,
    }


class TestModelRequest(BaseModel):
    model: str
    provider: Optional[str] = None
    prompt: Optional[str] = None


class TestAllModelsRequest(BaseModel):
    max_per_provider: Optional[int] = None


def is_error_content(text: str) -> bool:
    """Detect if output text actually contains an upstream error or notice."""
    t = text.strip().lower()
    if not t:
        return True
    error_markers = [
        "error",
        "http 4",
        "http 5",
        "notice:",
        "unauthorized",
        "forbidden",
        "invalid_argument",
        "rate-limit",
        "outdated version",
        "failover",
        "nodename nor servname",
        "connection refused",
    ]
    return any(marker in t for marker in error_markers)


prober = HealthProber(
    get_router=lambda: router,
    get_config=lambda: config,
    evaluate=is_error_content,
    interval_seconds=config.prober_interval_seconds,
    delay_seconds=config.prober_delay_seconds,
    timeout_seconds=config.prober_timeout_seconds,
    record_health=store.record_health,
)

# Pending interactive Cline re-auth flows, keyed by an opaque flow id so the
# device code itself never leaves the server.
_pending_cline_flows: Dict[str, Dict[str, Any]] = {}


class ProberUpdateRequest(BaseModel):
    enabled: Optional[bool] = None
    interval_seconds: Optional[int] = None
    delay_seconds: Optional[float] = None


@app.get("/api/prober")
async def get_prober():
    """Report background health prober state and the last honest probe results."""
    return prober.status()


@app.post("/api/prober")
async def update_prober(req: ProberUpdateRequest):
    """Enable/disable the health prober or tune its cadence."""
    if req.interval_seconds is not None:
        config.prober_interval_seconds = max(60, int(req.interval_seconds))
        prober.interval_seconds = config.prober_interval_seconds
    if req.delay_seconds is not None:
        config.prober_delay_seconds = max(0.0, float(req.delay_seconds))
        prober.delay_seconds = config.prober_delay_seconds
    if req.enabled is not None:
        config.enable_prober = bool(req.enabled)

    config.save()

    if config.enable_prober:
        prober.start()
    else:
        await prober.stop()

    return prober.status()


@app.post("/api/prober/run")
async def run_prober_now():
    """Run one probe sweep immediately, regardless of the schedule."""
    state = await prober.probe_once()
    return {"status": "completed", **state}


UPDATE_STATUS_TTL_SECONDS = 300
_update_status_cache: Dict[str, Any] = {"at": 0.0, "data": None}


class UpdateRequest(BaseModel):
    restart: bool = True


@app.get("/api/update/status")
async def update_status(fetch: bool = False):
    """How far behind the checkout is.

    A network fetch happens at most once per TTL so a 5-second dashboard poll
    does not hammer the remote; ``fetched_at`` reports the real age.
    """
    now = time.time()
    cached = _update_status_cache["data"]
    if (not fetch and cached is not None
            and now - _update_status_cache["at"] < UPDATE_STATUS_TTL_SECONDS):
        return cached

    data = await asyncio.to_thread(updater.status, fetch, fetch)
    _update_status_cache["at"] = now
    _update_status_cache["data"] = data
    return data


@app.post("/api/update")
async def run_update(req: UpdateRequest):
    """Pull the latest commit, sync dependencies and reload in place."""
    result = await asyncio.to_thread(updater.run_update, req.restart)
    if result.get("status") == "updated":
        _update_status_cache["at"] = 0.0
        _update_status_cache["data"] = None
    return result


@app.get("/api/store")
async def store_stats():
    """Database size, WAL size and maintenance state.

    The WAL is reported because an unmanaged one is exactly how the neighbouring
    OmniRoute installation reached 185 MB.
    """
    stats = await asyncio.to_thread(store.stats)
    return {
        **stats,
        "path": str(store.path),
        "retention_days": config.retention_days(),
        "maintenance_seconds": config.store_maintenance_seconds,
    }


@app.post("/api/store/maintain")
async def store_maintain():
    """Run prune + checkpoint (+ vacuum when due, + body expiry) now."""
    result = await asyncio.to_thread(
        store.maintain,
        config.retention_days(),
        7,
        None,
        config.retention_bodies_days,
    )
    return {"status": "completed", **result, **(await asyncio.to_thread(store.stats))}


@app.get("/api/usage")
async def usage_summary(days: int = 7, group_by: str = "provider", limit: int = 50):
    """Request and token totals over a window, grouped by provider/model/combo.

    Only recorded values — rows produced from a chars÷4 heuristic carry
    tokens_is_estimate=1 rather than silent real numbers.
    """
    since = int(time.time()) - max(1, days) * 86400
    rows = await asyncio.to_thread(store.usage_summary, since, group_by, limit)
    return {
        "status": "success",
        "days": days,
        "group_by": group_by,
        "since": since,
        "as_of": int(time.time()),
        "groups": rows,
        "daily": await asyncio.to_thread(store.usage_daily, since, days),
        "totals": await asyncio.to_thread(store.request_stats, since),
    }


@app.get("/api/tokens")
async def token_totals(days: int = 7):
    """Token accounting exactly as recorded: in, out, cache read/write, reasoning."""
    since = int(time.time()) - max(1, days) * 86400
    return {
        "status": "success",
        "days": days,
        "since": since,
        "as_of": int(time.time()),
        **await asyncio.to_thread(store.token_breakdown, since),
    }


@app.get("/api/provider-stats")
async def provider_stats(days: int = 7):
    """Per-provider volume, success rate and p50/p95 latency and TTFT."""
    since = int(time.time()) - max(1, days) * 86400
    return {
        "status": "success",
        "days": days,
        "since": since,
        "as_of": int(time.time()),
        "providers": await asyncio.to_thread(store.provider_metrics, since),
    }


@app.get("/api/costs")
async def cost_summary(days: int = 7, group_by: str = "provider", limit: int = 50):
    """Spend over a window, grouped by provider/model/key.

    Recorded only where an upstream reported cost; everything else reads as
    unknown, never as zero. Until any provider reports cost this honestly
    reports unknowns rather than figures.
    """
    since = int(time.time()) - max(1, days) * 86400
    groups = await asyncio.to_thread(store.cost_summary, since, group_by, limit)
    unknown = sum(int(g.get("unknown_cost_requests") or 0) for g in groups)
    billed = sum(float(g.get("cost_billed_usd") or 0) for g in groups)
    estimated = sum(float(g.get("cost_estimated_usd") or 0) for g in groups)
    return {
        "status": "success",
        "days": days,
        "group_by": group_by,
        "since": since,
        "as_of": int(time.time()),
        "groups": groups,
        "totals": {
            "cost_billed_usd": round(billed, 6),
            "cost_estimated_usd": round(estimated, 6),
            "unknown_cost_requests": unknown,
        },
    }


@app.get("/api/quota")
async def quota_status():
    """Quota as providers reported it — unknown stays unknown.

    Per provider/connection: latest remaining %, is_exhausted, resets_at, and
    the derived exhausted_until in epoch seconds. Providers that report nothing
    render unknown, never a fabricated 100%.
    """
    snapshots = await asyncio.to_thread(store.latest_quota)
    now = int(time.time())
    providers: Dict[str, Any] = {}
    for snap in snapshots:
        until = exhausted_until(snap)
        providers.setdefault(snap["provider"], []).append(
            {
                "connection": snap.get("connection"),
                "remaining_pct": snap.get("remaining_pct"),
                "is_exhausted": bool(snap.get("is_exhausted")),
                "resets_at": snap.get("resets_at"),
                "exhausted_until": until,
                "source": snap.get("source"),
                "at": snap.get("at"),
            }
        )
    return {
        "status": "success",
        "as_of": now,
        "providers": providers,
        "unknown_providers": sorted(
            name
            for name in config.providers
            if name not in providers
        ),
    }


@app.get("/api/health/history")
async def health_history(limit: int = 100):
    """Recent probes, newest first."""
    return {"status": "success", "checks": await asyncio.to_thread(store.health_history, limit)}


@app.get("/api/health/connections")
async def health_connections(days: int = 7):
    """Per-connection success rate and latency spread over the last N days."""
    since = int(time.time()) - max(1, days) * 86400
    connections = await asyncio.to_thread(store.connection_stats, since)
    latest = {f"{r['provider']}|{r['connection']}": r for r in await asyncio.to_thread(store.latest_health)}
    for row in connections:
        row["latest"] = latest.get(f"{row['provider']}|{row['connection']}")
        row["ok_rate_pct"] = round(
            (row["ok_count"] or 0) * 100.0 / row["checks"], 1
        ) if row["checks"] else 0.0
    return {"status": "success", "days": days, "connections": connections}


class ValidateNodeRequest(BaseModel):
    provider: str
    model: Optional[str] = None


@app.post("/api/providers/node/validate")
async def validate_provider_node(req: ValidateNodeRequest):
    """Test a declarative provider node before trusting it.

    A node is *unverified* until it answers a real completion: a correct-looking
    base_url and path prove nothing. The result is recorded on the node so the
    dashboard can show verified/unverified rather than guessing from the form.
    """
    global router

    conf = config.providers.get(req.provider)
    if not isinstance(conf, dict) or str(conf.get("kind") or "").lower() != "node":
        return JSONResponse(
            {"status": "error", "message": f"{req.provider} is not a provider node"},
            status_code=400,
        )

    node = NodeProvider(req.provider, conf)
    result: Dict[str, Any] = {"status": "success", "provider": req.provider, "describe": node.describe()}

    try:
        result["available"] = await node.is_available()
    except Exception as e:
        result["available"] = False
        result["available_error"] = str(e)

    models: List[str] = []
    try:
        models = await node.fetch_models()
    except Exception as e:
        result["catalog_error"] = str(e)
    result["model_count"] = len(models)
    result["models"] = models[:50]

    probe_model = req.model or (models[0] if models else None)
    if not probe_model:
        result["verified"] = False
        result["message"] = "No model to probe; check the models path or set a model."
        return result

    measurement = await probe_stream(node, probe_model, config.prober_timeout_seconds)
    text = measurement["text"]
    ok = measurement["error"] is None and not is_error_content(text)
    detail = measurement["error"] or (None if ok else (text or "empty response")[:200])

    result["completion"] = {
        "ok": ok,
        "model": probe_model,
        "latency_ms": measurement["latency_ms"],
        "ttft_ms": measurement["ttft_ms"],
        "error": detail,
    }
    result["verified"] = ok
    result["message"] = (
        f"Verified with a real completion ({measurement['latency_ms']}ms)"
        if ok
        else f"Not verified: {detail}"
    )

    # Record it on the node. Only a real completion sets verified_at.
    config.update_provider_tokens(
        req.provider,
        {
            "verified_at": int(time.time()) if ok else None,
            "last_error": None if ok else detail,
            "last_validated_model": probe_model,
        },
    )
    router = build_router()
    return result


@app.get("/api/providers/nodes")
async def list_provider_nodes():
    """Every declarative node, with its derived URLs and verification state."""
    nodes = []
    for name, conf in (config.providers or {}).items():
        if isinstance(conf, dict) and str(conf.get("kind") or "").lower() == "node":
            nodes.append(NodeProvider(name, conf).describe())
    return {"status": "success", "nodes": nodes}


class RefreshCatalogRequest(BaseModel):
    provider: Optional[str] = None


@app.get("/api/models/catalog")
async def model_catalog():
    """Discovered catalogs with freshness, plus the config's own size.

    Freshness is reported because a stale catalog shows up later as a confusing
    400 from a chat endpoint, not as a visibly old list.
    """
    freshness = await asyncio.to_thread(store.catalog_freshness)
    models = await asyncio.to_thread(store.all_models)
    for provider, info in freshness.items():
        if info.get("synced_at"):
            info["synced_ago_seconds"] = max(0, int(time.time()) - int(info["synced_at"]))
    return {
        "status": "success",
        "providers": freshness,
        "models": models,
        "total_models": sum(len(v) for v in models.values()),
        "refresh_hours": config.catalog_refresh_hours,
        "unseen_days": config.catalog_unseen_days,
        "config_kb": round(_config_size_bytes() / 1024, 1),
        "test_results_kept": _test_results_count(),
    }


@app.post("/api/models/refresh")
async def refresh_model_catalog(req: Optional[RefreshCatalogRequest] = None):
    """Refresh one provider's catalog, or all of them."""
    only = [req.provider] if req and req.provider else None
    result = await catalog.sync_all(router, store, only=only)
    if result.get("refreshed"):
        # Pinned models first, then drop observations the providers stopped listing.
        catalog.apply_config_models(store, config)
        result["expired"] = await asyncio.to_thread(
            store.expire_models, config.catalog_unseen_days
        )
    return {"status": "success", **result}


@app.get("/api/logs")
async def list_logs(
    limit: int = 50,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    status: Optional[str] = None,
    days: Optional[int] = None,
    before_id: Optional[int] = None,
):
    """Durable request history, newest first.

    Pages backwards by ``before_id`` rather than OFFSET so rows arriving mid-page
    cannot make the next page skip or repeat entries.
    """
    since = int(time.time()) - days * 86400 if days else None
    rows = await asyncio.to_thread(
        store.recent_requests,
        limit,
        provider,
        model,
        status,
        since,
        before_id,
    )
    return {
        "status": "success",
        "requests": rows,
        "count": len(rows),
        "stats": await asyncio.to_thread(store.request_stats, since),
        "next_before_id": rows[-1]["id"] if len(rows) == max(1, limit) else None,
    }


@app.get("/api/logs/{request_id}")
async def get_log(request_id: int):
    """One request, with its body artifacts when they have not expired."""
    row = await asyncio.to_thread(store.request_by_id, request_id)
    if not row:
        return JSONResponse({"status": "error", "message": "Request not found"}, status_code=404)

    body = None
    path = row.get("body_path")
    if path:
        try:
            with open(path, "r", encoding="utf-8") as f:
                body = json.load(f)
        except FileNotFoundError:
            body = None  # expired: the row outlives the body by design
        except Exception as e:
            body = {"error": f"could not read body: {e}"}

    return {"status": "success", "request": row, "body": body, "body_expired": bool(path) and body is None}


@app.get("/api/cline/auth/status")
async def cline_auth_status():
    """Honest, secret-free view of the Cline credential state."""
    provider = router.providers.get("cline")
    if not provider or not hasattr(provider, "auth_status"):
        return JSONResponse({"status": "error", "message": "Cline provider unavailable"}, status_code=400)

    status = await provider.auth_status()
    discovered = TokenFetcher.fetch_cline_credentials()
    status["local_session_available"] = bool(discovered)
    status["local_session_source"] = discovered.get("source", "")
    status["device_verification_uri"] = cline_auth.WORKOS_DEVICE_URL.replace(
        "/user_management/authorize/device", ""
    )
    return status


@app.post("/api/cline/auth/start")
async def cline_auth_start():
    """Begin interactive Cline re-authentication (WorkOS device authorization)."""
    try:
        flow = await cline_auth.start_device_flow()
    except cline_auth.DeviceFlowError as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=502)

    flow_id = f"flow_{uuid.uuid4().hex[:12]}"
    _pending_cline_flows[flow_id] = {
        "device_code": flow["device_code"],
        "expires_at": time.time() + flow["expires_in"],
    }
    return {
        "status": "pending",
        "flow_id": flow_id,
        "user_code": flow["user_code"],
        "verification_uri": flow["verification_uri"],
        "verification_uri_complete": flow["verification_uri_complete"],
        "expires_in": flow["expires_in"],
        "interval": flow["interval"],
    }


@app.post("/api/cline/auth/poll")
async def cline_auth_poll(flow_id: str):
    """Poll a pending Cline re-auth flow; persists the session once approved."""
    global router
    flow = _pending_cline_flows.get(flow_id)
    if not flow:
        return JSONResponse(
            {"status": "error", "message": "Unknown or expired re-auth flow"}, status_code=404
        )
    if flow["expires_at"] < time.time():
        _pending_cline_flows.pop(flow_id, None)
        return JSONResponse(
            {"status": "error", "message": "Re-auth flow expired; start again"}, status_code=410
        )

    try:
        session = await cline_auth.complete_device_flow(flow["device_code"])
    except cline_auth.AuthorizationPending as e:
        return {"status": "pending", "interval": e.interval}
    except cline_auth.DeviceFlowError as e:
        _pending_cline_flows.pop(flow_id, None)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)

    _pending_cline_flows.pop(flow_id, None)

    expires_at = normalize_expires_at(session.get("expires_at"))
    config.update_provider_tokens(
        "cline",
        {
            "access_token": session["access_token"],
            "token": session["access_token"],
            "refresh_token": session["refresh_token"],
            "expires_at": expires_at,
            "email": session.get("email", ""),
            "source": "device-flow",
        },
    )
    router = build_router()

    refreshed = router.providers.get("cline")
    if refreshed is not None and session.get("accounts") is None:
        return {
            "status": "warning",
            "message": (
                "Cline login succeeded but the account has no linked Cline "
                "workspace, so upstream calls will still be rejected."
            ),
            "email": session.get("email", ""),
        }

    return {
        "status": "ok",
        "email": session.get("email", ""),
        "expires_at": expires_at,
    }


@app.post("/api/test-provider")
async def test_provider(req: TestProviderRequest):
    """Test a provider with a fast greeting completion and honest error detection."""
    target_provider = router.providers.get(req.provider)
    if not target_provider:
        return JSONResponse({"status": "error", "message": f"Unknown provider {req.provider}"}, status_code=400)

    test_model = req.model or (target_provider.supported_models[0] if target_provider.supported_models else "default")
    start = time.time()
    try:
        res = await target_provider.chat_complete(
            model=test_model,
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=60,
        )
        latency = round((time.time() - start) * 1000)
        content = res.get("choices", [{}])[0].get("message", {}).get("content", "")
        if is_error_content(content):
            return {
                "status": "failed",
                "latency_ms": latency,
                "error": content[:200],
            }
        return {
            "status": "success",
            "latency_ms": latency,
            "response": content[:150] or "[Empty response received]",
        }
    except Exception as e:
        latency = round((time.time() - start) * 1000)
        return {
            "status": "failed",
            "latency_ms": latency,
            "error": str(e),
        }


@app.post("/api/test-model")
async def test_model_endpoint(req: TestModelRequest):
    """Test a specific model for a provider and persist honest test status."""
    prov_name = req.provider
    mod_name = req.model
    if not prov_name:
        if "/" in mod_name:
            prov_name, mod_name = mod_name.split("/", 1)
        else:
            for p_key, p_inst in router.providers.items():
                if mod_name in p_inst.supported_models:
                    prov_name = p_key
                    break
    if not prov_name:
        prov_name = "antigravity" if "gemini" in mod_name else "custom"

    target_provider = router.providers.get(prov_name)
    if not target_provider:
        return JSONResponse(
            {"status": "error", "message": f"Unknown provider {prov_name}"},
            status_code=400,
        )

    test_prompt = req.prompt or "Hi"
    start = time.time()
    status = "error"
    error_msg = None
    response_text = ""
    try:
        res = await target_provider.chat_complete(
            model=mod_name,
            messages=[{"role": "user", "content": test_prompt}],
            max_tokens=256,
        )
        latency = round((time.time() - start) * 1000)
        content = res.get("choices", [{}])[0].get("message", {}).get("content", "")
        response_text = content[:200]
        if is_error_content(content):
            status = "error"
            error_msg = content[:200]
        else:
            status = "ok"
    except Exception as e:
        latency = round((time.time() - start) * 1000)
        status = "error"
        error_msg = str(e)

    # Persist in config
    if prov_name not in config.providers:
        config.providers[prov_name] = {}
    p_conf = config.providers[prov_name]
    if "test_results" not in p_conf or not isinstance(p_conf["test_results"], dict):
        p_conf["test_results"] = {}

    p_conf["test_results"][mod_name] = {
        "status": status,
        "latency_ms": latency,
        "response": response_text,
        "error": error_msg,
        "tested_at": int(time.time()),
    }

    ok_count = sum(1 for v in p_conf["test_results"].values() if v.get("status") == "ok")
    p_conf["last_test_status"] = "ok" if ok_count > 0 else "error"
    # Keep config bounded: it is rewritten wholesale on every save.
    config.prune_test_results()
    config.save()

    return {
        "status": status,
        "provider": prov_name,
        "model": mod_name,
        "latency_ms": latency,
        "response": response_text,
        "error": error_msg,
    }


@app.post("/api/test-all-models")
async def test_all_models_endpoint(req: Optional[TestAllModelsRequest] = None):
    """Test every model of every provider and record per-model honest results."""
    max_per = req.max_per_provider if req else None
    results: Dict[str, Any] = {}
    total_tested = 0

    for p_name, provider in router.providers.items():
        models = provider.get_models()
        if max_per and max_per > 0:
            models = models[:max_per]

        if p_name not in config.providers:
            config.providers[p_name] = {}
        p_conf = config.providers[p_name]
        if "test_results" not in p_conf or not isinstance(p_conf["test_results"], dict):
            p_conf["test_results"] = {}

        provider_summary = {"ok": 0, "error": 0, "models": {}}
        for m in models:
            start = time.time()
            status = "error"
            error_msg = None
            response_text = ""
            try:
                res = await provider.chat_complete(
                    model=m,
                    messages=[{"role": "user", "content": "Hi"}],
                    max_tokens=60,
                )
                latency = round((time.time() - start) * 1000)
                content = res.get("choices", [{}])[0].get("message", {}).get("content", "")
                response_text = content[:200]
                if is_error_content(content):
                    status = "error"
                    error_msg = content[:200]
                else:
                    status = "ok"
            except Exception as e:
                latency = round((time.time() - start) * 1000)
                status = "error"
                error_msg = str(e)

            test_item = {
                "status": status,
                "latency_ms": latency,
                "response": response_text,
                "error": error_msg,
                "tested_at": int(time.time()),
            }
            p_conf["test_results"][m] = test_item
            provider_summary["models"][m] = test_item
            if status == "ok":
                provider_summary["ok"] += 1
            else:
                provider_summary["error"] += 1
            total_tested += 1

        ok_count = sum(1 for v in p_conf["test_results"].values() if v.get("status") == "ok")
        p_conf["last_test_status"] = "ok" if ok_count > 0 else "error"
        results[p_name] = provider_summary

    # A full sweep can add hundreds of entries; trim before writing them out.
    config.prune_test_results()
    config.save()
    return {
        "status": "completed",
        "tested_count": total_tested,
        "results": results,
    }


class ComboCreateRequest(BaseModel):
    name: str
    models: List[str]
    strategy: Optional[str] = "fallback"  # "fallback", "round-robin", "random"
    description: Optional[str] = ""


class ComboUpdateRequest(BaseModel):
    models: Optional[List[str]] = None
    strategy: Optional[str] = None
    description: Optional[str] = None


class ComboTestRequest(BaseModel):
    name: str
    prompt: Optional[str] = "ping"


@app.get("/api/combos")
async def list_combos():
    """Return all defined combos with their strategies and models."""
    combos_list = []
    for name, data in config.combos.items():
        if isinstance(data, dict):
            combos_list.append({
                "name": name,
                "strategy": data.get("strategy", "fallback"),
                "models": data.get("models", []),
                "description": data.get("description", ""),
                "source": data.get("source", "custom"),
            })
    return {"status": "success", "combos": combos_list}


@app.post("/api/combos")
async def create_combo(req: ComboCreateRequest):
    """Create a new model combo."""
    name = req.name.strip()
    if not name:
        return JSONResponse({"status": "error", "message": "Combo name is required"}, status_code=400)
    if not req.models:
        return JSONResponse({"status": "error", "message": "At least one model must be specified"}, status_code=400)

    clean_strategy = req.strategy.lower() if req.strategy else "fallback"
    if clean_strategy not in ("fallback", "round-robin", "random"):
        clean_strategy = "fallback"

    combo_data = {
        "name": name,
        "strategy": clean_strategy,
        "models": req.models,
        "description": req.description or "",
        "source": "custom",
    }
    config.set_combo(name, combo_data)
    router.set_combos(config.combos)
    return {"status": "success", "combo": combo_data}


@app.put("/api/combos/{name}")
async def update_combo(name: str, req: ComboUpdateRequest):
    """Update an existing model combo."""
    if name not in config.combos:
        return JSONResponse({"status": "error", "message": f"Combo '{name}' not found"}, status_code=404)

    combo = dict(config.combos[name])
    if req.models is not None:
        combo["models"] = req.models
    if req.strategy is not None:
        s = req.strategy.lower()
        if s in ("fallback", "round-robin", "random"):
            combo["strategy"] = s
    if req.description is not None:
        combo["description"] = req.description

    config.set_combo(name, combo)
    router.set_combos(config.combos)
    return {"status": "success", "combo": combo}


@app.delete("/api/combos/{name}")
async def delete_combo(name: str):
    """Delete a model combo."""
    if not config.delete_combo(name):
        return JSONResponse({"status": "error", "message": f"Combo '{name}' not found"}, status_code=404)
    router.set_combos(config.combos)
    return {"status": "success", "message": f"Combo '{name}' deleted"}


@app.post("/api/combos/import-9router")
async def import_combos_9router():
    """Import combos directly from 9Router SQLite database."""
    from kiterouter.token_fetcher import TokenFetcher
    found = TokenFetcher.fetch_combos_from_9router()
    if not found:
        return JSONResponse({
            "status": "warning",
            "message": "No combos found in 9Router",
            "imported_count": 0,
            "combos": [],
        })

    imported_names = []
    for c_name, c_data in found.items():
        config.set_combo(c_name, c_data)
        imported_names.append(c_name)

    router.set_combos(config.combos)
    return {
        "status": "success",
        "imported_count": len(imported_names),
        "imported_combos": imported_names,
    }


@app.post("/api/test-combo")
async def test_combo(req: ComboTestRequest):
    """Test a combo with a simple prompt and return response and latency."""
    combo = router.get_combo(req.name)
    if not combo:
        return JSONResponse({"status": "error", "message": f"Combo '{req.name}' not found"}, status_code=404)

    prompt = req.prompt or "ping"
    messages = [{"role": "user", "content": prompt}]
    start_time = time.time()
    collected = []
    try:
        async for chunk in router.stream_combo(combo, messages, max_tokens=30):
            if chunk.startswith("data: ") and chunk.strip() != "data: [DONE]":
                try:
                    payload = json.loads(chunk[6:].strip())
                    delta = payload.get("choices", [{}])[0].get("delta", {})
                    c = delta.get("content") or delta.get("reasoning_content") or ""
                    if c:
                        collected.append(c)
                except Exception:
                    pass
        latency_ms = round((time.time() - start_time) * 1000)
        resp_text = "".join(collected).strip()
        status = "ok" if (resp_text and not is_error_content(resp_text)) else "error"
        return {
            "status": status,
            "combo": req.name,
            "latency_ms": latency_ms,
            "response": resp_text,
            "error": resp_text if status == "error" else None,
        }
    except Exception as e:
        latency_ms = round((time.time() - start_time) * 1000)
        return {
            "status": "error",
            "combo": req.name,
            "latency_ms": latency_ms,
            "response": "",
            "error": str(e),
        }


@app.get("/api/recent-requests")
async def get_recent_requests(limit: int = 15):
    """Return recent completion requests (newest first)."""
    return {
        "status": "success",
        "requests": _recent_requests[:limit],
    }


@app.delete("/api/recent-requests")
async def clear_recent_requests():
    """Clear all recent request logs."""
    global _recent_requests
    _recent_requests = []
    try:
        if REQUEST_LOG_FILE.exists():
            REQUEST_LOG_FILE.unlink()
    except Exception:
        pass
    return {"status": "success", "message": "Request history cleared"}


@app.get("/api/stats")
async def get_gateway_stats():
    """Return high-level operational telemetry and stats."""
    total = len(_recent_requests)
    ok_count = sum(1 for r in _recent_requests if r.get("status") == "ok")
    err_count = total - ok_count
    rate = round((ok_count / total * 100), 1) if total > 0 else 100.0
    tot_in = sum(r.get("tokens_in", 0) for r in _recent_requests)
    tot_out = sum(r.get("tokens_out", 0) for r in _recent_requests)
    tot_saved = sum(r.get("tokens_saved", 0) for r in _recent_requests)
    avg_latency = round(sum(r.get("latency_ms", 0) for r in _recent_requests) / total) if total > 0 else 0

    return {
        "status": "success",
        "total_requests": total,
        "successful_requests": ok_count,
        "failed_requests": err_count,
        "success_rate_pct": rate,
        "tokens_in": tot_in,
        "tokens_out": tot_out,
        "tokens_saved_rtk": tot_saved + router.metrics.get("saved_tokens_approx", 0),
        "avg_latency_ms": avg_latency,
        "combos_count": len(config.combos),
        "providers_configured": len(config.providers),
        "store": {
            **await asyncio.to_thread(store.request_stats, int(time.time()) - 7 * 86400),
            "days": 7,
            "as_of": int(time.time()),
        },
    }


@app.get("/api/config/backup")
async def backup_config():
    """Export configuration as downloadable backup."""
    data = {
        "host": config.host,
        "port": config.port,
        "enable_rtk": config.enable_rtk,
        "max_tool_chars": config.max_tool_chars,
        "combos": config.combos,
        "providers": config.providers,
    }
    return JSONResponse(
        content=data,
        headers={"Content-Disposition": "attachment; filename=kiterouter-backup.json"},
    )


class ConfigRestoreRequest(BaseModel):
    combos: Optional[Dict[str, Any]] = None
    providers: Optional[Dict[str, Any]] = None
    enable_rtk: Optional[bool] = None


@app.post("/api/config/restore")
async def restore_config(req: ConfigRestoreRequest):
    """Restore combos and provider configurations from a backup."""
    if req.combos is not None:
        for k, v in req.combos.items():
            if isinstance(v, dict):
                config.combos[k] = v
    if req.providers is not None:
        for k, v in req.providers.items():
            if isinstance(v, dict):
                config.providers[k] = v
    if req.enable_rtk is not None:
        config.enable_rtk = req.enable_rtk
    config.save()
    router.set_combos(config.combos)
    return {"status": "success", "message": "Configuration restored successfully"}


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, http_request: Request):
    messages = req.messages
    rtk_saved = 0
    if config.enable_rtk:
        messages, stats = compress_messages(
            messages, max_tool_chars=config.max_tool_chars
        )
        rtk_saved = stats.saved_chars // 4
        router.metrics["saved_tokens_approx"] += rtk_saved

    prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
    prompt_str = str(messages[-1].get("content", "")) if messages else ""
    tokens_in = max(1, prompt_chars // 4)
    session_key = derive_session_key(messages)
    start_time = time.time()
    combo_info = router.get_combo(req.model)
    if combo_info:
        provider_name = f"combo/{combo_info['name']}"
    else:
        explicit_p, _ = router.parse_model_and_provider(req.model)
        provider_name = explicit_p or (
            router.fallback_chain[0] if router.fallback_chain else "unknown"
        )

    if req.stream:
        async def logging_stream():
            full_text = []
            has_error = False
            err_text = None
            usage = UsageCollector()
            first_content_at: Optional[float] = None
            try:
                async for chunk in router.stream_with_fallback(
                    model=req.model,
                    messages=messages,
                    temperature=req.temperature or 0.7,
                    max_tokens=req.max_tokens,
                ):
                    if chunk.startswith("data: ") and chunk.strip() != "data: [DONE]":
                        try:
                            payload = json.loads(chunk[6:].strip())
                            usage.feed(payload)
                            delta = payload.get("choices", [{}])[0].get("delta", {})
                            text = extract_delta_text(delta)
                            if text:
                                # Time to first token, measured where the client
                                # would see it rather than inferred later.
                                if first_content_at is None:
                                    first_content_at = time.time()
                                full_text.append(text)
                        except Exception:
                            pass
                    yield chunk
            except Exception as e:
                has_error = True
                err_text = str(e)
                raise e
            finally:
                resp_text = "".join(full_text)
                tokens_out = max(1, len(resp_text) // 4)
                tokens_in_reported = tokens_in
                tokens_out_reported = tokens_out
                tokens = usage.usage
                tokens_is_estimate = 1
                if tokens is not None:
                    tokens_in_reported = tokens["tokens_in"] or tokens_in
                    tokens_out_reported = tokens["tokens_out"] or tokens_out
                    tokens_is_estimate = 0
                latency_ms = round((time.time() - start_time) * 1000)
                ttft_ms = (
                    round((first_content_at - start_time) * 1000)
                    if first_content_at is not None
                    else None
                )
                if not has_error and is_error_content(resp_text):
                    has_error = True
                    err_text = resp_text[:200]
                explicit_p, _ = router.parse_model_and_provider(req.model)
                record_request_log(
                    model=req.model,
                    provider=provider_name,
                    tokens_in=tokens_in_reported,
                    tokens_out=tokens_out_reported,
                    status="error" if has_error else "ok",
                    latency_ms=latency_ms,
                    ttft_ms=ttft_ms,
                    error=err_text,
                    prompt_preview=prompt_str,
                    response_preview=resp_text,
                    tokens_saved=rtk_saved,
                    session_key=session_key,
                    connection=connection_id(config.providers.get(explicit_p or "", {})),
                    combo=combo_info["name"] if combo_info else None,
                    tokens_cache_read=tokens["tokens_cache_read"] if tokens else 0,
                    tokens_cache_write=tokens["tokens_cache_write"] if tokens else 0,
                    tokens_reasoning=tokens["tokens_reasoning"] if tokens else 0,
                    tokens_is_estimate=tokens_is_estimate,
                    cost_usd=usage.cost_usd,
                    api_key_id=derive_api_key_id(http_request),
                    body={
                        "request": {
                            "model": req.model,
                            "messages": messages,
                            "max_tokens": req.max_tokens,
                            "temperature": req.temperature,
                            "stream": True,
                        },
                        "response": {"text": resp_text[:20000], "status": "error" if has_error else "ok"},
                    },
                )

        return StreamingResponse(logging_stream(), media_type="text/event-stream")
    else:
        # Aggregate chunks into a single JSON response
        full_text = []
        has_error = False
        err_text = None
        usage = UsageCollector()
        first_content_at: Optional[float] = None
        try:
            async for chunk in router.stream_with_fallback(
                model=req.model,
                messages=messages,
                temperature=req.temperature or 0.7,
                max_tokens=req.max_tokens,
            ):
                if chunk.startswith("data: ") and chunk.strip() != "data: [DONE]":
                    try:
                        payload = json.loads(chunk[6:].strip())
                        usage.feed(payload)
                        delta = payload.get("choices", [{}])[0].get("delta", {})
                        # Shared with the streaming path and the prober: models that
                        # answer in reasoning_content would otherwise aggregate to
                        # empty text for non-streaming clients.
                        text = extract_delta_text(delta)
                        if text:
                            if first_content_at is None:
                                first_content_at = time.time()
                            full_text.append(text)
                    except Exception:
                        continue
        except Exception as e:
            has_error = True
            err_text = str(e)

        resp_text = "".join(full_text)
        tokens = usage.usage
        if tokens is not None:
            tokens_in_reported = tokens["tokens_in"] or tokens_in
            tokens_out_reported = tokens["tokens_out"] or max(1, len(resp_text) // 4)
            tokens_is_estimate = 0
        else:
            tokens_in_reported = tokens_in
            tokens_out_reported = max(1, len(resp_text) // 4)
            tokens_is_estimate = 1
        latency_ms = round((time.time() - start_time) * 1000)
        ttft_ms = (
            round((first_content_at - start_time) * 1000)
            if first_content_at is not None
            else None
        )
        if not has_error and is_error_content(resp_text):
            has_error = True
            err_text = resp_text[:200]

        record_request_log(
            model=req.model,
            provider=provider_name,
            tokens_in=tokens_in_reported,
            tokens_out=tokens_out_reported,
            status="error" if has_error else "ok",
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
            error=err_text,
            prompt_preview=prompt_str,
            response_preview=resp_text,
            tokens_saved=rtk_saved,
            session_key=session_key,
            connection=connection_id(config.providers.get(explicit_p or "", {})),
            combo=combo_info["name"] if combo_info else None,
            tokens_cache_read=tokens["tokens_cache_read"] if tokens else 0,
            tokens_cache_write=tokens["tokens_cache_write"] if tokens else 0,
            tokens_reasoning=tokens["tokens_reasoning"] if tokens else 0,
            tokens_is_estimate=tokens_is_estimate,
            cost_usd=usage.cost_usd,
            api_key_id=derive_api_key_id(http_request),
            body={
                "request": {
                    "model": req.model,
                    "messages": messages,
                    "max_tokens": req.max_tokens,
                    "temperature": req.temperature,
                    "stream": False,
                },
                "response": {"text": resp_text[:20000], "status": "error" if has_error else "ok"},
            },
        )

        return {
            "id": f"chatcmpl-{int(time.time() * 1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": resp_text,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": tokens_in_reported,
                "completion_tokens": tokens_out_reported,
                "total_tokens": tokens_in_reported + tokens_out_reported,
            },
        }


@app.post("/v1/messages")
async def anthropic_messages(req: AnthropicMessageRequest, http_request: Request):
    """Native Anthropic Messages API for Claude Code."""
    converted_messages: List[Dict[str, Any]] = []
    if req.system:
        converted_messages.append({"role": "system", "content": req.system})

    for m in req.messages:
        converted_messages.append(m)

    if config.enable_rtk:
        converted_messages, stats = compress_messages(
            converted_messages, max_tool_chars=config.max_tool_chars
        )
        router.metrics["saved_tokens_approx"] += stats.saved_chars // 4

    prompt_chars = sum(len(str(m.get("content", ""))) for m in converted_messages)
    tokens_in = max(1, prompt_chars // 4)
    start_time = time.time()
    explicit_p, _ = router.parse_model_and_provider(req.model)
    provider_name = explicit_p or (
        router.fallback_chain[0] if router.fallback_chain else "unknown"
    )

    async def anthropic_event_stream():
        msg_id = f"msg_{int(time.time() * 1000)}"
        full_text = []
        has_error = False
        err_text = None
        usage = UsageCollector()
        # 1. message_start
        yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [], 'model': req.model, 'stop_reason': None, 'usage': {'input_tokens': tokens_in, 'output_tokens': 0}}})}\n\n"
        # 2. content_block_start
        yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"

        try:
            # 3. Stream content deltas
            async for chunk in router.stream_with_fallback(
                model=req.model,
                messages=converted_messages,
                temperature=req.temperature or 0.7,
                max_tokens=req.max_tokens,
            ):
                if chunk.startswith("data: ") and chunk.strip() != "data: [DONE]":
                    try:
                        payload = json.loads(chunk[6:].strip())
                        usage.feed(payload)
                        delta = payload.get("choices", [{}])[0].get("delta", {})
                        content_text = delta.get("content", "")
                        if content_text:
                            full_text.append(content_text)
                            event_data = {
                                "type": "content_block_delta",
                                "index": 0,
                                "delta": {"type": "text_delta", "text": content_text},
                            }
                            yield f"event: content_block_delta\ndata: {json.dumps(event_data)}\n\n"
                    except Exception:
                        continue
        except Exception as e:
            has_error = True
            err_text = str(e)
            raise e
        finally:
            resp_text = "".join(full_text)
            tokens = usage.usage
            if tokens is not None:
                tokens_in_reported = tokens["tokens_in"] or tokens_in
                tokens_out_reported = tokens["tokens_out"] or max(1, len(resp_text) // 4)
                tokens_is_estimate = 0
            else:
                tokens_in_reported = tokens_in
                tokens_out_reported = max(1, len(resp_text) // 4)
                tokens_is_estimate = 1
            latency_ms = round((time.time() - start_time) * 1000)
            if not has_error and is_error_content(resp_text):
                has_error = True
                err_text = resp_text[:200]
            record_request_log(
                model=req.model,
                provider=provider_name,
                tokens_in=tokens_in_reported,
                tokens_out=tokens_out_reported,
                status="error" if has_error else "ok",
                latency_ms=latency_ms,
                error=err_text,
                connection=connection_id(config.providers.get(explicit_p or "", {})),
                session_key=derive_session_key(converted_messages),
                tokens_cache_read=tokens["tokens_cache_read"] if tokens else 0,
                tokens_cache_write=tokens["tokens_cache_write"] if tokens else 0,
                tokens_reasoning=tokens["tokens_reasoning"] if tokens else 0,
                tokens_is_estimate=tokens_is_estimate,
                cost_usd=usage.cost_usd,
                api_key_id=derive_api_key_id(http_request),
            )

        # 4. content_block_stop & message_delta & message_stop
        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
        yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': tokens_out_reported}})}\n\n"
        yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"

    return StreamingResponse(anthropic_event_stream(), media_type="text/event-stream")


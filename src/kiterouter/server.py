"""KiteRouter FastAPI server: dual protocol (OpenAI + Anthropic) and RTK compression."""
from __future__ import annotations

import asyncio
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

from kiterouter import cline_auth, updater
from kiterouter.compressor import compress_messages
from kiterouter.config import KiteConfig
from kiterouter.live_health import LiveHealth
from kiterouter.prober import HealthProber
from kiterouter.router import ProviderRouter
from kiterouter.store import Store
from kiterouter.token_fetcher import TokenFetcher, normalize_expires_at

logger = logging.getLogger("kiterouter.server")


async def _store_maintenance_loop() -> None:
    """Prune, checkpoint and (weekly) vacuum on a schedule.

    Runs independently of the prober: history must stay bounded whether or not
    anyone enabled background probing. Sleeps first so startup is not delayed.
    """
    while True:
        await asyncio.sleep(max(60, int(config.store_maintenance_seconds)))
        try:
            result = await asyncio.to_thread(store.maintain, config.retention_days(), 7)
            if result.get("pruned") or result.get("vacuumed"):
                logger.info("Store maintenance: %s", result)
        except Exception as e:
            logger.debug("Store maintenance failed: %s", e)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if config.enable_prober:
        prober.interval_seconds = config.prober_interval_seconds
        prober.delay_seconds = config.prober_delay_seconds
        prober.start()
    maintenance = asyncio.create_task(_store_maintenance_loop())
    try:
        yield
    finally:
        maintenance.cancel()
        try:
            await maintenance
        except (asyncio.CancelledError, Exception):
            pass
        await prober.stop()
        live_health.flush(force=True)
        await asyncio.to_thread(store.close)


app = FastAPI(title="KiteRouter", version="0.1.0", lifespan=lifespan)

config = KiteConfig.load()
router = ProviderRouter(config=config)
STATIC_DIR = Path(__file__).parent / "static"
CONFIG_DIR = Path.home() / ".kiterouter"
REQUEST_LOG_FILE = CONFIG_DIR / "request_log.json"
LIVE_HEALTH_FILE = CONFIG_DIR / "live_health.json"
STORE_FILE = CONFIG_DIR / "kiterouter.db"
_recent_requests: List[Dict[str, Any]] = []
live_health = LiveHealth(LIVE_HEALTH_FILE)
store = Store(STORE_FILE)


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

    # Passive health: every real request is evidence, so the topology reflects
    # reality between tests rather than a frozen snapshot.
    live_health.record(provider, status, model=model, latency_ms=latency_ms, error=error)
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
    router = ProviderRouter(config=config)
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
    router = ProviderRouter(config=config)
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
        router = ProviderRouter(config=config)
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
        router = ProviderRouter(config=config)
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
    """Run prune + checkpoint (and vacuum when due) now."""
    result = await asyncio.to_thread(store.maintain, config.retention_days(), 7)
    return {"status": "completed", **result, **(await asyncio.to_thread(store.stats))}


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
    router = ProviderRouter(config=config)

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
async def chat_completions(req: ChatCompletionRequest):
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
                            delta = payload.get("choices", [{}])[0].get("delta", {})
                            c = delta.get("content", "")
                            if c:
                                full_text.append(c)
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
                latency_ms = round((time.time() - start_time) * 1000)
                if not has_error and is_error_content(resp_text):
                    has_error = True
                    err_text = resp_text[:200]
                record_request_log(
                    model=req.model,
                    provider=provider_name,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    status="error" if has_error else "ok",
                    latency_ms=latency_ms,
                    error=err_text,
                    prompt_preview=prompt_str,
                    response_preview=resp_text,
                    tokens_saved=rtk_saved,
                )

        return StreamingResponse(logging_stream(), media_type="text/event-stream")
    else:
        # Aggregate chunks into a single JSON response
        full_text = []
        has_error = False
        err_text = None
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
                        delta = payload.get("choices", [{}])[0].get("delta", {})
                        if "content" in delta and delta["content"]:
                            full_text.append(delta["content"])
                    except Exception:
                        continue
        except Exception as e:
            has_error = True
            err_text = str(e)

        resp_text = "".join(full_text)
        tokens_out = max(1, len(resp_text) // 4)
        latency_ms = round((time.time() - start_time) * 1000)
        if not has_error and is_error_content(resp_text):
            has_error = True
            err_text = resp_text[:200]

        record_request_log(
            model=req.model,
            provider=provider_name,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            status="error" if has_error else "ok",
            latency_ms=latency_ms,
            error=err_text,
            prompt_preview=prompt_str,
            response_preview=resp_text,
            tokens_saved=rtk_saved,
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
                "prompt_tokens": tokens_in,
                "completion_tokens": tokens_out,
                "total_tokens": tokens_in + tokens_out,
            },
        }


@app.post("/v1/messages")
async def anthropic_messages(req: AnthropicMessageRequest):
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
            tokens_out = max(1, len(resp_text) // 4)
            latency_ms = round((time.time() - start_time) * 1000)
            if not has_error and is_error_content(resp_text):
                has_error = True
                err_text = resp_text[:200]
            record_request_log(
                model=req.model,
                provider=provider_name,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                status="error" if has_error else "ok",
                latency_ms=latency_ms,
                error=err_text,
            )

        # 4. content_block_stop & message_delta & message_stop
        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
        yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': tokens_out}})}\n\n"
        yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"

    return StreamingResponse(anthropic_event_stream(), media_type="text/event-stream")


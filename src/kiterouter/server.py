"""KiteRouter FastAPI server: dual protocol (OpenAI + Anthropic) and RTK compression."""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from kiterouter.compressor import compress_messages
from kiterouter.config import KiteConfig
from kiterouter.router import ProviderRouter

logger = logging.getLogger("kiterouter.server")

app = FastAPI(title="KiteRouter", version="0.1.0")

config = KiteConfig.load()
router = ProviderRouter(config=config.providers)
STATIC_DIR = Path(__file__).parent / "static"
CONFIG_DIR = Path.home() / ".kiterouter"
REQUEST_LOG_FILE = CONFIG_DIR / "request_log.json"
_recent_requests: List[Dict[str, Any]] = []


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
) -> Dict[str, Any]:
    global _recent_requests
    entry = {
        "id": f"req_{uuid.uuid4().hex[:8]}",
        "timestamp": time.time(),
        "model": model,
        "provider": provider,
        "tokens_in": max(1, int(tokens_in)),
        "tokens_out": max(1, int(tokens_out)),
        "status": status,
        "latency_ms": int(latency_ms),
        "error": error,
    }
    _recent_requests.insert(0, entry)
    _recent_requests = _recent_requests[:100]
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(REQUEST_LOG_FILE, "w") as f:
            json.dump(_recent_requests, f)
    except Exception as e:
        logger.debug(f"Could not write request log: {e}")
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
    """Return provider configurations with recursively masked secret keys."""
    masked_providers = redact_secrets_recursive(config.providers)
    available = await router.get_available_providers()
    return {
        "port": config.port,
        "enable_rtk": config.enable_rtk,
        "providers": masked_providers,
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
    router = ProviderRouter(config=config.providers)
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
    elif req.source == "omniroute":
        creds_map = TokenFetcher.fetch_from_omniroute()
    else:
        return JSONResponse({"status": "error", "message": f"Unsupported source: {req.source}"}, status_code=400)

    if not creds_map:
        return JSONResponse({"status": "warning", "message": f"No active credentials found in {req.source}", "imported_count": 0})

    imported = []
    for p_name, creds in creds_map.items():
        if p_name in ("port", "host", "enable_rtk", "max_tool_chars"):
            continue
        if p_name not in config.providers:
            config.providers[p_name] = {}
        for k, v in creds.items():
            config.providers[p_name][k] = v
        imported.append(p_name)

    # Invariant: KiteRouter port must NEVER be altered by sync
    config.port = 3001
    config.save()
    global router
    router = ProviderRouter(config=config.providers)
    return {
        "status": "success",
        "source": req.source,
        "imported_count": len(imported),
        "imported_providers": imported,
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
        router = ProviderRouter(config=config.providers)
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
        router = ProviderRouter(config=config.providers)
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
    provider: str
    model: str


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
            max_tokens=10,
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
    target_provider = router.providers.get(req.provider)
    if not target_provider:
        return JSONResponse(
            {"status": "error", "message": f"Unknown provider {req.provider}"},
            status_code=400,
        )

    start = time.time()
    status = "error"
    error_msg = None
    response_text = ""
    try:
        res = await target_provider.chat_complete(
            model=req.model,
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=10,
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
    if req.provider not in config.providers:
        config.providers[req.provider] = {}
    p_conf = config.providers[req.provider]
    if "test_results" not in p_conf or not isinstance(p_conf["test_results"], dict):
        p_conf["test_results"] = {}

    p_conf["test_results"][req.model] = {
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
        "provider": req.provider,
        "model": req.model,
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
                    max_tokens=10,
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


@app.get("/api/recent-requests")
async def get_recent_requests(limit: int = 15):
    """Return recent completion requests (newest first)."""
    return {
        "status": "success",
        "requests": _recent_requests[:limit],
    }


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    messages = req.messages
    if config.enable_rtk:
        messages, stats = compress_messages(
            messages, max_tool_chars=config.max_tool_chars
        )
        router.metrics["saved_tokens_approx"] += stats.saved_chars // 4

    prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
    tokens_in = max(1, prompt_chars // 4)
    start_time = time.time()
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


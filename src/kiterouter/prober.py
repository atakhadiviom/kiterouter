"""Background health prober: periodic, honest, low-impact status checks.

Probes one representative model per provider on a schedule so the dashboard can
show real degraded/healthy state between on-demand tests. Deliberately gentle:
providers are probed sequentially with a pause between them, and nothing is
retried in a burst, matching the gateway's anti-ban posture.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("kiterouter.prober")

# Placeholder model ids that stand in for "no catalog configured"; probing them
# would spend a request on a model that cannot exist upstream.
PLACEHOLDER_MODELS = {"custom/default"}

PROBE_TIMEOUT_SECONDS = 90
BACKOFF_BASE_SECONDS = 60
MAX_BACKOFF_SECONDS = 3600
# Credentials that need a human are backed off much harder than a cooldown:
# retrying a revoked key every minute achieves nothing but noise.
TERMINAL_BACKOFF_SECONDS = 21600

# Errors that no amount of retrying will fix — they need the operator.
TERMINAL_MARKERS = (
    "unauthorized",
    "invalid_grant",
    "invalid api key",
    "invalid_api_key",
    "forbidden",
    "revoked",
    "expired",
    "http 401",
    "http 403",
    "re-authenticate",
    "insufficient",
    "billing",
)


def connection_id(provider_config: Dict[str, Any]) -> str:
    """Stable identity for one credential set.

    KiteRouter holds a single credential set per provider, so a connection is
    identified by a short hash of its credential material. Swapping the
    credential produces a *new* connection — otherwise a replacement key
    inherits the previous key's failures, which is exactly the masking problem
    per-connection health exists to avoid.
    """
    material = "|".join(
        str(provider_config.get(field) or "")
        for field in (
            "api_key",
            "access_token",
            "token",
            "refresh_token",
            "endpoint",
            "base_url",
            "account_id",
        )
    )
    if not material.strip("|"):
        return "default"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:10]


def is_terminal_error(text: Optional[str]) -> bool:
    """True when the failure needs the operator, not another retry."""
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in TERMINAL_MARKERS)


async def probe_stream(
    provider: Any, model: str, timeout_seconds: int = PROBE_TIMEOUT_SECONDS
) -> Dict[str, Any]:
    """One real completion, timed for latency and time-to-first-token.

    Probing through the streaming path is what makes TTFT measurable. Where an
    adapter buffers the whole body (Cursor reads ``resp.content`` before
    yielding), TTFT ends up equal to latency — which is honest, because that is
    what a client actually experiences.
    """
    started = time.time()
    ttft_ms: Optional[int] = None
    parts: List[str] = []
    error: Optional[str] = None

    async def _run() -> None:
        nonlocal ttft_ms
        async for chunk in provider.stream_chat(
            model=model, messages=[{"role": "user", "content": "Hi"}], max_tokens=48
        ):
            if not chunk.startswith("data: ") or chunk.strip() == "data: [DONE]":
                continue
            try:
                payload = json.loads(chunk[6:].strip())
            except Exception:
                continue
            delta = (payload.get("choices") or [{}])[0].get("delta") or {}
            text = delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning")
            if text:
                if ttft_ms is None:
                    ttft_ms = int((time.time() - started) * 1000)
                parts.append(text)

    try:
        await asyncio.wait_for(_run(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        error = f"probe timed out after {timeout_seconds}s"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    return {
        "text": "".join(parts),
        "latency_ms": int((time.time() - started) * 1000),
        "ttft_ms": ttft_ms,
        "error": error,
    }


class HealthProber:
    """Periodically probes providers and records honest completion results."""

    def __init__(
        self,
        get_router: Callable[[], Any],
        get_config: Callable[[], Any],
        evaluate: Callable[[str], bool],
        interval_seconds: int = 900,
        delay_seconds: float = 3.0,
        timeout_seconds: int = PROBE_TIMEOUT_SECONDS,
        record_health: Optional[Callable[..., None]] = None,
    ) -> None:
        self._get_router = get_router
        self._get_config = get_config
        self._evaluate = evaluate
        self._record_health = record_health

        self.interval_seconds = interval_seconds
        self.delay_seconds = delay_seconds
        self.timeout_seconds = timeout_seconds
        self.enabled = False
        self.running = False
        self.last_run: Optional[int] = None
        self.last_duration_ms: Optional[int] = None
        self.next_run: Optional[int] = None
        self.last_error: Optional[str] = None

        # Per-connection failure state, keyed provider|connection.
        self._backoff: Dict[str, Dict[str, Any]] = {}

        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Start the background loop (idempotent)."""
        if self.running:
            return
        self._stop.clear()
        self.running = True
        self.enabled = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Health prober started (interval=%ss)", self.interval_seconds)

    async def stop(self) -> None:
        """Stop the background loop and wait for it to unwind."""
        self.enabled = False
        if not self.running:
            return
        self.running = False
        self._stop.set()
        task, self._task = self._task, None
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("Health prober stopped")

    async def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await self.probe_once()
                except Exception as e:
                    self.last_error = str(e)
                    logger.warning("Health probe run failed: %s", e)
                self.next_run = int(time.time()) + max(60, self.interval_seconds)
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=max(60, self.interval_seconds)
                    )
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise

    # ---------------------------------------------------------------- probing

    def _targets(self) -> List[Dict[str, Any]]:
        """One target per connection, honouring an optional model override."""
        router = self._get_router()
        config = self._get_config()
        overrides = {}
        prober_conf = getattr(config, "prober", None)
        if isinstance(prober_conf, dict) and isinstance(prober_conf.get("models"), dict):
            overrides = prober_conf["models"]

        provider_configs = getattr(config, "providers", None) or {}
        targets: List[Dict[str, Any]] = []

        for name, provider in getattr(router, "providers", {}).items():
            if name in overrides:
                models = list(overrides[name] or [])
            else:
                models = list(provider.get_models())[:1]

            connection = connection_id(provider_configs.get(name) or {})

            probeable = [m for m in models if m not in PLACEHOLDER_MODELS]
            if not probeable:
                targets.append(
                    {
                        "provider": name,
                        "connection": connection,
                        "model": None,
                        "instance": provider,
                        "skip_reason": "no models configured",
                    }
                )
                continue
            for model in probeable:
                targets.append(
                    {
                        "provider": name,
                        "connection": connection,
                        "model": model,
                        "instance": provider,
                    }
                )
        return targets

    def _state_for(self, key: str) -> Dict[str, Any]:
        return self._backoff.setdefault(
            key, {"failures": 0, "next_at": 0.0, "needs_action": False, "last_error": None}
        )

    def _note_failure(self, key: str, error: Optional[str]) -> Dict[str, Any]:
        """Grow the backoff for a connection that just failed."""
        state = self._state_for(key)
        state["failures"] += 1
        state["last_error"] = (error or "")[:200] or None
        terminal = is_terminal_error(error)
        state["needs_action"] = terminal
        if terminal:
            # A revoked key or an unpaid account does not get better on its own,
            # so it starts at the long backoff instead of climbing to it.
            wait = TERMINAL_BACKOFF_SECONDS
        else:
            wait = min(
                MAX_BACKOFF_SECONDS, BACKOFF_BASE_SECONDS * (2 ** (state["failures"] - 1))
            )
        state["next_at"] = time.time() + wait
        return state

    def _note_success(self, key: str) -> None:
        state = self._state_for(key)
        state.update({"failures": 0, "next_at": 0.0, "needs_action": False, "last_error": None})

    def backoff_state(self) -> Dict[str, Any]:
        return {k: dict(v) for k, v in self._backoff.items()}

    async def probe_once(self) -> Dict[str, Any]:
        """Probe every connection sequentially and persist the honest results."""
        started = time.time()
        config = self._get_config()
        summary = {"ok": 0, "error": 0, "unavailable": 0, "skipped": 0, "needs_action": 0}
        results: Dict[str, Any] = {}
        now = time.time()

        for target in self._targets():
            if self._stop.is_set():
                break
            name = target["provider"]
            connection = target["connection"]
            model, provider = target["model"], target["instance"]
            key = f"{name}|{connection}"

            reason = target.get("skip_reason")
            if not reason:
                try:
                    available = await provider.is_available()
                except Exception:
                    available = False
                if not available:
                    reason = "not configured"

            if reason:
                summary["unavailable"] += 1
                results[key] = {
                    "status": "unavailable",
                    "provider": name,
                    "connection": connection,
                    "model": model,
                    "reason": reason,
                    "probed_at": int(time.time()),
                }
                continue

            # A connection that keeps failing is left alone until its backoff
            # expires instead of being retried on every sweep.
            state = self._state_for(key)
            if state["next_at"] and now < state["next_at"]:
                summary["skipped"] += 1
                results[key] = {
                    "status": "backoff",
                    "provider": name,
                    "connection": connection,
                    "model": model,
                    "reason": f"retry in {int(state['next_at'] - now)}s after "
                              f"{state['failures']} failure(s)",
                    "needs_action": state["needs_action"],
                    "error": state["last_error"],
                    "probed_at": int(time.time()),
                }
                if state["needs_action"]:
                    summary["needs_action"] += 1
                continue

            measurement = await probe_stream(provider, model, self.timeout_seconds)
            latency = measurement["latency_ms"]
            ttft = measurement["ttft_ms"]
            content = measurement["text"]

            status = "error"
            error_msg: Optional[str] = measurement["error"]
            if error_msg is None:
                if self._evaluate(content):
                    error_msg = (content or "empty response")[:200]
                else:
                    status = "ok"

            if status == "ok":
                summary["ok"] += 1
                self._note_success(key)
            else:
                summary["error"] += 1
                state = self._note_failure(key, error_msg)
                if state["needs_action"]:
                    summary["needs_action"] += 1

            probed_at = int(time.time())
            self._record(
                config,
                name,
                model,
                {
                    "status": status,
                    "latency_ms": latency,
                    "ttft_ms": ttft,
                    "response": (content or "")[:200],
                    "error": error_msg,
                    "tested_at": probed_at,
                    "source": "prober",
                },
            )
            if self._record_health is not None:
                try:
                    self._record_health(
                        name,
                        connection,
                        status == "ok",
                        model=model,
                        latency_ms=latency,
                        ttft_ms=ttft,
                        error=error_msg,
                    )
                except Exception as e:
                    logger.debug("Could not persist health row: %s", e)

            results[key] = {
                "status": status,
                "provider": name,
                "connection": connection,
                "model": model,
                "latency_ms": latency,
                "ttft_ms": ttft,
                "error": error_msg,
                "needs_action": self._state_for(key)["needs_action"],
                "probed_at": probed_at,
            }
            if self.delay_seconds > 0:
                await asyncio.sleep(self.delay_seconds)

        self.last_run = int(time.time())
        self.last_duration_ms = round((time.time() - started) * 1000)
        self.last_error = None
        state = {
            "last_run": self.last_run,
            "last_duration_ms": self.last_duration_ms,
            "connections": results,
            # Kept for the existing provider cards, which read the old shape.
            "providers": {v["provider"]: v for v in results.values() if "provider" in v},
            "summary": summary,
        }
        try:
            # Probes also write per-model results into config; keep it bounded.
            if hasattr(config, "prune_test_results"):
                config.prune_test_results()
            config.set_prober_state(state)
        except Exception as e:
            logger.debug("Could not persist prober state: %s", e)

        logger.info("Health probe complete: %s", summary)
        return state

    def _record(self, config: Any, provider: str, model: str, entry: Dict[str, Any]) -> None:
        """Mirror the probe result into the provider's normal test store."""
        providers = getattr(config, "providers", None)
        if not isinstance(providers, dict):
            return
        if provider not in providers or not isinstance(providers[provider], dict):
            providers[provider] = {}
        p_conf = providers[provider]
        if not isinstance(p_conf.get("test_results"), dict):
            p_conf["test_results"] = {}
        p_conf["test_results"][model] = entry
        ok_count = sum(
            1 for v in p_conf["test_results"].values() if isinstance(v, dict) and v.get("status") == "ok"
        )
        p_conf["last_test_status"] = "ok" if ok_count > 0 else "error"

    def status(self) -> Dict[str, Any]:
        config = self._get_config()
        state = getattr(config, "prober", None)
        state = state if isinstance(state, dict) else {}
        return {
            "enabled": self.enabled,
            "running": self.running,
            "interval_seconds": self.interval_seconds,
            "delay_seconds": self.delay_seconds,
            "timeout_seconds": self.timeout_seconds,
            "last_run": self.last_run,
            "last_duration_ms": self.last_duration_ms,
            "next_run": self.next_run,
            "last_error": self.last_error,
            "connections": state.get("connections", {}),
            "providers": state.get("providers", {}),
            "summary": state.get("summary", {}),
            "backoff": self.backoff_state(),
            "needs_action": sorted(
                key for key, value in self._backoff.items() if value.get("needs_action")
            ),
        }

"""Background health prober: periodic, honest, low-impact status checks.

Probes one representative model per provider on a schedule so the dashboard can
show real degraded/healthy state between on-demand tests. Deliberately gentle:
providers are probed sequentially with a pause between them, and nothing is
retried in a burst, matching the gateway's anti-ban posture.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("kiterouter.prober")

# Placeholder model ids that stand in for "no catalog configured"; probing them
# would spend a request on a model that cannot exist upstream.
PLACEHOLDER_MODELS = {"custom/default"}


class HealthProber:
    """Periodically probes providers and records honest completion results."""

    def __init__(
        self,
        get_router: Callable[[], Any],
        get_config: Callable[[], Any],
        evaluate: Callable[[str], bool],
        interval_seconds: int = 900,
        delay_seconds: float = 3.0,
    ) -> None:
        self._get_router = get_router
        self._get_config = get_config
        self._evaluate = evaluate

        self.interval_seconds = interval_seconds
        self.delay_seconds = delay_seconds
        self.enabled = False
        self.running = False
        self.last_run: Optional[int] = None
        self.last_duration_ms: Optional[int] = None
        self.next_run: Optional[int] = None
        self.last_error: Optional[str] = None

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
        """Provider/model pairs to probe, honouring an optional config override."""
        router = self._get_router()
        config = self._get_config()
        overrides = {}
        prober_conf = getattr(config, "prober", None)
        if isinstance(prober_conf, dict) and isinstance(prober_conf.get("models"), dict):
            overrides = prober_conf["models"]

        targets: List[Dict[str, Any]] = []
        for name, provider in getattr(router, "providers", {}).items():
            if name in overrides:
                models = list(overrides[name] or [])
            else:
                models = list(provider.get_models())[:1]

            probeable = [m for m in models if m not in PLACEHOLDER_MODELS]
            if not probeable:
                targets.append(
                    {
                        "provider": name,
                        "model": None,
                        "instance": provider,
                        "skip_reason": "no models configured",
                    }
                )
                continue
            for model in probeable:
                targets.append({"provider": name, "model": model, "instance": provider})
        return targets

    async def probe_once(self) -> Dict[str, Any]:
        """Probe every provider sequentially and persist the honest results."""
        started = time.time()
        config = self._get_config()
        summary = {"ok": 0, "error": 0, "unavailable": 0}
        results: Dict[str, Any] = {}

        for target in self._targets():
            if self._stop.is_set():
                break
            name, model, provider = target["provider"], target["model"], target["instance"]

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
                results[name] = {
                    "status": "unavailable",
                    "model": model,
                    "reason": reason,
                    "probed_at": int(time.time()),
                }
                continue

            probe_start = time.time()
            status = "error"
            error_msg: Optional[str] = None
            response_text = ""
            try:
                res = await provider.chat_complete(
                    model=model, messages=[{"role": "user", "content": "Hi"}], max_tokens=48
                )
                content = res.get("choices", [{}])[0].get("message", {}).get("content", "")
                response_text = content[:200]
                if self._evaluate(content):
                    error_msg = content[:200]
                else:
                    status = "ok"
            except Exception as e:
                error_msg = str(e)

            latency = round((time.time() - probe_start) * 1000)
            if status == "ok":
                summary["ok"] += 1
            else:
                summary["error"] += 1

            probed_at = int(time.time())
            entry = {
                "status": status,
                "latency_ms": latency,
                "response": response_text,
                "error": error_msg,
                "tested_at": probed_at,
                "source": "prober",
            }
            results[name] = {
                "status": status,
                "model": model,
                "latency_ms": latency,
                "error": error_msg,
                "probed_at": probed_at,
            }

            self._record(config, name, model, entry)
            if self.delay_seconds > 0:
                await asyncio.sleep(self.delay_seconds)

        self.last_run = int(time.time())
        self.last_duration_ms = round((time.time() - started) * 1000)
        self.last_error = None
        state = {
            "last_run": self.last_run,
            "last_duration_ms": self.last_duration_ms,
            "providers": results,
            "summary": summary,
        }
        try:
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
        return {
            "enabled": self.enabled,
            "running": self.running,
            "interval_seconds": self.interval_seconds,
            "delay_seconds": self.delay_seconds,
            "last_run": self.last_run,
            "last_duration_ms": self.last_duration_ms,
            "next_run": self.next_run,
            "last_error": self.last_error,
            "providers": (state or {}).get("providers", {}) if isinstance(state, dict) else {},
            "summary": (state or {}).get("summary", {}) if isinstance(state, dict) else {},
        }

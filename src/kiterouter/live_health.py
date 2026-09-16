"""Passive provider health, derived from real traffic.

Test results only change when something actually runs a test, so between probes
the dashboard shows a frozen snapshot — a provider that has recovered keeps
reading as failed, and one that has broken keeps reading as healthy. This module
records what really happened on live requests instead, so the topology moves on
every request without polling anything upstream.

State is held in memory and written to a small dedicated file on a throttle. It
deliberately does not touch the main config, which is large and rewritten
wholesale on every save.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("kiterouter.live_health")

SAVE_INTERVAL_SECONDS = 15
MAX_ERROR_CHARS = 200


class LiveHealth:
    """Last real outcome per provider, cheap to update on every request."""

    def __init__(self, path: Path, save_interval: float = SAVE_INTERVAL_SECONDS):
        self.path = path
        self._save_interval = save_interval
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._last_save = 0.0
        self.load()

    def load(self) -> None:
        """Restore the last known outcomes so a restart does not blank the view."""
        try:
            if not self.path.exists():
                return
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._entries = {
                    k: v for k, v in data.items() if isinstance(v, dict) and "status" in v
                }
        except Exception as e:
            logger.debug("Could not read live health: %s", e)

    def record(
        self,
        provider: Optional[str],
        status: str,
        model: Optional[str] = None,
        latency_ms: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        """Record the outcome of one real request."""
        if not provider:
            return
        entry: Dict[str, Any] = {
            "status": "ok" if status == "ok" else "error",
            "model": model or "",
            "latency_ms": int(latency_ms or 0),
            "at": int(time.time()),
        }
        if entry["status"] == "error":
            entry["error"] = (error or "Request failed")[:MAX_ERROR_CHARS]
        self._entries[provider] = entry
        self._maybe_save()

    def get(self, provider: str) -> Optional[Dict[str, Any]]:
        return self._entries.get(provider)

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Copy of the current state — callers must not mutate our internals."""
        return {provider: dict(entry) for provider, entry in self._entries.items()}

    def _maybe_save(self) -> None:
        if time.time() - self._last_save < self._save_interval:
            return
        self.flush()

    def flush(self, force: bool = False) -> bool:
        """Persist the current state; throttled unless forced."""
        if not force and time.time() - self._last_save < self._save_interval:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._entries, f)
            self._last_save = time.time()
            return True
        except Exception as e:
            logger.debug("Could not write live health: %s", e)
            return False

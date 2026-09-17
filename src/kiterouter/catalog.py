"""Model catalog sync.

Provider model lists change constantly, so they are treated as data with a
source and a freshness stamp rather than as code or config:

- a **manual** entry is operator intent and is never removed or reclassified
  by a sync;
- a **discovery** entry is an observation, so it expires once the provider
  stops listing it.

Catalogs live in the store, not in ``config.json``, because config is rewritten
wholesale on every save — accumulating hundreds of model entries there made
every save slower and the file larger.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from kiterouter.store import Store

logger = logging.getLogger("kiterouter.catalog")

MAX_MODEL_ID_CHARS = 200


async def sync_provider(
    name: str, provider: Any, store: Store, timeout_seconds: int = 60
) -> Dict[str, Any]:
    """Refresh one provider's catalog. Never raises."""
    started = time.time()
    result: Dict[str, Any] = {"provider": name, "ok": False, "models": 0, "error": None}
    try:
        models = await asyncio.wait_for(provider.fetch_models(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        result["error"] = f"catalog fetch timed out after {timeout_seconds}s"
        return result
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        return result

    clean = [
        str(m)[:MAX_MODEL_ID_CHARS]
        for m in (models or [])
        if isinstance(m, (str, int)) and str(m).strip()
    ]
    if not clean:
        result["error"] = "provider returned no models"
        return result

    try:
        result["models"] = store.record_models(name, clean, source="discovery")
        result["ok"] = True
    except Exception as e:
        result["error"] = f"could not persist catalog: {e}"
    result["duration_ms"] = int((time.time() - started) * 1000)
    return result


async def sync_all(
    router: Any,
    store: Store,
    only: Optional[List[str]] = None,
    timeout_seconds: int = 60,
) -> Dict[str, Any]:
    """Refresh every available provider's catalog, sequentially.

    Sequential on purpose: a burst of catalog requests against a dozen providers
    is exactly the pattern this gateway avoids elsewhere.
    """
    targets: List[Any] = []
    for name, provider in (getattr(router, "providers", {}) or {}).items():
        if only and name not in only:
            continue
        try:
            if await provider.is_available():
                targets.append((name, provider))
        except Exception:
            continue

    outcomes: Dict[str, Any] = {}
    refreshed = 0
    for name, provider in targets:
        outcome = await sync_provider(name, provider, store, timeout_seconds)
        outcomes[name] = outcome
        if outcome.get("ok"):
            refreshed += 1

    return {
        "providers": len(targets),
        "refreshed": refreshed,
        "failed": len(targets) - refreshed,
        "results": outcomes,
        "synced_at": int(time.time()),
    }


def apply_config_models(store: Store, config: Any) -> int:
    """Treat models listed in config as manual entries.

    Config is where the operator (and the import path) pins a model, so those are
    recorded as `manual` and survive every subsequent discovery.
    """
    pinned = 0
    for name, conf in (getattr(config, "providers", None) or {}).items():
        if not isinstance(conf, dict):
            continue
        models = conf.get("models")
        if not isinstance(models, list):
            continue
        for entry in models:
            if isinstance(entry, str):
                model_id = entry
            elif isinstance(entry, dict):
                model_id = entry.get("id") or entry.get("name")
            else:
                model_id = None
            if model_id:
                store.record_manual_model(name, str(model_id))
                pinned += 1
    return pinned

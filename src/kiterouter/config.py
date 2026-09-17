"""Configuration management for KiteRouter."""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_DIR = Path.home() / ".kiterouter"
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class KiteConfig:
    host: str = "127.0.0.1"
    port: int = 3001
    enable_rtk: bool = True
    max_tool_chars: int = 12000
    enable_prober: bool = False
    prober_interval_seconds: int = 900
    prober_delay_seconds: float = 3.0
    prober_timeout_seconds: int = 90
    prober: Dict[str, Any] = field(default_factory=dict)
    # Retention is declared once, here, rather than per call site. A gateway that
    # only ever grows is a slow-motion outage.
    retention_health_checks_days: int = 30
    retention_requests_days: int = 30
    retention_bodies_days: int = 3
    retention_usage_days: int = 365
    store_maintenance_seconds: int = 900
    # test_results live in config.json, which is rewritten wholesale on every
    # save — so they are bounded rather than accumulated forever. Full history
    # belongs in the store.
    retention_test_results_days: int = 30
    max_test_results_per_provider: int = 60
    catalog_refresh_hours: int = 24
    catalog_unseen_days: int = 14
    # Bodies are the disk-heavy part of history: capped per file, and expired
    # well before the request rows that point at them.
    save_bodies: bool = True
    max_body_bytes: int = 200_000
    providers: Dict[str, Any] = field(default_factory=lambda: {
        "cursor": {"enabled": True, "token": "", "machine_id": ""},
        "antigravity": {"enabled": True, "token": "", "project_id": ""},
        "cline": {
            "enabled": True,
            "api_key": "",
            "access_token": "",
            "refresh_token": "",
            "expires_at": 0,
            "email": "",
        },
        "opencode_free": {"enabled": True},
        "opencode_go": {"enabled": True, "api_key": ""},
        "kiro": {"enabled": True, "token": ""},
        "glm": {"enabled": True, "api_key": ""},
        "minimax": {"enabled": True, "api_key": ""},
        "codex": {"enabled": True, "api_key": ""},
        "command_code": {"enabled": True, "api_key": ""},
        "claude": {"enabled": True, "api_key": ""},
        "copilot": {"enabled": True, "token": ""},
        "vertex": {"enabled": True, "api_key": "", "project_id": ""},
        "custom": {"enabled": True, "endpoint": "", "api_key": ""},
    })
    combos: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "KiteConfig":
        if not CONFIG_FILE.exists():
            config = cls()
            config.save()
            return config

        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            saved_providers = data.get("providers", {})
            saved_combos = data.get("combos", {})
            default_config = cls()
            merged_providers = default_config.providers.copy()
            for k, v in saved_providers.items():
                if k in merged_providers and isinstance(v, dict):
                    merged_providers[k].update(v)
                else:
                    merged_providers[k] = v

            port = data.get("port", 3001)
            if port == 20128 or not port:
                port = 3001

            return cls(
                host=data.get("host", "127.0.0.1"),
                port=port,
                enable_rtk=data.get("enable_rtk", True),
                max_tool_chars=data.get("max_tool_chars", 12000),
                enable_prober=data.get("enable_prober", False),
                prober_interval_seconds=data.get("prober_interval_seconds", 900),
                prober_delay_seconds=data.get("prober_delay_seconds", 3.0),
                prober_timeout_seconds=data.get("prober_timeout_seconds", 90),
                prober=data.get("prober", {}) if isinstance(data.get("prober", {}), dict) else {},
                retention_health_checks_days=data.get("retention_health_checks_days", 30),
                retention_requests_days=data.get("retention_requests_days", 30),
                retention_bodies_days=data.get("retention_bodies_days", 3),
                retention_usage_days=data.get("retention_usage_days", 365),
                store_maintenance_seconds=data.get("store_maintenance_seconds", 900),
                retention_test_results_days=data.get("retention_test_results_days", 30),
                max_test_results_per_provider=data.get("max_test_results_per_provider", 60),
                catalog_refresh_hours=data.get("catalog_refresh_hours", 24),
                catalog_unseen_days=data.get("catalog_unseen_days", 14),
                save_bodies=data.get("save_bodies", True),
                max_body_bytes=data.get("max_body_bytes", 200_000),
                providers=merged_providers,
                combos=saved_combos if isinstance(saved_combos, dict) else {},
            )
        except Exception:
            return cls()

    def save(self) -> None:
        if getattr(self, "port", None) == 20128 or not getattr(self, "port", None):
            self.port = 3001
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)

    def set_combo(self, name: str, combo_data: Dict[str, Any]) -> None:
        """Add or update a model combo and persist."""
        self.combos[name] = combo_data
        self.save()

    def delete_combo(self, name: str) -> bool:
        """Delete a model combo and persist."""
        if name in self.combos:
            del self.combos[name]
            self.save()
            return True
        return False

    def update_provider_tokens(self, provider: str, updates: Dict[str, Any]) -> None:
        """Update tokens/credentials for a provider and persist to disk."""
        if provider in self.providers and isinstance(self.providers[provider], dict):
            self.providers[provider].update(updates)
        else:
            self.providers[provider] = updates
        self.save()

    def set_prober_state(self, updates: Dict[str, Any]) -> None:
        """Merge and persist background health prober state."""
        if not isinstance(getattr(self, "prober", None), dict):
            self.prober = {}
        self.prober.update(updates)
        self.save()

    def retention_days(self) -> Dict[str, int]:
        """Retention per stored table — only tables that actually exist."""
        return {
            "health_checks": int(self.retention_health_checks_days),
            "requests": int(self.retention_requests_days),
        }

    def prune_test_results(
        self, now: Optional[float] = None
    ) -> int:
        """Bound the per-model results kept in config.

        Config is rewritten wholesale on every save, so accumulated results make
        every save slower and the file larger. Results older than the retention
        window are dropped, and each provider is capped as a backstop. The full
        history lives in the store, so nothing is actually lost.
        """
        moment = float(now if now is not None else time.time())
        cutoff = moment - int(self.retention_test_results_days) * 86400
        removed = 0

        for provider, conf in (self.providers or {}).items():
            if not isinstance(conf, dict):
                continue
            results = conf.get("test_results")
            if not isinstance(results, dict) or not results:
                continue

            kept = {
                model: entry
                for model, entry in results.items()
                if isinstance(entry, dict) and (entry.get("tested_at") or 0) >= cutoff
            }
            removed += len(results) - len(kept)

            cap = int(self.max_test_results_per_provider)
            if cap and len(kept) > cap:
                newest = sorted(
                    kept.items(), key=lambda kv: kv[1].get("tested_at") or 0, reverse=True
                )[:cap]
                removed += len(kept) - len(newest)
                kept = dict(newest)

            if kept:
                conf["test_results"] = kept
            elif "test_results" in conf:
                conf["test_results"] = {}

        return removed


def persist_provider_tokens(provider: str, updates: Dict[str, Any]) -> None:
    """Safely update and persist provider tokens in ~/.kiterouter/config.json."""
    try:
        cfg = KiteConfig.load()
        cfg.update_provider_tokens(provider, updates)
    except Exception:
        pass

"""Configuration management for KiteRouter."""
from __future__ import annotations

import json
import os
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
    providers: Dict[str, Any] = field(default_factory=lambda: {
        "opencode_free": {"enabled": True},
        "opencode_go": {"enabled": True, "api_key": ""},
        "cursor": {"enabled": True, "token": "", "machine_id": ""},
        "antigravity": {"enabled": True, "token": ""},
        "cline": {"enabled": True, "api_key": ""},
    })

    @classmethod
    def load(cls) -> "KiteConfig":
        if not CONFIG_FILE.exists():
            config = cls()
            config.save()
            return config

        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return cls(
                host=data.get("host", "127.0.0.1"),
                port=data.get("port", 3001),
                enable_rtk=data.get("enable_rtk", True),
                max_tool_chars=data.get("max_tool_chars", 12000),
                providers=data.get("providers", {}),
            )
        except Exception:
            return cls()

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)

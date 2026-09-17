"""Shared test fixtures."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """Point config persistence at a throwaway file.

    Endpoint tests exercise real save paths (``/api/config``, ``/api/sync-source``,
    ``/api/fetch-token``), so without this the suite overwrites the developer's
    live ``~/.kiterouter/config.json`` with whatever the tests imported.
    """
    config_dir = tmp_path / "kiterouter"
    config_dir.mkdir()
    monkeypatch.setattr("kiterouter.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("kiterouter.config.CONFIG_FILE", config_dir / "config.json")

    # The server keeps its own copies of these paths, so patching config.py
    # alone still let request logging and live health write to the real ones.
    from kiterouter import server

    monkeypatch.setattr(server, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(server, "REQUEST_LOG_FILE", config_dir / "request_log.json")
    monkeypatch.setattr(server, "LIVE_HEALTH_FILE", config_dir / "live_health.json")
    monkeypatch.setattr(server.live_health, "path", config_dir / "live_health.json")
    monkeypatch.setattr(server.live_health, "_entries", {})
    monkeypatch.setattr(server.live_health, "_last_save", 0.0)

    # The store is created at import against the real home directory. Patching
    # its path would not help: the sqlite connection is already open against the
    # old file, so writes would still land in the developer's real database.
    monkeypatch.setattr(server, "STORE_FILE", config_dir / "kiterouter.db")
    monkeypatch.setattr(server, "BODY_DIR", config_dir / "bodies")
    server.store.reopen(config_dir / "kiterouter.db")


@pytest.fixture(autouse=True)
def _block_real_config_writes(monkeypatch):
    """Keep adapters from persisting refreshed credentials to the live config."""
    monkeypatch.setattr(
        "kiterouter.providers.cline.persist_provider_tokens", lambda *a, **k: None
    )

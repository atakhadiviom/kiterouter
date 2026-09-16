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


@pytest.fixture(autouse=True)
def _block_real_config_writes(monkeypatch):
    """Keep adapters from persisting refreshed credentials to the live config."""
    monkeypatch.setattr(
        "kiterouter.providers.cline.persist_provider_tokens", lambda *a, **k: None
    )

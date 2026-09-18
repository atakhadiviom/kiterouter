"""Tests for model catalogs, their provenance rules, and the config bound."""
from __future__ import annotations

import asyncio
import time

import pytest

from kiterouter import catalog
from kiterouter.config import KiteConfig
from kiterouter.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "kiterouter.db")
    yield s
    s.close()


# ── store: catalogs ──────────────────────────────────────────────────────────

def test_catalog_records_and_reads_back(store):
    store.record_models("groq", ["llama-3.3", "mixtral"], source="discovery")
    assert store.models_for("groq") == ["llama-3.3", "mixtral"]
    assert store.all_models() == {"groq": ["llama-3.3", "mixtral"]}


def test_recording_the_same_model_twice_does_not_duplicate(store):
    store.record_models("groq", ["a", "b"])
    store.record_models("groq", ["a", "b", "c"])
    assert store.models_for("groq") == ["a", "b", "c"]


def test_a_pinned_model_survives_rediscovery_and_stays_manual(store):
    """Discovery must not reclassify what the operator pinned."""
    store.record_manual_model("groq", "mine")
    store.record_models("groq", ["mine", "theirs"], source="discovery")

    freshness = store.catalog_freshness()["groq"]
    assert freshness["manual"] == 1
    assert freshness["models"] == 2


def test_a_manual_model_is_pinned_at_first(store):
    store.record_models("groq", ["a"])
    store.record_manual_model("groq", "a")
    assert store.catalog_freshness()["groq"]["manual"] == 1


def test_expiry_drops_unseen_discoveries_but_never_pins(store):
    now = int(time.time())
    store.record_models("groq", ["stale"], now=now - 40 * 86400)
    store.record_models("groq", ["fresh"], now=now - 3600)
    store.record_manual_model("groq", "pinned", now=now - 400 * 86400)

    removed = store.expire_models(unseen_days=14, now=now)

    assert removed == 1
    assert store.models_for("groq") == ["fresh", "pinned"]


def test_expiry_can_be_disabled(store):
    store.record_models("groq", ["old"], now=1)
    assert store.expire_models(unseen_days=0) == 0
    assert store.models_for("groq") == ["old"]


def test_freshness_reports_counts_and_sync_time(store):
    now = int(time.time())
    store.record_models("groq", ["a", "b"], now=now)
    info = store.catalog_freshness()["groq"]
    assert info["models"] == 2
    assert info["synced_at"] == now


def test_blank_model_ids_are_ignored(store):
    store.record_models("groq", ["a", "", None])
    assert store.models_for("groq") == ["a"]


# ── catalog sync ─────────────────────────────────────────────────────────────

class FakeProvider:
    def __init__(self, models=None, available=True, raises=None, delay=0.0):
        self._models = models or []
        self._available = available
        self._raises = raises
        self._delay = delay
        self.calls = 0

    async def is_available(self):
        return self._available

    async def fetch_models(self):
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises:
            raise self._raises
        return list(self._models)


class FakeRouter:
    def __init__(self, providers):
        self.providers = providers


def test_sync_persists_a_catalog(store):
    result = asyncio.run(catalog.sync_provider("groq", FakeProvider(["a", "b"]), store))
    assert result["ok"] is True and result["models"] == 2
    assert store.models_for("groq") == ["a", "b"]


def test_sync_reports_an_empty_catalog_rather_than_clearing_it(store):
    """An empty response is a failure, not evidence the models are gone."""
    store.record_models("groq", ["a"])
    result = asyncio.run(catalog.sync_provider("groq", FakeProvider([]), store))
    assert result["ok"] is False
    assert "no models" in result["error"]
    assert store.models_for("groq") == ["a"], "an empty answer must not wipe the catalog"


def test_sync_reports_a_provider_exception(store):
    result = asyncio.run(
        catalog.sync_provider("groq", FakeProvider(raises=RuntimeError("boom")), store)
    )
    assert result["ok"] is False and "boom" in result["error"]


def test_sync_times_out_instead_of_hanging(store):
    result = asyncio.run(
        catalog.sync_provider("slow", FakeProvider(["a"], delay=1.0), store, timeout_seconds=0.05)
    )
    assert result["ok"] is False and "timed out" in result["error"]


def test_sync_all_skips_unavailable_providers(store):
    router = FakeRouter(
        {"a": FakeProvider(["m1"]), "b": FakeProvider(["m2"], available=False)}
    )
    result = asyncio.run(catalog.sync_all(router, store))

    assert result["providers"] == 1
    assert result["refreshed"] == 1
    assert store.models_for("b") == [], "an unavailable provider should not be fetched"


def test_sync_all_can_be_limited_to_one_provider(store):
    router = FakeRouter({"a": FakeProvider(["m1"]), "b": FakeProvider(["m2"])})
    asyncio.run(catalog.sync_all(router, store, only=["b"]))
    assert store.models_for("a") == []
    assert store.models_for("b") == ["m2"]


def test_sync_all_counts_failures(store):
    router = FakeRouter({"a": FakeProvider(["m1"]), "b": FakeProvider(raises=ValueError("x"))})
    result = asyncio.run(catalog.sync_all(router, store))
    assert result["refreshed"] == 1 and result["failed"] == 1


def test_config_models_are_pinned_as_manual(store):
    config = KiteConfig()
    config.providers = {
        "a": {"models": ["one", {"id": "two"}, {"name": "three"}, {"nope": 1}, 5]},
        "b": {"models": "not-a-list"},
    }
    pinned = catalog.apply_config_models(store, config)

    assert pinned == 3
    assert store.models_for("a") == ["one", "three", "two"]
    assert store.catalog_freshness()["a"]["manual"] == 3


# ── the config bound ─────────────────────────────────────────────────────────

def test_prune_drops_results_older_than_retention():
    now = time.time()
    config = KiteConfig()
    config.providers = {
        "a": {
            "test_results": {
                "old": {"status": "ok", "tested_at": now - 40 * 86400},
                "new": {"status": "ok", "tested_at": now - 3600},
            }
        }
    }
    removed = config.prune_test_results(now=now)

    assert removed == 1
    assert set(config.providers["a"]["test_results"]) == {"new"}


def test_prune_caps_each_provider():
    now = time.time()
    config = KiteConfig()
    config.max_test_results_per_provider = 3
    config.providers = {
        "a": {
            "test_results": {
                f"m{i}": {"status": "ok", "tested_at": now - i} for i in range(10)
            }
        }
    }
    removed = config.prune_test_results(now=now)

    kept = config.providers["a"]["test_results"]
    assert removed == 7
    assert len(kept) == 3
    assert set(kept) == {"m0", "m1", "m2"}, "the newest results are the ones kept"


def test_prune_leaves_a_healthy_config_alone():
    now = time.time()
    config = KiteConfig()
    config.providers = {"a": {"test_results": {"m": {"status": "ok", "tested_at": now}}}}
    assert config.prune_test_results(now=now) == 0
    assert set(config.providers["a"]["test_results"]) == {"m"}


def test_prune_tolerates_missing_or_odd_results():
    config = KiteConfig()
    config.providers = {"a": {}, "b": {"test_results": {}}, "c": "not-a-dict"}
    assert config.prune_test_results() == 0


def test_retention_days_covers_the_tables_that_exist():
    """Declared retention must name real tables, or it prunes nothing."""
    config = KiteConfig()
    config.retention_health_checks_days = 14
    config.retention_requests_days = 45
    assert config.retention_days() == {
        "health_checks": 14,
        "requests": 45,
        "quota_snapshots": 90,
        "events": 90,
    }


def test_catalog_settings_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr("kiterouter.config.CONFIG_FILE", tmp_path / "config.json")
    config = KiteConfig()
    config.catalog_refresh_hours = 6
    config.catalog_unseen_days = 7
    config.retention_test_results_days = 14
    config.save()

    reloaded = KiteConfig.load()
    assert reloaded.catalog_refresh_hours == 6
    assert reloaded.catalog_unseen_days == 7
    assert reloaded.retention_test_results_days == 14
    assert reloaded.port == 3001

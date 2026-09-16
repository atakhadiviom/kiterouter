"""Tests for the background health prober."""
from __future__ import annotations

import asyncio

import pytest

from kiterouter.config import KiteConfig
from kiterouter.prober import HealthProber


def evaluate(text: str) -> bool:
    """Stand-in for the server's honest-status check."""
    stripped = text.strip().lower()
    if not stripped:
        return True
    return any(m in stripped for m in ("error", "unauthorized", "http 4", "http 5"))


class FakeProvider:
    def __init__(self, name, models, content="Hello there", available=True, raises=None):
        self.name = name
        self._models = models
        self._content = content
        self._available = available
        self._raises = raises
        self.calls = []

    def get_models(self):
        return list(self._models)

    async def is_available(self):
        return self._available

    async def chat_complete(self, model, messages, max_tokens=None, **kwargs):
        self.calls.append(model)
        if self._raises:
            raise self._raises
        return {"choices": [{"message": {"content": self._content}}]}


class FakeRouter:
    def __init__(self, providers):
        self.providers = providers


@pytest.fixture
def prober_factory(monkeypatch, tmp_path):
    monkeypatch.setattr("kiterouter.config.CONFIG_FILE", tmp_path / "config.json")

    def build(providers, **kwargs):
        config = KiteConfig()
        config.providers = {name: {} for name in providers}
        router = FakeRouter(providers)
        prober = HealthProber(
            get_router=lambda: router,
            get_config=lambda: config,
            evaluate=evaluate,
            interval_seconds=kwargs.pop("interval_seconds", 900),
            delay_seconds=kwargs.pop("delay_seconds", 0),
        )
        return prober, config

    return build


def test_probe_once_records_honest_success(prober_factory):
    provider = FakeProvider("alpha", ["model-a"])
    prober, config = prober_factory({"alpha": provider})

    state = asyncio.run(prober.probe_once())

    assert state["summary"] == {"ok": 1, "error": 0, "unavailable": 0}
    assert config.providers["alpha"]["test_results"]["model-a"]["status"] == "ok"
    assert config.providers["alpha"]["test_results"]["model-a"]["source"] == "prober"
    assert config.providers["alpha"]["last_test_status"] == "ok"
    assert config.prober["summary"]["ok"] == 1


def test_probe_once_marks_error_content_as_error(prober_factory):
    provider = FakeProvider("beta", ["model-b"], content="Cline upstream HTTP 401: unauthorized")
    prober, config = prober_factory({"beta": provider})

    state = asyncio.run(prober.probe_once())

    assert state["summary"]["error"] == 1
    assert config.providers["beta"]["test_results"]["model-b"]["status"] == "error"
    assert config.providers["beta"]["last_test_status"] == "error"


def test_probe_once_marks_raised_exceptions_as_error(prober_factory):
    provider = FakeProvider("gamma", ["model-c"], raises=RuntimeError("connection refused"))
    prober, config = prober_factory({"gamma": provider})

    state = asyncio.run(prober.probe_once())

    assert state["providers"]["gamma"]["status"] == "error"
    assert "connection refused" in state["providers"]["gamma"]["error"]


def test_probe_once_skips_unavailable_providers_without_calling_them(prober_factory):
    provider = FakeProvider("delta", ["model-d"], available=False)
    prober, config = prober_factory({"delta": provider})

    state = asyncio.run(prober.probe_once())

    assert state["providers"]["delta"]["status"] == "unavailable"
    assert state["providers"]["delta"]["reason"] == "not configured"
    assert state["summary"] == {"ok": 0, "error": 0, "unavailable": 1}
    assert provider.calls == []
    assert "delta" not in config.providers["delta"].get("test_results", {})


def test_probe_skips_placeholder_models_without_spending_a_request(prober_factory):
    provider = FakeProvider("placeholder", ["custom/default"])
    prober, _ = prober_factory({"placeholder": provider})

    state = asyncio.run(prober.probe_once())

    assert state["providers"]["placeholder"]["status"] == "unavailable"
    assert state["providers"]["placeholder"]["reason"] == "no models configured"
    assert provider.calls == []


def test_probe_probes_only_the_first_model_by_default(prober_factory):
    provider = FakeProvider("epsilon", ["m1", "m2", "m3"])
    prober, _ = prober_factory({"epsilon": provider})

    asyncio.run(prober.probe_once())

    assert provider.calls == ["m1"]


def test_probe_honours_configured_model_overrides(prober_factory):
    provider = FakeProvider("zeta", ["m1", "m2"])
    prober, config = prober_factory({"zeta": provider})
    config.prober["models"] = {"zeta": ["m2"]}

    asyncio.run(prober.probe_once())

    assert provider.calls == ["m2"]


def test_probe_reports_timestamps(prober_factory):
    prober, _ = prober_factory({"eta": FakeProvider("eta", ["m1"])})

    state = asyncio.run(prober.probe_once())

    assert state["last_run"] is not None
    assert prober.last_run is not None
    assert prober.last_duration_ms is not None


def test_start_and_stop_manage_the_background_task(prober_factory):
    prober, _ = prober_factory({"theta": FakeProvider("theta", ["m1"])})

    assert prober.running is False

    async def lifecycle():
        prober.start()
        assert prober.running is True
        assert prober.enabled is True
        await prober.stop()
        assert prober.running is False
        assert prober.enabled is False

    asyncio.run(lifecycle())


def test_start_is_idempotent(prober_factory):
    prober, _ = prober_factory({"iota": FakeProvider("iota", ["m1"])})

    async def lifecycle():
        prober.start()
        first = prober._task
        prober.start()
        assert prober._task is first
        await prober.stop()

    asyncio.run(lifecycle())


def test_status_exposes_last_results(prober_factory):
    prober, _ = prober_factory({"kappa": FakeProvider("kappa", ["m1"])})
    asyncio.run(prober.probe_once())

    status = prober.status()

    assert status["providers"]["kappa"]["status"] == "ok"
    assert status["summary"]["ok"] == 1
    assert status["interval_seconds"] == 900


def test_prober_settings_round_trip_through_config(monkeypatch, tmp_path):
    monkeypatch.setattr("kiterouter.config.CONFIG_FILE", tmp_path / "config.json")
    config = KiteConfig()
    config.enable_prober = True
    config.prober_interval_seconds = 120
    config.prober_delay_seconds = 1.5
    config.set_prober_state({"last_run": 42, "summary": {"ok": 3}})
    config.save()

    reloaded = KiteConfig.load()
    assert reloaded.enable_prober is True
    assert reloaded.prober_interval_seconds == 120
    assert reloaded.prober_delay_seconds == 1.5
    assert reloaded.prober["last_run"] == 42
    assert reloaded.port == 3001

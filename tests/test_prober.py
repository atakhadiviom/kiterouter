"""Tests for the background health prober."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from kiterouter.config import KiteConfig
from kiterouter.prober import (
    BACKOFF_BASE_SECONDS,
    MAX_BACKOFF_SECONDS,
    HealthProber,
    connection_id,
    is_terminal_error,
    probe_stream,
)


def evaluate(text: str) -> bool:
    """Stand-in for the server's honest-status check."""
    stripped = text.strip().lower()
    if not stripped:
        return True
    return any(m in stripped for m in ("error", "unauthorized", "http 4", "http 5"))


def sse(text: str) -> str:
    payload = {"choices": [{"delta": {"content": text}, "index": 0}]}
    return f"data: {json.dumps(payload)}\n\n"


class FakeProvider:
    """Streams like a real adapter: OpenAI-format SSE chunks."""

    def __init__(
        self,
        name,
        models,
        content="Hello there",
        available=True,
        raises=None,
        chunk_delay=0.0,
        ttft_delay=None,
    ):
        self.name = name
        self._models = models
        self._content = content
        self._available = available
        self._raises = raises
        self._chunk_delay = chunk_delay
        self._ttft_delay = ttft_delay
        self.calls = []

    def get_models(self):
        return list(self._models)

    async def is_available(self):
        return self._available

    async def stream_chat(self, model, messages, **kwargs):
        self.calls.append(model)
        if self._raises:
            raise self._raises
        if self._ttft_delay:
            await asyncio.sleep(self._ttft_delay)
        yield sse(self._content)
        if self._chunk_delay:
            await asyncio.sleep(self._chunk_delay)
        yield "data: [DONE]\n\n"


class FakeRouter:
    def __init__(self, providers):
        self.providers = providers


@pytest.fixture
def prober_factory(monkeypatch, tmp_path):
    monkeypatch.setattr("kiterouter.config.CONFIG_FILE", tmp_path / "config.json")

    def build(providers, provider_config=None, **kwargs):
        config = KiteConfig()
        config.providers = provider_config or {name: {} for name in providers}
        router = FakeRouter(providers)
        recorded = []
        prober = HealthProber(
            get_router=lambda: router,
            get_config=lambda: config,
            evaluate=evaluate,
            interval_seconds=kwargs.pop("interval_seconds", 900),
            delay_seconds=kwargs.pop("delay_seconds", 0),
            timeout_seconds=kwargs.pop("timeout_seconds", 5),
            record_health=kwargs.pop("record_health", None)
            or (lambda *a, **k: recorded.append((a, k))),
        )
        return prober, config, recorded

    return build


# ── connection identity ──────────────────────────────────────────────────────

def test_connection_id_is_stable_for_the_same_credential():
    conf = {"api_key": "abc123"}
    assert connection_id(conf) == connection_id(dict(conf))


def test_a_new_credential_is_a_new_connection():
    """Otherwise a replacement key inherits the old key's failures."""
    assert connection_id({"api_key": "old"}) != connection_id({"api_key": "new"})


def test_connection_id_falls_back_when_unconfigured():
    assert connection_id({}) == "default"
    assert connection_id({"api_key": "", "token": None}) == "default"


def test_connection_id_ignores_unrelated_fields():
    assert connection_id({"api_key": "k", "enabled": True}) == connection_id({"api_key": "k"})


# ── terminal vs retryable failures ───────────────────────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "Cline upstream HTTP 401",
        "unauthorized",
        "invalid_grant",
        "token expired",
        "HTTP 403 forbidden",
    ],
)
def test_terminal_failures_are_recognised(text):
    assert is_terminal_error(text) is True


@pytest.mark.parametrize("text", ["HTTP 500", "connection reset", "rate limited", "", None])
def test_transient_failures_are_not_terminal(text):
    assert is_terminal_error(text) is False


# ── TTFT ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_probe_stream_measures_ttft_and_latency():
    provider = FakeProvider("p", ["m"], ttft_delay=0.05, chunk_delay=0.05)
    result = await probe_stream(provider, "m", timeout_seconds=5)

    assert result["error"] is None
    assert result["text"] == "Hello there"
    assert result["ttft_ms"] >= 40, "ttft should reflect the first chunk's delay"
    assert result["latency_ms"] > result["ttft_ms"], "latency covers the whole stream"


@pytest.mark.asyncio
async def test_a_buffering_provider_reports_ttft_close_to_latency():
    """Honest: if the adapter buffers, the client waits for everything anyway."""

    class Buffering(FakeProvider):
        async def stream_chat(self, model, messages, **kwargs):
            await asyncio.sleep(0.05)
            yield sse("all at once")

    result = await probe_stream(Buffering("p", ["m"]), "m", timeout_seconds=5)
    assert result["ttft_ms"] is not None
    assert abs(result["ttft_ms"] - result["latency_ms"]) < 25


@pytest.mark.asyncio
async def test_probe_stream_records_a_timeout_without_raising():
    class Hanging(FakeProvider):
        async def stream_chat(self, model, messages, **kwargs):
            await asyncio.sleep(10)
            yield sse("too late")

    result = await probe_stream(Hanging("p", ["m"]), "m", timeout_seconds=0.05)

    assert result["error"] is not None and "timed out" in result["error"]
    assert result["ttft_ms"] is None


@pytest.mark.asyncio
async def test_probe_stream_captures_exceptions():
    result = await probe_stream(FakeProvider("p", ["m"], raises=RuntimeError("boom")), "m")
    assert "boom" in result["error"]


# ── probing ──────────────────────────────────────────────────────────────────

def test_probe_once_records_honest_success(prober_factory):
    provider = FakeProvider("alpha", ["model-a"])
    prober, config, recorded = prober_factory({"alpha": provider})

    state = asyncio.run(prober.probe_once())

    assert state["summary"]["ok"] == 1
    assert config.providers["alpha"]["test_results"]["model-a"]["status"] == "ok"
    assert config.providers["alpha"]["test_results"]["model-a"]["source"] == "prober"
    assert config.providers["alpha"]["last_test_status"] == "ok"
    assert config.prober["summary"]["ok"] == 1
    assert len(recorded) == 1, "the probe should reach the store"


def test_probe_once_marks_error_content_as_error(prober_factory):
    provider = FakeProvider("beta", ["model-b"], content="Cline upstream HTTP 401: unauthorized")
    prober, config, recorded = prober_factory({"beta": provider})

    state = asyncio.run(prober.probe_once())

    assert state["summary"]["error"] == 1
    assert config.providers["beta"]["test_results"]["model-b"]["status"] == "error"
    assert config.providers["beta"]["last_test_status"] == "error"
    assert recorded[0][1]["error"].startswith("Cline")


def test_probe_once_marks_raised_exceptions_as_error(prober_factory):
    provider = FakeProvider("gamma", ["model-c"], raises=RuntimeError("connection refused"))
    prober, _, _ = prober_factory({"gamma": provider})

    state = asyncio.run(prober.probe_once())

    assert state["connections"]["gamma|default"]["status"] == "error"
    assert "connection refused" in state["connections"]["gamma|default"]["error"]


def test_probes_are_recorded_with_ttft(prober_factory):
    provider = FakeProvider("theta", ["m1"], ttft_delay=0.03)
    prober, _, recorded = prober_factory({"theta": provider})

    asyncio.run(prober.probe_once())

    positionals, keywords = recorded[0]
    assert positionals[0] == "theta"
    assert keywords["ttft_ms"] is not None
    assert keywords["latency_ms"] >= keywords["ttft_ms"]


def test_probes_are_keyed_by_connection_not_just_provider(prober_factory):
    provider = FakeProvider("delta", ["model-d"])
    prober, _, _ = prober_factory(
        {"delta": provider}, provider_config={"delta": {"api_key": "secret"}}
    )

    state = asyncio.run(prober.probe_once())

    key = next(iter(state["connections"]))
    assert key.startswith("delta|")
    assert key.split("|", 1)[1] == connection_id({"api_key": "secret"})


def test_each_connection_is_probed_on_its_own(prober_factory):
    a = FakeProvider("a", ["m1"])
    b = FakeProvider("b", ["m2"], content="HTTP 500 upstream broke")
    prober, _, recorded = prober_factory({"a": a, "b": b})

    state = asyncio.run(prober.probe_once())

    assert state["summary"]["ok"] == 1
    assert state["summary"]["error"] == 1
    assert {r[0][0] for r in recorded} == {"a", "b"}


def test_probe_once_skips_unavailable_providers_without_calling_them(prober_factory):
    provider = FakeProvider("unavail", ["m"], available=False)
    prober, config, _ = prober_factory({"unavail": provider})

    state = asyncio.run(prober.probe_once())

    assert state["connections"]["unavail|default"]["status"] == "unavailable"
    assert state["connections"]["unavail|default"]["reason"] == "not configured"
    assert state["summary"]["unavailable"] == 1
    assert provider.calls == []
    assert "unavail" not in config.providers["unavail"].get("test_results", {})


def test_probe_skips_placeholder_models_without_spending_a_request(prober_factory):
    provider = FakeProvider("placeholder", ["custom/default"])
    prober, _, _ = prober_factory({"placeholder": provider})

    state = asyncio.run(prober.probe_once())

    assert state["connections"]["placeholder|default"]["status"] == "unavailable"
    assert state["connections"]["placeholder|default"]["reason"] == "no models configured"
    assert provider.calls == []


def test_probe_probes_only_the_first_model_by_default(prober_factory):
    provider = FakeProvider("epsilon", ["m1", "m2", "m3"])
    prober, _, _ = prober_factory({"epsilon": provider})

    asyncio.run(prober.probe_once())

    assert provider.calls == ["m1"]


def test_probe_honours_configured_model_overrides(prober_factory):
    provider = FakeProvider("zeta", ["m1", "m2"])
    prober, config, _ = prober_factory({"zeta": provider})
    config.prober["models"] = {"zeta": ["m2"]}

    asyncio.run(prober.probe_once())

    assert provider.calls == ["m2"]


def test_probe_reports_timestamps(prober_factory):
    prober, _, _ = prober_factory({"eta": FakeProvider("eta", ["m1"])})

    state = asyncio.run(prober.probe_once())

    assert state["last_run"] is not None
    assert prober.last_run is not None
    assert prober.last_duration_ms is not None


# ── backoff ──────────────────────────────────────────────────────────────────

def test_a_failing_connection_backs_off_instead_of_being_retried(prober_factory):
    provider = FakeProvider("flaky", ["m"], content="HTTP 500 boom")
    prober, _, _ = prober_factory({"flaky": provider})

    first = asyncio.run(prober.probe_once())
    assert first["summary"]["error"] == 1

    second = asyncio.run(prober.probe_once())
    assert second["summary"]["skipped"] == 1, "a failing connection must not be hammered"
    assert provider.calls == ["m"], "the provider should not have been called again"


def test_backoff_grows_with_consecutive_failures_and_saturates(prober_factory):
    prober, _, _ = prober_factory({})
    key = "flaky|default"

    for expected in (1, 2, 3):
        state = prober._note_failure(key, "HTTP 500 boom")
        assert state["failures"] == expected

    for _ in range(20):
        state = prober._note_failure(key, "HTTP 500 boom")
    remaining = state["next_at"] - time.time()
    assert BACKOFF_BASE_SECONDS <= remaining <= MAX_BACKOFF_SECONDS + 1


def test_terminal_failures_back_off_harder_and_flag_the_operator(prober_factory):
    prober, _, _ = prober_factory({})
    state = prober._note_failure("cline|abc", "Cline HTTP 401 unauthorized")

    assert state["needs_action"] is True
    remaining = state["next_at"] - time.time()
    assert remaining > MAX_BACKOFF_SECONDS, "a revoked credential needs a human, not a retry"


def test_a_success_clears_the_backoff(prober_factory):
    prober, _, _ = prober_factory({})
    key = "recover|default"

    prober._note_failure(key, "boom")
    assert prober.backoff_state()[key]["failures"] == 1

    prober._note_success(key)
    state = prober.backoff_state()[key]
    assert state["failures"] == 0
    assert state["next_at"] == 0.0
    assert state["needs_action"] is False
    assert state["last_error"] is None


def test_needs_action_is_reported_in_status(prober_factory):
    prober, _, _ = prober_factory({})
    prober._note_failure("codex|xyz", "token expired")

    status = prober.status()
    assert "codex|xyz" in status["needs_action"]
    assert status["timeout_seconds"] == 5


# ── lifecycle ────────────────────────────────────────────────────────────────

def test_start_and_stop_manage_the_background_task(prober_factory):
    prober, _, _ = prober_factory({"iota": FakeProvider("iota", ["m1"])})

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
    prober, _, _ = prober_factory({"lam": FakeProvider("lam", ["m1"])})

    async def lifecycle():
        prober.start()
        first = prober._task
        prober.start()
        assert prober._task is first
        await prober.stop()

    asyncio.run(lifecycle())


def test_status_exposes_connection_results(prober_factory):
    prober, _, _ = prober_factory({"kappa": FakeProvider("kappa", ["m1"])})
    asyncio.run(prober.probe_once())

    status = prober.status()

    assert status["summary"]["ok"] == 1
    assert status["connections"], "per-connection results should be exposed"
    assert status["providers"]["kappa"]["status"] == "ok", "old shape kept for provider cards"
    assert status["interval_seconds"] == 900


def test_prober_settings_round_trip_through_config(monkeypatch, tmp_path):
    monkeypatch.setattr("kiterouter.config.CONFIG_FILE", tmp_path / "config.json")
    config = KiteConfig()
    config.enable_prober = True
    config.prober_interval_seconds = 60
    config.prober_delay_seconds = 1.5
    config.prober_timeout_seconds = 120
    config.retention_health_checks_days = 14
    config.set_prober_state({"last_run": 42, "summary": {"ok": 3}})
    config.save()

    reloaded = KiteConfig.load()
    assert reloaded.enable_prober is True
    assert reloaded.prober_interval_seconds == 60
    assert reloaded.prober_delay_seconds == 1.5
    assert reloaded.prober_timeout_seconds == 120
    assert reloaded.retention_health_checks_days == 14
    assert reloaded.retention_days() == {"health_checks": 14, "requests": 30}
    assert reloaded.prober["last_run"] == 42
    assert reloaded.port == 3001

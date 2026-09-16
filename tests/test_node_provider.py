"""Tests for declarative provider nodes and the shared wire translators."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from kiterouter.config import KiteConfig
from kiterouter.providers import translate
from kiterouter.providers.node import NodeProvider, join_url
from kiterouter.router import ProviderRouter


def collect(provider, model="m", **kwargs):
    async def _run():
        out = []
        async for chunk in provider.stream_chat(
            model=model, messages=kwargs.pop("messages", [{"role": "user", "content": "Hi"}]), **kwargs
        ):
            out.append(chunk)
        return out

    return asyncio.run(_run())


# ── URL joining ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "base,path,expected",
    [
        ("https://api.x.com/v1", "/chat/completions", "https://api.x.com/v1/chat/completions"),
        ("https://api.x.com/v1/", "/chat/completions", "https://api.x.com/v1/chat/completions"),
        ("https://api.x.com/v1/", "chat/completions", "https://api.x.com/v1/chat/completions"),
        ("https://api.x.com/v1", "https://other/y", "https://other/y"),
        ("", "/x", "/x"),
        ("https://api.x.com", "", "https://api.x.com"),
    ],
)
def test_join_url(base, path, expected):
    assert join_url(base, path) == expected


# ── defaults per api_type ────────────────────────────────────────────────────

def test_openai_node_defaults():
    node = NodeProvider("n", {"base_url": "https://api.x.com/v1", "api_key": "k"})
    assert node.api_type == "openai-compatible"
    assert node.chat_url("m") == "https://api.x.com/v1/chat/completions"
    assert node.models_url() == "https://api.x.com/v1/models"
    assert node.headers()["authorization"] == "Bearer k"


def test_anthropic_node_defaults_to_its_own_path_and_auth():
    node = NodeProvider("a", {"api_type": "anthropic", "base_url": "https://api.anthropic.com/v1", "api_key": "k"})
    assert node.chat_url("m") == "https://api.anthropic.com/v1/messages"
    headers = node.headers()
    assert headers["x-api-key"] == "k"
    assert headers["anthropic-version"] == translate.ANTHROPIC_VERSION
    assert "authorization" not in headers, "Anthropic uses x-api-key, not bearer"


def test_gemini_node_substitutes_the_model_into_the_path():
    node = NodeProvider("g", {"api_type": "gemini", "base_url": "https://g.example/v1beta", "api_key": "k"})
    assert node.chat_url("gemini-3-flash") == (
        "https://g.example/v1beta/models/gemini-3-flash:streamGenerateContent?alt=sse"
    )


def test_responses_node_uses_the_responses_path():
    node = NodeProvider("r", {"api_type": "openai-responses", "base_url": "https://api.x.com/v1"})
    assert node.chat_url("m") == "https://api.x.com/v1/responses"


def test_unknown_api_type_falls_back_to_openai_rather_than_failing_later():
    node = NodeProvider("weird", {"api_type": "carrier-pigeon", "base_url": "https://api.x.com/v1"})
    assert node.api_type == "openai-compatible"


def test_custom_headers_win_over_defaults():
    node = NodeProvider(
        "n",
        {"base_url": "https://api.x.com/v1", "api_key": "k", "custom_headers": {"x-trace": "1", "authorization": "Token z"}},
    )
    headers = node.headers()
    assert headers["x-trace"] == "1"
    assert headers["authorization"] == "Token z"


def test_absolute_chat_path_overrides_the_base_url():
    node = NodeProvider("n", {"base_url": "https://api.x.com/v1", "chat_path": "https://edge/x"})
    assert node.chat_url("m") == "https://edge/x"


# ── availability ─────────────────────────────────────────────────────────────

def test_availability_requires_a_credential_when_auth_is_on():
    assert asyncio.run(NodeProvider("n", {"base_url": "https://api.x.com/v1"}).is_available()) is False
    assert asyncio.run(NodeProvider("n", {"base_url": "https://api.x.com/v1", "api_key": "k"}).is_available()) is True


def test_keyless_auth_needs_no_credential():
    node = NodeProvider("n", {"base_url": "https://api.x.com/v1", "auth": "none"})
    assert asyncio.run(node.is_available()) is True


def test_disabled_or_urlless_nodes_are_unavailable():
    assert asyncio.run(NodeProvider("n", {"base_url": "https://api.x.com/v1", "api_key": "k", "enabled": False}).is_available()) is False
    assert asyncio.run(NodeProvider("n", {"api_key": "k"}).is_available()) is False


# ── request payloads ─────────────────────────────────────────────────────────

def test_openai_payload_passes_messages_through():
    node = NodeProvider("n", {"base_url": "https://api.x.com/v1", "api_key": "k"})
    payload = node._payload("m", [{"role": "user", "content": "hi"}], 0.5, 64)
    assert payload["model"] == "m"
    assert payload["stream"] is True
    assert payload["max_tokens"] == 64
    assert payload["messages"][0]["content"] == "hi"


def test_anthropic_payload_hoists_system_and_forces_max_tokens():
    payload = translate.build_anthropic_request(
        [{"role": "system", "content": "be terse"}, {"role": "user", "content": "hi"}], "m"
    )
    assert payload["system"] == "be terse"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["max_tokens"] == 4096, "Anthropic requires max_tokens"


def test_anthropic_payload_folds_content_parts_into_text():
    payload = translate.build_anthropic_request(
        [{"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "image", "source": {}}, {"type": "text", "text": "b"}]}],
        "m",
    )
    assert payload["messages"][0]["content"] == "ab"


def test_gemini_payload_maps_roles_and_system():
    payload = translate.build_gemini_request(
        [{"role": "system", "content": "rules"}, {"role": "assistant", "content": "prior"}, {"role": "user", "content": "hi"}]
    )
    assert payload["systemInstruction"] == {"parts": [{"text": "rules"}]}
    assert payload["contents"] == [
        {"role": "model", "parts": [{"text": "prior"}]},
        {"role": "user", "parts": [{"text": "hi"}]},
    ]


def test_responses_payload_uses_input_and_instructions():
    payload = translate.build_responses_request(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}], "m"
    )
    assert payload["instructions"] == "sys"
    assert payload["input"] == [{"role": "user", "content": "hi"}]
    assert payload["max_output_tokens"] >= 4096


def test_empty_message_lists_still_produce_a_valid_request():
    assert translate.build_anthropic_request([], "m")["messages"]
    assert translate.build_gemini_request([])["contents"]
    assert translate.build_responses_request([], "m")["input"]


# ── delta extraction ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "delta,expected",
    [
        ({"content": "hi"}, "hi"),
        ({"reasoning_content": "thinking"}, "thinking"),
        ({"reasoning": "thinking"}, "thinking"),
        ({"content": ""}, None),
        ({}, None),
    ],
)
def test_extract_delta_text(delta, expected):
    assert translate.extract_delta_text(delta) == expected


# ── event parsing ────────────────────────────────────────────────────────────

def test_anthropic_events_translate_to_text_and_finish():
    text, done = translate.parse_anthropic_event(
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}}
    )
    assert (text, done) == ("hello", False)
    assert translate.parse_anthropic_event({"type": "message_stop"}) == (None, True)
    assert translate.parse_anthropic_event({"type": "ping"}) == (None, False)


def test_gemini_events_translate_to_text_and_finish():
    text, done = translate.parse_gemini_event(
        {"candidates": [{"content": {"parts": [{"text": "he"}, {"text": "llo"}]}}]}
    )
    assert text == "hello" and done is False
    _, done = translate.parse_gemini_event({"candidates": [{"finishReason": "STOP"}]})
    assert done is True


def test_responses_events_translate_to_text_and_finish():
    assert translate.parse_responses_event({"type": "response.output_text.delta", "delta": "x"}) == ("x", False)
    assert translate.parse_responses_event({"type": "response.completed"}) == (None, True)
    text, done = translate.parse_responses_event({"type": "error", "error": {"message": "boom"}})
    assert "boom" in text and done is True


def test_parse_sse_data_ignores_sentinels_and_junk():
    assert translate.parse_sse_data("data: [DONE]") is None
    assert translate.parse_sse_data("") is None
    assert translate.parse_sse_data("data: not json") is None
    assert translate.parse_sse_data('data: {"a": 1}') == {"a": 1}


# ── model catalogs ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"data": [{"id": "a"}, {"id": "b"}]}, ["a", "b"]),
        ([{"id": "a"}], ["a"]),
        ({"models": [{"name": "a"}]}, ["a"]),
        ({"models": [{"id": "a"}, {"id": "a"}]}, ["a"]),
        ({"data": {"a": {"id": "a"}}}, ["a"]),
        ({"data": ["a", "b"]}, ["a", "b"]),
        ({"unexpected": 1}, []),
        ("nonsense", []),
        (None, []),
    ],
)
def test_parse_models_payload_tolerates_every_shape(payload, expected):
    assert translate.parse_models_payload(payload) == expected


# ── streaming, end to end through the node ───────────────────────────────────

class FakeStream:
    def __init__(self, lines, status_code=200):
        self._lines = lines
        self.status_code = status_code

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeClient:
    def __init__(self, response):
        self._response = response
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self._response


def stream_with(node, lines, status_code=200):
    client = FakeClient(FakeStream(lines, status_code))
    with patch("httpx.AsyncClient", lambda **kwargs: client):
        chunks = collect(node)
    return chunks, client


def test_openai_node_passes_sse_through_and_posts_to_the_right_url():
    node = NodeProvider("n", {"base_url": "https://api.x.com/v1", "api_key": "k"})
    lines = ['data: {"choices":[{"delta":{"content":"hi"}}]}', "", "data: [DONE]"]
    chunks, client = stream_with(node, lines)

    method, url, kwargs = client.requests[0]
    assert method == "POST"
    assert url == "https://api.x.com/v1/chat/completions"
    assert kwargs["headers"]["authorization"] == "Bearer k"
    assert kwargs["json"]["stream"] is True
    assert any("hi" in c for c in chunks)
    assert chunks[-1].strip() == "data: [DONE]"


def test_anthropic_node_translates_events_to_openai_chunks():
    node = NodeProvider("a", {"api_type": "anthropic", "base_url": "https://api.anthropic.com/v1", "api_key": "k"})
    lines = [
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hello"}}',
        'data: {"type":"message_stop"}',
    ]
    chunks, client = stream_with(node, lines)

    assert client.requests[0][1] == "https://api.anthropic.com/v1/messages"
    joined = "".join(chunks)
    assert "hello" in joined
    assert '"finish_reason": "stop"' in joined or '"finish_reason":"stop"' in joined
    assert joined.rstrip().endswith("data: [DONE]")


def test_gemini_node_translates_candidates_to_openai_chunks():
    node = NodeProvider("g", {"api_type": "gemini", "base_url": "https://g.example/v1beta"})
    lines = [
        'data: {"candidates":[{"content":{"parts":[{"text":"budget"}]}}]}',
        'data: {"candidates":[{"content":{"parts":[{"text":" ok"}]},"finishReason":"STOP"}]}',
    ]
    chunks, client = stream_with(node, lines)

    assert "models/m:streamGenerateContent" in client.requests[0][1]
    joined = "".join(chunks)
    assert "budget" in joined and " ok" in joined


def test_responses_node_translates_output_text_deltas():
    node = NodeProvider("r", {"api_type": "openai-responses", "base_url": "https://api.x.com/v1"})
    lines = [
        'data: {"type":"response.output_text.delta","delta":"par"}' 
        ,
        'data: {"type":"response.completed"}',
    ]
    chunks, _ = stream_with(node, lines)
    assert "par" in "".join(chunks)


def test_upstream_error_is_reported_with_its_status_and_body():
    node = NodeProvider("n", {"base_url": "https://api.x.com/v1", "api_key": "k"})
    chunks, _ = stream_with(node, [], status_code=404)
    joined = "".join(chunks)
    assert "HTTP 404" in joined
    assert joined.rstrip().endswith("data: [DONE]")


def test_a_node_without_base_url_explains_itself_instead_of_crashing():
    node = NodeProvider("n", {"api_key": "k"})
    chunks = collect(node)
    assert "no base_url" in "".join(chunks)


def test_describe_leaks_no_secret():
    node = NodeProvider("n", {"base_url": "https://api.x.com/v1", "api_key": "super-secret", "custom_headers": {"x-a": "b"}})
    described = json.dumps(node.describe())
    assert "super-secret" not in described
    assert described.count("true") >= 1
    assert node.describe()["has_credential"] is True


# ── router integration ───────────────────────────────────────────────────────

def node_config(**overrides):
    config = {
        "kind": "node",
        "enabled": True,
        "prefix": "mn",
        "base_url": "https://api.example.com/v1",
        "api_key": "k",
        "models": ["model-x"],
    }
    config.update(overrides)
    return config


def test_router_builds_a_node_provider_from_config():
    config = KiteConfig()
    config.providers["mynode"] = node_config()
    router = ProviderRouter(config=config)

    assert isinstance(router.providers["mynode"], NodeProvider)
    assert router.prefix_map["mn"] == "mynode"


def test_node_prefix_routes_to_the_node():
    config = KiteConfig()
    config.providers["mynode"] = node_config()
    router = ProviderRouter(config=config)

    provider, model = router.parse_model_and_provider("mn/model-x")
    assert provider == "mynode"
    assert model == "model-x"


def test_a_node_id_itself_works_as_a_prefix():
    config = KiteConfig()
    config.providers["mynode"] = node_config()
    router = ProviderRouter(config=config)

    provider, model = router.parse_model_and_provider("mynode/model-x")
    assert provider == "mynode" and model == "model-x"


def test_a_node_prefix_cannot_shadow_a_builtin_alias():
    config = KiteConfig()
    config.providers["mynode"] = node_config(prefix="cc")  # 'cc' already means claude
    router = ProviderRouter(config=config)

    assert router.prefix_map["cc"] == "claude", "a node must not hijack an existing alias"
    provider, _ = router.parse_model_and_provider("cc/claude-opus-4-7")
    assert provider == "claude"


def test_node_models_are_offered_by_the_router():
    config = KiteConfig()
    config.providers["mynode"] = node_config(models=["model-x", "model-y"])
    router = ProviderRouter(config=config)

    ids = [m["id"] for m in router.get_all_models()]
    assert any("model-x" in i for i in ids)


def test_the_legacy_custom_path_still_works_for_unknown_ids():
    """Existing imports must keep behaving exactly as before."""
    from kiterouter.providers.custom import CustomProvider

    config = KiteConfig()
    config.providers["groq"] = {"enabled": True, "api_key": "gsk_x"}
    router = ProviderRouter(config=config)

    provider = router.providers["groq"]
    assert isinstance(provider, CustomProvider)
    assert provider.endpoint == "https://api.groq.com/openai/v1/chat/completions"
    assert "groq/llama-3.3-70b-versatile" in provider.supported_models

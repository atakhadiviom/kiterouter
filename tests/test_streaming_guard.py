"""Tests for the upstream stream stall guard."""
from __future__ import annotations

import asyncio

import pytest

from kiterouter.providers.streaming import UpstreamStalled, iter_upstream_lines


class FakeStream:
    def __init__(self, lines, delay=0.0):
        self._lines = lines
        self._delay = delay

    async def aiter_lines(self):
        for line in self._lines:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield line


def collect(lines, delay=0.0, **kwargs):
    async def _run():
        out = []
        async for line in iter_upstream_lines(FakeStream(lines, delay), **kwargs):
            out.append(line)
        return out

    return asyncio.run(_run())


def test_normal_lines_pass_through_unchanged():
    lines = ['data: {"a":1}', "", "data: [DONE]"]
    assert collect(lines) == ['data: {"a":1}', "data: [DONE]"]


def test_blank_lines_are_dropped():
    assert collect(["", "   ", 'data: x']) == ["data: x"]


def test_keep_alives_alone_are_reported_as_a_stall():
    """The real failure: an upstream holding the connection with comments only."""
    lines = [": keep-alive"] * 50

    async def _run():
        out = []
        async for line in iter_upstream_lines(
            FakeStream(lines, delay=0.02), first_content_timeout=0.05, idle_timeout=5
        ):
            out.append(line)
        return out

    with pytest.raises(UpstreamStalled) as err:
        asyncio.run(_run())
    assert "keep-alives" in str(err.value)


def test_content_resets_the_stall_window():
    """Keep-alives after real content are fine — the stream is alive."""
    lines = ['data: {"choices":[{"delta":{"content":"a"}}]}'] + [": keep-alive"] * 20

    async def _run():
        out = []
        async for line in iter_upstream_lines(
            FakeStream(lines, delay=0.01), first_content_timeout=0.05, idle_timeout=5
        ):
            out.append(line)
        return out

    out = asyncio.run(_run())
    assert len(out) == 1


def test_a_silent_stream_times_out_on_idle():
    class Silent:
        async def aiter_lines(self):
            await asyncio.sleep(10)
            yield "never"

    async def _run():
        async for _ in iter_upstream_lines(Silent(), idle_timeout=0.05):
            pass

    with pytest.raises(UpstreamStalled) as err:
        asyncio.run(_run())
    assert "no data" in str(err.value)


def test_a_finished_stream_ends_cleanly():
    assert collect(["data: x"]) == ["data: x"]

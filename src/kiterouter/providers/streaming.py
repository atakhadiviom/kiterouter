"""Guards for reading upstream SSE streams.

Some upstreams accept a request, then hold the connection open sending only SSE
comments (": keep-alive") and never produce content. A per-chunk read timeout
never fires in that case, because comments *are* data — so the client waits
forever. These helpers watch for actual progress and give up honestly instead.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncGenerator

FIRST_CONTENT_TIMEOUT_SECONDS = 60.0
IDLE_TIMEOUT_SECONDS = 120.0


class UpstreamStalled(Exception):
    """The upstream stopped making progress."""


async def iter_upstream_lines(
    response: Any,
    *,
    first_content_timeout: float = FIRST_CONTENT_TIMEOUT_SECONDS,
    idle_timeout: float = IDLE_TIMEOUT_SECONDS,
) -> AsyncGenerator[str, None]:
    """Yield upstream lines, raising UpstreamStalled when the stream goes quiet.

    SSE comments and blank lines do not count as progress, so a stream that only
    emits keep-alives is detected on its next keep-alive rather than never.
    """
    iterator = response.aiter_lines().__aiter__()
    started = time.time()
    last_progress = started
    seen_content = False

    while True:
        try:
            line = await asyncio.wait_for(iterator.__anext__(), timeout=idle_timeout)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            raise UpstreamStalled(
                f"no data for {idle_timeout:.0f}s"
            )

        stripped = line.strip()
        if not stripped or stripped.startswith(":"):
            # Comment or keep-alive. If nothing real has arrived, this is a stall.
            quiet_for = time.time() - last_progress
            if not seen_content and quiet_for > first_content_timeout:
                raise UpstreamStalled(
                    f"no content after {quiet_for:.0f}s (only keep-alives)"
                )
            continue

        seen_content = True
        last_progress = time.time()
        yield line

"""Read owner emergency pause through the private governed MCP service.

The voice worker must not mount the governance volume: it contains audit,
approval and connection state. The existing sidecar is the sole trusted reader.
A timeout, unavailable sidecar, unexpected HTTP status or malformed response
fails closed. This is admission checking, NOT distributed effect cancellation.
"""
from __future__ import annotations

import asyncio
import http.client
import logging
from collections.abc import Callable

from pause import HOLDING

HOST = "governed-tools"
PORT = 9120
PATH = "/internal/voice-pause"
TIMEOUT_SECONDS = 2
LOGGER = logging.getLogger("model-garden.voice-pause")


def _check() -> bool:
    """Only an explicit HTTP 200 from the private sidecar permits voice work."""
    connection = http.client.HTTPConnection(HOST, PORT, timeout=TIMEOUT_SECONDS)
    try:
        connection.request("GET", PATH)
        response = connection.getresponse()
        return response.status != 200
    except (OSError, TimeoutError, http.client.HTTPException):
        return True
    finally:
        connection.close()


async def paused() -> bool:
    """Bounded private check without blocking LiveKit's media event loop."""
    return await asyncio.to_thread(_check)


async def stop_session_on_pause(session, *, interval_seconds: float = 1.0,
                                on_pause: Callable[[], None] | None = None) -> None:
    """Close an already admitted voice session when owner pause becomes visible.

    This is a best-effort local fence, not synchronous cancellation of audio,
    remote SIP transfers or a provider request already accepted. Errors reading
    the private sidecar are treated as paused. Cancellation on job shutdown must
    propagate without closing a normally completed session.
    """
    while True:
        try:
            should_stop = await paused()
        except Exception as exc:
            LOGGER.error("voice pause check failed (%s); closing session", type(exc).__name__)
            should_stop = True
        if should_stop:
            if on_pause is not None:
                try:
                    on_pause()
                except Exception as exc:
                    LOGGER.error("voice pause evidence suppression failed (%s)", type(exc).__name__)
            try:
                await session.aclose()
            except Exception as exc:
                # A failed SDK close is not a provider-side cancellation receipt.
                LOGGER.error("voice session pause close failed (%s)", type(exc).__name__)
            return
        await asyncio.sleep(interval_seconds)

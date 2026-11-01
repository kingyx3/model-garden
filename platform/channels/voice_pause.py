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


async def stop_session_on_pause(session, *, interval_seconds: float = 1.0) -> None:
    """Close an already admitted voice session when owner pause becomes visible.

    This is a best-effort local fence, not synchronous cancellation of audio,
    remote SIP transfers or a provider request already accepted. Errors reading
    the private sidecar are treated as paused. Cancellation on job shutdown must
    propagate without closing a normally completed session.
    """
    # Once a pause or untrusted status is observed, it cannot be undone by
    # a later active response. A failed SDK close is not a successful stop:
    # keep trying until the SDK acknowledges closure or job shutdown cancels
    # this watcher. Never log exception bodies (which may contain call data).
    stop_requested = False
    close_failure_logged = False
    while True:
        if not stop_requested:
            try:
                stop_requested = await paused()
            except Exception as exc:
                LOGGER.error("voice pause check failed (%s); closing session", type(exc).__name__)
                stop_requested = True
        if stop_requested:
            try:
                await session.aclose()
                return
            except Exception as exc:
                if not close_failure_logged:
                    LOGGER.error("voice session pause close failed (%s); retrying until shutdown",
                                 type(exc).__name__)
                    close_failure_logged = True
        await asyncio.sleep(interval_seconds)

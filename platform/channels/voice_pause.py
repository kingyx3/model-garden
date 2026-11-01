"""Read owner emergency pause through the private governed MCP service.

The voice worker must not mount the governance volume: it contains audit,
approval and connection state. The existing sidecar is the sole trusted reader.
A timeout, unavailable sidecar, unexpected HTTP status or malformed response
fails closed. This is admission checking, NOT distributed effect cancellation.
"""
from __future__ import annotations

import http.client

from pause import HOLDING

HOST = "governed-tools"
PORT = 9120
PATH = "/internal/voice-pause"
TIMEOUT_SECONDS = 2


def paused() -> bool:
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

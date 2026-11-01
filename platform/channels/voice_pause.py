"""Read the owner emergency pause from the shared governance volume in LiveKit.

The voice worker mounts the same per-client governance volume read-only. It
cannot change owner state or audit receipts. An absent/untrusted mount fails
closed; only a genuine first-boot empty governance directory is unpaused.
This is an admission/side-effect check, NOT a distributed cancellation fence.
"""
from __future__ import annotations

import pathlib

from pause import HOLDING, PauseStore

PAUSE_ROOT = pathlib.Path("/opt/governance/.modelgarden")


def paused() -> bool:
    """Read current owner state for voice admission and fallback actions."""
    return PauseStore(PAUSE_ROOT).is_paused()

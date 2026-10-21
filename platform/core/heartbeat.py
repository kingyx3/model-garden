"""Payload-free heartbeat shared by the worker and the channel gateway (#231).

A component writes ``{"at", "failed", "persistentFailures"}`` after each pass of its own loop, so a wedged process is noticed even
while its port still answers. Only fixed job names and a time are stored, never an error message or user data. ``check`` classifies
the file as healthy, degraded (a job failed on three passes running) or unavailable (missing, malformed, stale or from the future).
"""
from __future__ import annotations

import json
import math
import os
import pathlib
import re
from collections.abc import Mapping
from typing import Any

PERSISTENT_AFTER = 3
NAME = re.compile(r"[a-z][a-z0-9-]{0,39}")


class Beat:
    """Keeps the consecutive-failure streaks between passes and writes the heartbeat file."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = pathlib.Path(path)
        self._streaks: dict[str, int] = {}

    def write(self, now: int, ran: Mapping[str, bool]) -> dict[str, Any]:
        """``ran`` maps a job name to whether it FAILED on this pass; jobs not named keep their streak."""
        for name, failed in ran.items():
            self._streaks[name] = self._streaks.get(name, 0) + 1 if failed else 0
        document = {"at": now, "failed": sorted(name for name, failed in ran.items() if failed),
                    "persistentFailures": sorted(name for name, count in self._streaks.items() if count >= PERSISTENT_AFTER)}
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(document, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        return document


def check(heartbeat: pathlib.Path, *, now: float, max_age: int = 300) -> dict[str, Any]:
    unavailable = {"status": "unavailable", "reason": "missing-or-invalid", "failedJobs": [], "persistentFailures": []}
    try:
        if heartbeat.is_symlink() or heartbeat.stat().st_size > 4096:
            return unavailable
        document = json.loads(heartbeat.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return unavailable
    if not isinstance(document, dict):
        return unavailable
    at = document.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
        return {**unavailable, "reason": "invalid"}
    failed = document.get("failed", [])
    persistent = document.get("persistentFailures", [])
    if not isinstance(failed, list) or not isinstance(persistent, list) or not all(isinstance(x, str) and NAME.fullmatch(x) for x in failed + persistent):
        return {**unavailable, "reason": "invalid"}
    age = now - at
    if age < -30:
        return {**unavailable, "reason": "future"}
    if age > max_age:
        return {**unavailable, "reason": "stale"}
    return {"status": "degraded" if persistent else "healthy", "reason": "persistent-job-failures" if persistent else "recent",
            "failedJobs": sorted(set(failed)), "persistentFailures": sorted(set(persistent))}

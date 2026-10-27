"""Payload-free heartbeat shared by the worker and the channel gateway (#231).

A component writes ``{"at", "failed", "persistentFailures"}`` after each pass of its own loop, so a wedged process is noticed even
while its port still answers. Only fixed job names and a time are stored, never an error message or user data. ``check`` classifies
the file as healthy, degraded (a job failed on three passes running) or unavailable (missing, malformed, stale or from the future).

The heartbeat is a health *claim*, so reads and writes must resist filesystem link/replacement attacks: otherwise an attacker
sharing a writable volume could forge "healthy" while a component is down.
"""
from __future__ import annotations

import json
import math
import os
import pathlib
import re
import secrets
import stat
from collections.abc import Mapping
from typing import Any

PERSISTENT_AFTER = 3
NAME = re.compile(r"[a-z][a-z0-9-]{0,39}")
MAX_BEAT_BYTES = 4096


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
        _write_pinned(self.path, document)
        return document


def _write_pinned(path: pathlib.Path, document: Mapping[str, Any]) -> None:
    """Durably publish a small payload-free health/state record without following linked paths."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    temporary = None
    try:
        pinned = os.fstat(directory)
        # Use an unpredictable, exclusive name *inside the pinned directory*.
        # The old predictable .tmp + O_TRUNC could truncate a hardlinked victim.
        for _ in range(5):
            candidate = ".heartbeat-" + secrets.token_hex(12)
            try:
                descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                     0o600, dir_fd=directory)
                temporary = candidate
                break
            except FileExistsError:
                continue
        else:
            raise OSError("cannot allocate heartbeat temporary file")
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(document, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        current = os.stat(path.parent, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise OSError("heartbeat directory replaced during write")
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
        os.close(directory)



def _read_pinned(heartbeat: pathlib.Path) -> dict[str, Any] | None:
    """Read a bounded regular inode through a no-follow directory fd.

    Reject linked, writable-by-others and replaced files; never block on a FIFO
    or follow a symlink to an unrelated tenant's data.
    """
    directory = os.open(heartbeat.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        pinned_dir = os.fstat(directory)
        descriptor = os.open(heartbeat.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_BEAT_BYTES
                    or stat.S_IMODE(before.st_mode) & 0o022):
                return None
            raw = os.read(descriptor, MAX_BEAT_BYTES + 1)
            after = os.fstat(descriptor)
            current = os.stat(heartbeat.name, dir_fd=directory, follow_symlinks=False)
            parent = os.stat(heartbeat.parent, follow_symlinks=False)
            if (len(raw) > MAX_BEAT_BYTES
                    or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                       != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
                    or (pinned_dir.st_dev, pinned_dir.st_ino) != (parent.st_dev, parent.st_ino)):
                return None
            document = json.loads(raw.decode("utf-8"))
            return document if isinstance(document, dict) else None
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def check(heartbeat: pathlib.Path, *, now: float, max_age: int = 300) -> dict[str, Any]:
    unavailable = {"status": "unavailable", "reason": "missing-or-invalid", "failedJobs": [], "persistentFailures": []}
    try:
        document = _read_pinned(pathlib.Path(heartbeat))
    except (OSError, ValueError, TypeError, UnicodeError):
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

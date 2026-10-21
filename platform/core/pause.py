"""Owner-controlled emergency pause for one AI coworker (#232).

One small state file on the governance volume. While it says paused, the shared governed boundary denies every Tool that is not a
read, the worker starts no scheduled, event, mailbox or CRM work, and the gateway answers with a holding message. A file that exists
but cannot be read counts as paused: the control fails safe, never open.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import tempfile
from typing import Any

FILE = "pause.json"
MAX_REASON = 300
CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
HOLDING = "I am paused by my owner for now, so I cannot act on this. A person has been told and will pick it up."


def clean(reason: Any) -> str:
    return CONTROL.sub(" ", str(reason or "")).strip()[:MAX_REASON]


class PauseStore:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = pathlib.Path(root)
        self.path = self.root / FILE

    def status(self) -> dict[str, Any]:
        """``{"paused": bool, "by", "at", "reason"}``; an unreadable or malformed file reports paused."""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {"paused": False}
        except OSError:
            return {"paused": True, "by": None, "at": None, "reason": "pause state unreadable (failing safe)"}
        try:
            document = json.loads(raw)
            if not isinstance(document, dict) or not isinstance(document.get("paused"), bool):
                raise ValueError("shape")
        except ValueError:
            return {"paused": True, "by": None, "at": None, "reason": "pause state unreadable (failing safe)"}
        if not document["paused"]:
            return {"paused": False, "by": document.get("by"), "at": document.get("at")}
        found = {"paused": True, "by": document.get("by"), "at": document.get("at"), "reason": clean(document.get("reason"))}
        for key in ("cause", "window"):  # set only by a system-initiated pause (the monthly spend cap); an owner's pause has neither
            if isinstance(document.get(key), str):
                found[key] = clean(document[key])[:40]
        return found

    def is_paused(self) -> bool:
        return bool(self.status()["paused"])

    def _write(self, document: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(dir=self.root, prefix=".pause-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(document, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def pause(self, by: str, reason: str, now: int, *, cause: str | None = None, window: str | None = None) -> dict[str, Any]:
        by = clean(by)
        if not by:
            raise ValueError("who is pausing is required")
        extra = {key: value for key, value in (("cause", cause), ("window", window)) if value}
        self._write({"paused": True, "by": by, "at": int(now), "reason": clean(reason), **extra})
        return self.status()

    def resume(self, by: str, now: int) -> dict[str, Any]:
        by = clean(by)
        if not by:
            raise ValueError("who is resuming is required")
        self._write({"paused": False, "by": by, "at": int(now)})
        return self.status()

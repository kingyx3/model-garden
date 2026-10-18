"""A small, bounded outbox that carries digest-only events from one runtime process to the worker (#99).

The channel gateway sees each chat message but has no scheduler, no coworker and no owner permissions; the runtime worker has all
three but never sees the message. An event-triggered task ("when a new chat message arrives") needs the first to tell the second
*that* something arrived, and nothing more. This is that hand-over, on the governance volume both already share:

- **Digest only.** An entry is ``{"kind", "ref", "at"}``: the event kind, a 32-character hex digest of the channel, conversation and
  message ids (computed by the writer), and the time. It never holds a sender, an address, a conversation id or any text, so the
  worker, the trigger runner and the ledger can see it without seeing a conversation.
- **Append-only and bounded.** Writers append one line under an exclusive lock; once the file reaches ``MAX_BYTES`` further events
  are refused (``append`` returns ``False``) rather than letting an absent worker fill the volume. The reader keeps its own byte
  offset and is given only whole lines, so a half-written line is never read; when it has read everything and the file has grown
  past ``COMPACT_BYTES`` the reader empties it under the same lock.
- **Closed vocabulary.** Only the kinds listed in ``KINDS`` can be written.

It is not a queue with delivery guarantees beyond that: an event the worker cannot act on (no coworker configured) is simply left
until it can.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pathlib
import re
from collections.abc import Iterator

KINDS = ("chat.message",)
MAX_BYTES = 1_048_576
COMPACT_BYTES = 65_536
MAX_BATCH = 100
_REF = re.compile(r"^[0-9a-f]{32}$")


class EventOutbox:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = pathlib.Path(root)
        self.path = self.root / "events.jsonl"

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            os.chmod(self.root, 0o700)
        descriptor = os.open(self.root / ".events.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _size(self) -> int:
        try:
            return self.path.stat().st_size
        except FileNotFoundError:
            return 0

    def append(self, kind: str, reference: str, *, at: int) -> bool:
        """Add one event; ``False`` when the outbox is full."""
        if kind not in KINDS:
            raise ValueError("event kind must be one of " + ", ".join(KINDS))
        if not isinstance(reference, str) or not _REF.fullmatch(reference):
            raise ValueError("an event carries a 32-character hex digest, never an identifier")
        if not isinstance(at, int) or isinstance(at, bool) or at < 0:
            raise ValueError("an event carries the time it was seen")
        line = json.dumps({"kind": kind, "ref": reference, "at": at}, sort_keys=True) + "\n"
        with self._locked():
            if self._size() + len(line) > MAX_BYTES:
                return False
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
        return True

    def read(self, offset: int, *, limit: int = MAX_BATCH) -> tuple[list[dict[str, object]], int]:
        """Whole events after ``offset`` (at most ``limit``) and the offset after the last line read; a shorter file restarts at 0."""
        size = self._size()
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0 or offset > size:
            offset = 0
        events: list[dict[str, object]] = []
        if size == 0:
            return events, 0
        with open(self.path, "rb") as handle:
            handle.seek(offset)
            for raw in handle:
                if not raw.endswith(b"\n"):
                    break  # a line still being written
                offset += len(raw)
                try:
                    item = json.loads(raw)
                except ValueError:
                    continue
                if (isinstance(item, dict) and item.get("kind") in KINDS and isinstance(item.get("ref"), str)
                        and _REF.fullmatch(item["ref"]) and isinstance(item.get("at"), int)):
                    events.append({"kind": item["kind"], "ref": item["ref"], "at": item["at"]})
                if len(events) >= limit:
                    break
        return events, offset

    def compact(self, offset: int) -> int:
        """Empty the outbox when the reader has consumed all of a file grown past ``COMPACT_BYTES``; returns the reader's new offset."""
        with self._locked():
            size = self._size()
            if size >= COMPACT_BYTES and offset >= size:
                self.path.write_bytes(b"")
                return 0
        return offset

"""A bounded, private digest-only event handoff from the channel gateway to the worker.

The outbox is not an external delivery guarantee. An unreadable, truncated or
corrupted local queue must nevertheless never be mistaken for an empty queue:
otherwise an autonomous trigger can silently disappear or a forged record can
run after a pause. All participating local processes share one checked lock.
Only digest, kind and timestamp cross the channel/worker boundary (#232/#99).
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pathlib
import re
import stat
from collections.abc import Iterator

KINDS = ("chat.message",)
MAX_BYTES = 1_048_576
COMPACT_BYTES = 65_536
MAX_BATCH = 100
_REF = re.compile(r"^[0-9a-f]{32}$")
FILE = "events.jsonl"
LOCK = ".events.lock"
MARKER = ".events.initialized"
WITNESS = b"initialized\n"


def _private(info: os.stat_result) -> bool:
    return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and info.st_uid == os.geteuid() and not info.st_mode & 0o077)


def _open_private(directory: int, name: str, flags: int) -> int | None:
    """Open a pinned, single-linked, owner-private regular inode, never a link/FIFO."""
    try:
        before = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not _private(before):
        raise ValueError("event outbox state is untrusted; operator repair required")
    descriptor = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        opened = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (not _private(opened)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)):
            raise ValueError("event outbox state changed; operator repair required")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class EventOutbox:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = pathlib.Path(root)
        self.path = self.root / FILE

    @contextlib.contextmanager
    def _locked(self) -> Iterator[int]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        before = self.root.lstat()
        if (not stat.S_ISDIR(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_mode & 0o077):
            raise ValueError("event outbox directory is untrusted; operator repair required")
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            opened = os.fstat(directory)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError("event outbox directory changed")
            descriptor = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 0o600, dir_fd=directory)
            try:
                lock = os.fstat(descriptor)
                current = os.stat(LOCK, dir_fd=directory, follow_symlinks=False)
                if (not _private(lock) or (lock.st_dev, lock.st_ino) != (current.st_dev, current.st_ino)):
                    raise ValueError("event outbox lock is untrusted; operator repair required")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield directory
                current_dir = self.root.lstat()
                if (current_dir.st_dev, current_dir.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ValueError("event outbox directory changed")
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
        finally:
            os.close(directory)

    def _marker(self, directory: int) -> bool:
        handle = _open_private(directory, MARKER, os.O_RDONLY)
        if handle is None:
            return False
        with os.fdopen(handle, "rb") as stream:
            if stream.read(len(WITNESS) + 1) != WITNESS:
                raise ValueError("event outbox initialization witness is untrusted")
        return True

    @staticmethod
    def _write_marker(directory: int) -> None:
        handle = os.open(MARKER, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
        with os.fdopen(handle, "wb") as stream:
            stream.write(WITNESS)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)

    def _read_all(self, directory: int) -> bytes:
        initialized = self._marker(directory)
        handle = _open_private(directory, FILE, os.O_RDONLY)
        if handle is None:
            if initialized:
                raise ValueError("initialized event outbox is missing; operator repair required")
            return b""
        try:
            before = os.fstat(handle)
            with os.fdopen(handle, "rb", closefd=False) as stream:
                raw = stream.read(MAX_BYTES + 1)
            current = os.stat(FILE, dir_fd=directory, follow_symlinks=False)
            after = os.fstat(handle)
            if (len(raw) > MAX_BYTES or after.st_size != len(raw)
                    or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
                    or before.st_mtime_ns != after.st_mtime_ns):
                raise ValueError("event outbox is oversized or changed; operator repair required")
            return raw
        finally:
            os.close(handle)

    @staticmethod
    def _decode(raw: bytes) -> list[tuple[dict[str, object], int]]:
        """Validate the *entire* snapshot before returning any event or advancing an offset."""
        events: list[tuple[dict[str, object], int]] = []
        position = 0
        for line in raw.splitlines(keepends=True):
            if not line.endswith(b"\n"):
                raise ValueError("incomplete event outbox record; operator repair required")
            position += len(line)
            try:
                item = json.loads(line)
            except (UnicodeError, ValueError):
                raise ValueError("corrupt event outbox record; operator repair required") from None
            if (not isinstance(item, dict) or set(item) != {"kind", "ref", "at"}
                    or item["kind"] not in KINDS or not isinstance(item["ref"], str)
                    or _REF.fullmatch(item["ref"]) is None
                    or type(item["at"]) is not int or item["at"] < 0):
                raise ValueError("corrupt event outbox record; operator repair required")
            events.append((item, position))
        return events

    def append(self, kind: str, reference: str, *, at: int) -> bool:
        """Append one validated event; False only for a full, otherwise trusted queue."""
        if kind not in KINDS:
            raise ValueError("event kind must be one of " + ", ".join(KINDS))
        if not isinstance(reference, str) or _REF.fullmatch(reference) is None:
            raise ValueError("an event carries a 32-character hex digest, never an identifier")
        if type(at) is not int or at < 0:
            raise ValueError("an event carries the time it was seen")
        line = (json.dumps({"kind": kind, "ref": reference, "at": at}, sort_keys=True) + "\n").encode()
        with self._locked() as directory:
            initialized = self._marker(directory)
            handle = _open_private(directory, FILE, os.O_WRONLY | os.O_APPEND)
            if handle is None and initialized:
                raise ValueError("initialized event outbox is missing; operator repair required")
            if handle is None:
                if not initialized:
                    self._write_marker(directory)
                handle = os.open(FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            elif not initialized:
                # A legacy valid queue is enrolled on its first post-upgrade append.
                self._write_marker(directory)
            try:
                before = os.fstat(handle)
                if before.st_size + len(line) > MAX_BYTES:
                    return False
                if os.write(handle, line) != len(line):
                    raise OSError("event outbox append was incomplete")
                os.fsync(handle)
                after = os.stat(FILE, dir_fd=directory, follow_symlinks=False)
                if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
                    raise ValueError("event outbox changed during append")
            finally:
                os.close(handle)
        return True

    def read(self, offset: int, *, limit: int = MAX_BATCH) -> tuple[list[dict[str, object]], int]:
        """Read validated complete records after a trusted line boundary; no silent skips."""
        if type(offset) is not int or offset < 0:
            raise ValueError("event outbox offset is invalid")
        if type(limit) is not int or not 1 <= limit <= MAX_BATCH:
            raise ValueError("event outbox limit is invalid")
        with self._locked() as directory:
            raw = self._read_all(directory)
            entries = self._decode(raw)
            boundaries = {0, *(end for _, end in entries)}
            if offset not in boundaries:
                raise ValueError("event outbox offset is stale or not at a record boundary")
            selected = [(item, end) for item, end in entries if end > offset][:limit]
            return ([item for item, _ in selected], selected[-1][1] if selected else offset)

    def compact(self, offset: int) -> int:
        """Truncate only a fully consumed, validated queue while holding the writer lock."""
        if type(offset) is not int or offset < 0:
            raise ValueError("event outbox offset is invalid")
        with self._locked() as directory:
            raw = self._read_all(directory)
            entries = self._decode(raw)
            size = len(raw)
            if offset > size:
                raise ValueError("event outbox offset is stale")
            if size >= COMPACT_BYTES and offset == size:
                handle = _open_private(directory, FILE, os.O_RDWR)
                assert handle is not None
                try:
                    os.ftruncate(handle, 0)
                    os.fsync(handle)
                finally:
                    os.close(handle)
                return 0
            return offset

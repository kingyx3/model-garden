"""Owner-controlled emergency pause for one AI coworker (#232).

One small state file on the governance volume. While it says paused, the shared governed boundary denies every Tool that is not a
read, the worker starts no scheduled, event, mailbox or CRM work, and the gateway answers with a holding message. A file that exists
but cannot be read counts as paused: the control fails safe, never open.
"""
from __future__ import annotations

import errno
import fcntl
import json
import math
import os
import pathlib
import re
import secrets
import stat
import time
from contextlib import contextmanager
from typing import Any, Iterator

FILE = "pause.json"
LOCK_FILE = ".pause.lock"
MARKER = ".pause.initialized"  # Durable first-write witness; missing state after a write is unsafe.
MAX_STATE_BYTES = 8192
MAX_REASON = 300
MAX_OWNER_LOCK_WAIT_SECONDS = 30.0
DEFAULT_OWNER_LOCK_WAIT_SECONDS = 5.0
CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
HOLDING = "I am paused by my owner for now, so I cannot act on this. Please contact a person if this is urgent."


def clean(reason: Any) -> str:
    return CONTROL.sub(" ", str(reason or "")).strip()[:MAX_REASON]


def _private_regular(info: os.stat_result) -> bool:
    """Require a single, private, current-UID-owned regular state/lock inode."""
    return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and info.st_uid == os.geteuid() and not info.st_mode & 0o077)


def _fail_closed() -> dict[str, Any]:
    return {"paused": True, "by": None, "at": None, "reason": "pause state unreadable (failing safe)"}


class PauseStore:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = pathlib.Path(root)
        self.path = self.root / FILE

    def _same_root(self, expected: os.stat_result) -> bool:
        """An absent or replaced governance mount cannot authorize an unpause."""
        try:
            current = self.root.lstat()
        except OSError:
            return False
        return (stat.S_ISDIR(current.st_mode) and not current.st_mode & 0o022
                and (current.st_dev, current.st_ino) ==
                (expected.st_dev, expected.st_ino))

    def _marker_state(self) -> bool | None:
        """True for a trusted first-write witness, False if absent, None if untrusted."""
        try:
            info = (self.root / MARKER).lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return None
        return True if _private_regular(info) else None

    def status(self) -> dict[str, Any]:
        """Read only a trusted, bounded state file; suspicious paths mean paused.

        New workspaces have no pause file and start unpaused. Once a state or
        lock path exists, however, a symlink, hardlink, foreign owner, permissive
        mode, FIFO or invalid JSON cannot be used to impersonate an unpaused state.
        """
        try:
            root_info = self.root.lstat()
            if not stat.S_ISDIR(root_info.st_mode) or root_info.st_mode & 0o022:
                return _fail_closed()
        except FileNotFoundError:
            # A missing governance mount is not an untouched, initialized volume.
            return _fail_closed()
        except OSError:
            return _fail_closed()
        try:
            lock_info = (self.root / LOCK_FILE).lstat()
            if not _private_regular(lock_info):
                return _fail_closed()
        except FileNotFoundError:
            pass  # no transaction has run yet
        except OSError:
            return _fail_closed()
        marker_seen = self._marker_state()
        if marker_seen is None:
            return _fail_closed()
        try:
            path_info = self.path.lstat()
        except FileNotFoundError:
            # A newly provisioned *existing* volume may have no state file.
            # If its directory disappeared or was replaced during this read,
            # treating that missing file as first boot would allow writes.
            # A concurrent pause may create the state between the missing
            # lookup and the first-boot decision. Never return unpaused from
            # an observed transition: the next read can retry safely.
            # Check the root *before* probing again: a mount replaced during
            # the first lookup may be replaced yet again on the second.
            if not self._same_root(root_info):
                return _fail_closed()
            try:
                self.path.lstat()
            except FileNotFoundError:
                if not self._same_root(root_info):
                    return _fail_closed()
                # An initialized volume with a missing pause.json is corruption,
                # not a fresh deployment. Recheck the witness to catch a first
                # write racing the missing-state lookup.
                if marker_seen or self._marker_state() is not False:
                    return _fail_closed()
                return {"paused": False}
            except OSError:
                return _fail_closed()
            return _fail_closed()
        except OSError:
            return _fail_closed()
        try:
            if not _private_regular(path_info):
                return _fail_closed()
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            with os.fdopen(os.open(self.path, flags), "rb") as source:
                opened = os.fstat(source.fileno())
                if (not _private_regular(opened) or
                        (opened.st_dev, opened.st_ino) != (path_info.st_dev, path_info.st_ino)):
                    return _fail_closed()
                raw = source.read(MAX_STATE_BYTES + 1)
            if len(raw) > MAX_STATE_BYTES:
                return _fail_closed()
            document = json.loads(raw)
            if not isinstance(document, dict) or not isinstance(document.get("paused"), bool):
                return _fail_closed()
        except (OSError, ValueError, UnicodeError):
            # A file that disappears *after* lstat is suspicious, not fresh.
            return _fail_closed()
        # Atomic pause/resume replaces pause.json. If that happened while
        # decoding the previous inode, its stale "paused: false" cannot
        # authorize a new side effect. A fresh read will see the new state.
        try:
            current = self.path.lstat()
            if (not _private_regular(current) or
                    (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)):
                return _fail_closed()
        except OSError:
            return _fail_closed()
        # The root may be unmounted or swapped after the state inode was
        # inspected and decoded. Never trust an unpaused result from a
        # detached governance volume.
        if not self._same_root(root_info):
            return _fail_closed()
        if self._marker_state() is not marker_seen:
            return _fail_closed()
        if not document["paused"]:
            return {"paused": False, "by": document.get("by"), "at": document.get("at")}
        found = {"paused": True, "by": document.get("by"), "at": document.get("at"), "reason": clean(document.get("reason"))}
        for key in ("cause", "window"):
            if isinstance(document.get(key), str):
                found[key] = clean(document[key])[:40]
        return found

    def is_paused(self) -> bool:
        return bool(self.status()["paused"])

    @contextmanager
    def effect_guard(self) -> Iterator[None]:
        """Serialize a governed material effect with owner pause/resume.

        An already admitted effect finishes before a pause is acknowledged.
        A new effect must acquire the same trusted local lock and recheck the
        pause state while holding it. This is host-local and cooperative:
        it cannot cancel cloud writes or protect independent remote workers.
        """
        with self._transaction(shared=True):
            yield

    @contextmanager
    def _transaction(self, *, shared: bool = False, lock_timeout_seconds: float | None = None) -> Iterator[tuple[int, os.stat_result]]:
        """Hold the trusted mount and lock inode for a complete state transition.

        A replaced mount must never let a lock from the old volume authorize
        a write into a newly attached volume. All writes use the pinned dirfd.
        """
        if lock_timeout_seconds is not None and (
                isinstance(lock_timeout_seconds, bool) or
                not isinstance(lock_timeout_seconds, (int, float)) or
                not math.isfinite(lock_timeout_seconds) or
                not 0 < lock_timeout_seconds <= MAX_OWNER_LOCK_WAIT_SECONDS):
            raise ValueError("pause lock wait must be finite and between 0 and 30 seconds")
        root_info = self.root.lstat()
        if not stat.S_ISDIR(root_info.st_mode) or root_info.st_mode & 0o022:
            raise OSError("pause governance directory is untrusted")
        root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            opened_root = os.fstat(root_fd)
            if ((opened_root.st_dev, opened_root.st_ino) !=
                    (root_info.st_dev, root_info.st_ino) or not self._same_root(root_info)):
                raise OSError("pause governance directory changed")
            flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            descriptor = os.open(LOCK_FILE, flags, 0o600, dir_fd=root_fd)
            try:
                if not _private_regular(os.fstat(descriptor)):
                    raise OSError("pause transaction lock is untrusted")
                mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
                if lock_timeout_seconds is None:
                    fcntl.flock(descriptor, mode)
                else:
                    # Never acknowledge an owner command while an admitted
                    # material effect still holds the shared lease. The timeout
                    # reports an *unconfirmed* transition; it does not cancel
                    # the provider action or silently claim a pause.
                    deadline = time.monotonic() + lock_timeout_seconds
                    while True:
                        try:
                            fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
                            break
                        except OSError as exc:
                            if exc.errno not in (errno.EAGAIN, errno.EACCES):
                                raise
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise TimeoutError("pause transition not acknowledged: in-flight governed effect or state transition still holds the lock") from None
                            time.sleep(min(0.05, remaining))
                locked = os.fstat(descriptor)
                current_lock = os.stat(LOCK_FILE, dir_fd=root_fd, follow_symlinks=False)
                if (not _private_regular(locked) or not _private_regular(current_lock)
                        or (locked.st_dev, locked.st_ino) !=
                        (current_lock.st_dev, current_lock.st_ino)):
                    raise OSError("pause transaction lock changed")
                if not self._same_root(root_info):
                    raise OSError("pause governance directory changed during lock")
                yield root_fd, root_info
                if not self._same_root(root_info):
                    raise OSError("pause governance directory changed during transition")
            finally:
                os.close(descriptor)
        finally:
            os.close(root_fd)

    def _write(self, document: dict[str, Any], root_fd: int, root_info: os.stat_result) -> None:
        """Atomically replace pause state in the locked directory, not a raced path."""
        if not self._same_root(root_info):
            raise OSError("pause governance directory changed before write")
        # Commit a durable first-write witness *before* replacing the state.
        # A crash after this point must fail closed if pause.json is missing.
        # Legacy volumes without a witness remain readable until their first
        # post-upgrade pause/resume; no automatic migration unpauses anything.
        marker_fd = os.open(MARKER, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                            0o600, dir_fd=root_fd)
        try:
            marker_info = os.fstat(marker_fd)
            current_marker = os.stat(MARKER, dir_fd=root_fd, follow_symlinks=False)
            if (not _private_regular(marker_info) or not _private_regular(current_marker)
                    or (marker_info.st_dev, marker_info.st_ino) !=
                    (current_marker.st_dev, current_marker.st_ino)):
                raise OSError("pause initialization marker is untrusted")
            os.fsync(marker_fd)
            os.fsync(root_fd)
        finally:
            os.close(marker_fd)
        temporary = ".pause-" + secrets.token_hex(16)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600, dir_fd=root_fd)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                if not _private_regular(os.fstat(handle.fileno())):
                    raise OSError("pause temporary state is untrusted")
                json.dump(document, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            if not self._same_root(root_info):
                raise OSError("pause governance directory changed before replace")
            os.replace(temporary, FILE, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            os.fsync(root_fd)
            if not self._same_root(root_info):
                raise OSError("pause governance directory changed after replace")
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=root_fd)
            except OSError:
                pass
            raise

    def pause(self, by: str, reason: str, now: int, *, cause: str | None = None, window: str | None = None,
              lock_timeout_seconds: float | None = None) -> dict[str, Any]:
        by = clean(by)
        if not by:
            raise ValueError("who is pausing is required")
        with self._transaction(lock_timeout_seconds=lock_timeout_seconds) as transaction:
            # Re-check while holding the lock: a worker may have imposed a cap
            # between the caller's last read and this transition.
            state = self.status()
            if state["paused"] and state.get("by") is None:
                raise ValueError("untrusted pause state; operator must repair it before controls resume")
            if state.get("cause") == "spend-cap":
                raise ValueError("monthly spend cap is active; operator pause cannot replace it")
            extra = {key: value for key, value in (("cause", cause), ("window", window)) if value}
            self._write({"paused": True, "by": by, "at": int(now), "reason": clean(reason), **extra}, *transaction)
            return self.status()

    def resume(self, by: str, now: int, *, lock_timeout_seconds: float | None = None) -> dict[str, Any]:
        by = clean(by)
        if not by:
            raise ValueError("who is resuming is required")
        with self._transaction(lock_timeout_seconds=lock_timeout_seconds) as transaction:
            state = self.status()
            if state["paused"] and state.get("by") is None:
                raise ValueError("untrusted pause state; operator must repair it before controls resume")
            if state.get("cause") == "spend-cap":
                raise ValueError("monthly spend cap is active; wait for metering reconciliation")
            self._write({"paused": False, "by": by, "at": int(now)}, *transaction)
            return self.status()

    def clear_spend_cap(self, now: int, *, expected_window: str) -> bool:
        """Worker reconciliation of its own cap; never clear an owner pause.

        All state checks and the replace must share the same lock as manual
        commands or the check can become stale before the write.
        """
        with self._transaction() as transaction:
            state = self.status()
            if not (state["paused"] and state.get("cause") == "spend-cap"
                    and state.get("window") == expected_window):
                return False
            self._write({"paused": False, "by": "runtime:worker", "at": int(now)}, *transaction)
            return True

"""Private, fail-closed POSIX persistence for the tenant schedule store.

Keep the directory and lock pinned by file descriptor. A predictable temporary
path, symlink, hardlink, permissive file or truncated JSON must never grant a
schedule a new owner, goal or execution authority.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pathlib
import secrets
import stat
from collections.abc import Iterator
from typing import Any

MAX_STATE_BYTES = 4 * 1024 * 1024


def _private(info: os.stat_result, error: type[ValueError], *, directory: bool = False) -> None:
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or info.st_uid != os.geteuid() or info.st_mode & 0o077
            or (not directory and info.st_nlink != 1)):
        raise error("schedule store has unsafe ownership, permissions or links")


@contextlib.contextmanager
def locked(root: pathlib.Path, tenant: str, *, schema_version: int, max_schedules: int,
           error: type[ValueError]) -> Iterator[dict[str, Any]]:
    """Read/update state under one pinned lock; publish with fsync and exclusive random temp."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = None
    lock_fd = None
    temporary = None
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        _private(os.fstat(directory), error, directory=True)
        lock_name = f".{tenant}.schedules.lock"
        lock_fd = os.open(lock_name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                          0o600, dir_fd=directory)
        _private(os.fstat(lock_fd), error)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        lock_info = os.stat(lock_name, dir_fd=directory, follow_symlinks=False)
        opened_lock = os.fstat(lock_fd)
        if (lock_info.st_ino, lock_info.st_dev) != (opened_lock.st_ino, opened_lock.st_dev):
            raise error("schedule store lock changed during acquisition")

        name = f"{tenant}.schedules.json"
        state: dict[str, Any] = {"schemaVersion": schema_version, "tenant": tenant, "schedules": {}}
        initial = None
        try:
            state_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        except FileNotFoundError:
            state_fd = None
        if state_fd is not None:
            try:
                initial = os.fstat(state_fd)
                _private(initial, error)
                if initial.st_size > MAX_STATE_BYTES:
                    raise error("schedule store exceeds its size limit")
                with os.fdopen(state_fd, "rb") as stream:
                    state_fd = None
                    raw = stream.read(MAX_STATE_BYTES + 1)
                if len(raw) > MAX_STATE_BYTES:
                    raise error("schedule store exceeds its size limit")
                try:
                    loaded = json.loads(raw.decode("utf-8"))
                except (UnicodeError, ValueError) as exc:
                    raise error("schedule store is corrupt") from exc
                if (not isinstance(loaded, dict) or type(loaded.get("schemaVersion")) is not int
                        or loaded["schemaVersion"] != schema_version or loaded.get("tenant") != tenant
                        or not isinstance(loaded.get("schedules"), dict)
                        or len(loaded["schedules"]) > max_schedules):
                    raise error("schedule store has an unsupported format or belongs to another tenant")
                state = loaded
            finally:
                if state_fd is not None:
                    os.close(state_fd)

        before = json.dumps(state, sort_keys=True)
        yield state
        after = json.dumps(state, sort_keys=True)
        if after != before:
            encoded = (after + "\n").encode("utf-8")
            if len(encoded) > MAX_STATE_BYTES:
                raise error("schedule store exceeds its size limit")
            # A non-cooperating process must not replace the snapshot under this lock.
            try:
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                current = None
            if ((initial is None) != (current is None)
                    or (initial is not None and current is not None and
                        (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns)
                        != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns))):
                raise error("schedule store changed outside its lock")
            root_now = os.stat(root, follow_symlinks=False)
            root_open = os.fstat(directory)
            if (root_now.st_dev, root_now.st_ino) != (root_open.st_dev, root_open.st_ino):
                raise error("schedule store directory changed during write")
            temporary = f".{tenant}.{secrets.token_hex(16)}.tmp"
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=directory)
            with os.fdopen(handle, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            temporary = None
            os.fsync(directory)
    except OSError as exc:
        raise error("schedule store filesystem operation failed") from exc
    finally:
        if temporary is not None and directory is not None:
            try:
                os.unlink(temporary, dir_fd=directory)
            except OSError:
                pass
        if lock_fd is not None:
            os.close(lock_fd)
        if directory is not None:
            os.close(directory)

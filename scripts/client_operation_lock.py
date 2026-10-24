"""Fail-closed cooperative host lock for client Docker deploys and backup windows (#230).

The directory is pre-provisioned by the host operator; never auto-create or
unlink lock files. This cannot stop Docker restart policies or uncooperative
processes and is not a substitute for an approved maintenance freeze.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
import pathlib
import re
import stat
from collections.abc import Iterator

LOCK_ROOT = pathlib.Path("/run/lock/model-garden")
PROJECT = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class OperationLockError(ValueError):
    """A host lock is unavailable, unsafe or already held."""


@contextmanager
def project_operation_lock(project: str) -> Iterator[None]:
    """Exclude cooperative deploy/backup operations for one exact Compose project."""
    if not isinstance(project, str) or not PROJECT.fullmatch(project):
        raise OperationLockError("invalid client project for host operation lock")
    directory_fd = None
    lock_fd = None
    try:
        try:
            directory_fd = os.open(LOCK_ROOT, os.O_RDONLY | os.O_DIRECTORY |
                                   getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
            directory = os.fstat(directory_fd)
            if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid() or \
               stat.S_IMODE(directory.st_mode) != 0o700:
                raise OperationLockError("host operation lock directory is not private and operator-owned")
            lock_fd = os.open(project + ".lock", os.O_CREAT | os.O_RDWR |
                              getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                              0o600, dir_fd=directory_fd)
            info = os.fstat(lock_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or \
               info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
                raise OperationLockError("host operation lock file is unsafe")
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # The directory or lock name may have been atomically replaced
            # while opening/flocking. Never authorize work under a detached
            # inode that a second process cannot see or lock.
            current_root = os.stat(LOCK_ROOT, follow_symlinks=False)
            current_lock = os.stat(project + ".lock", dir_fd=directory_fd,
                                   follow_symlinks=False)
            if (not stat.S_ISDIR(current_root.st_mode) or
                    current_root.st_uid != os.geteuid() or
                    stat.S_IMODE(current_root.st_mode) != 0o700 or
                    (current_root.st_dev, current_root.st_ino) !=
                    (directory.st_dev, directory.st_ino) or
                    not stat.S_ISREG(current_lock.st_mode) or
                    current_lock.st_uid != os.geteuid() or
                    current_lock.st_nlink != 1 or
                    stat.S_IMODE(current_lock.st_mode) != 0o600 or
                    (current_lock.st_dev, current_lock.st_ino) !=
                    (info.st_dev, info.st_ino)):
                raise OperationLockError("host operation lock replaced during acquisition")
        except OSError as exc:
            raise OperationLockError("host operation lock unavailable or another operation is active") from exc
        yield
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if directory_fd is not None:
            os.close(directory_fd)

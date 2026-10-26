"""Bounded, authenticated *draft intake* for Tier 0 business-rules files.

The runtime data volume is NOT the approved workspace. No upload becomes a
KnowledgeSource, changes an Agent, or reaches kb.search until a separate owner
review, private workspace import and protected promotion.
"""
from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import stat
import sys
from collections.abc import Callable
from typing import Any

MAX_BYTES = 64 * 1024
MAX_PENDING = 20
_REQUEST = re.compile(r"^[0-9a-f]{32}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}\.(?:txt|md)$", re.IGNORECASE)
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class UploadError(ValueError):
    """Unacceptable file or request; never echo file content."""


class UploadConflict(UploadError):
    """An unsafe store, changed replay or exhausted pending quota."""


class UploadUnavailable(UploadError):
    """The write or its required audit receipt could not complete."""


def _knowledge():
    name = "model_garden_web_knowledge_core"
    module = sys.modules.get(name)
    if module is None:
        path = pathlib.Path(__file__).with_name("knowledge.py")
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def _private_dir(path: pathlib.Path) -> None:
    if path.is_symlink():
        raise UploadConflict("upload store must not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise UploadConflict("upload store must be a private directory")


def _regular(path: pathlib.Path) -> bool:
    if path.is_symlink():
        raise UploadConflict("upload entry must not be a symlink")
    if not path.exists():
        return False
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise UploadConflict("upload entry must be a private regular file")
    return True


def _create(path: pathlib.Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def stage(
    root: pathlib.Path, *, tenant: str, by: str, filename: str, content: str,
    request_id: str, now: int, audit: Callable[[str, str, dict[str, Any]], None],
) -> dict[str, Any]:
    """Persist one verified person's file, idempotently; require a ledger receipt."""
    if not isinstance(tenant, str) or not _TENANT.fullmatch(tenant):
        raise UploadError("invalid tenant")
    if not isinstance(by, str) or not 1 <= len(by) <= 256 or any(c in by for c in "\r\n\x00"):
        raise UploadError("invalid verified uploader")
    if not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id):
        raise UploadError("requestId must be 32 lowercase hex characters")
    if not isinstance(filename, str) or not _NAME.fullmatch(filename) or ".." in filename:
        raise UploadError("filename must be a simple .txt or .md basename")
    if not isinstance(content, str) or not content.strip() or "\x00" in content:
        raise UploadError("content must be nonempty UTF-8 text")
    try:
        raw = content.encode("utf-8")
    except UnicodeError as exc:
        raise UploadError("content must be valid UTF-8 text") from exc
    if len(raw) > MAX_BYTES:
        raise UploadError("upload exceeds the 64 KiB limit")
    try:
        report = _knowledge().check_source(content, filename=filename, classification="internal")
    except (ValueError, UnicodeError) as exc:
        raise UploadError("file could not be parsed as business rules") from exc

    upload_id = hashlib.sha256(f"{tenant}\0{by}\0{request_id}".encode("utf-8")).hexdigest()[:32]
    digest = hashlib.sha256(raw).hexdigest()
    extension = pathlib.Path(filename).suffix.lower()
    directory = pathlib.Path(root) / tenant
    output = {"uploadId": upload_id, "status": "draft", "sha256": digest,
              "sectionCount": report["sectionCount"], "alreadyStaged": False,
              "next": "A separate client-owner review and protected workspace promotion are required."}
    _private_dir(pathlib.Path(root))
    _private_dir(directory)
    lock = directory / ".upload.lock"
    if lock.is_symlink():
        raise UploadConflict("upload lock must not be a symlink")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise UploadConflict("upload lock must be a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        metadata_path = directory / f"u-{upload_id}.json"
        content_path = directory / f"u-{upload_id}{extension}"
        if _regular(metadata_path):
            if not _regular(content_path):
                raise UploadConflict("upload receipt exists without its file")
            try:
                existing = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise UploadConflict("upload receipt is invalid") from exc
            if (existing.get("tenant") != tenant or existing.get("by") != by or
                    existing.get("filename") != filename or existing.get("sha256") != digest or
                    existing.get("status") != "draft" or content_path.read_bytes() != raw):
                raise UploadConflict("requestId was already used for different content")
            output["alreadyStaged"] = True
            return output
        if _regular(content_path):
            raise UploadConflict("upload file exists without its receipt")
        if len(list(directory.glob("u-*.json"))) >= MAX_PENDING:
            raise UploadConflict("pending upload quota reached; review existing drafts")
        metadata = {"uploadId": upload_id, "tenant": tenant, "by": by, "filename": filename,
                    "status": "draft", "sha256": digest, "bytes": len(raw),
                    "sectionCount": report["sectionCount"], "createdAt": int(now)}
        # A crash before the ledger receipt must not leave an apparently
        # accepted draft. Pending-audit files fail closed on any retry.
        created: list[pathlib.Path] = []
        publish_path = directory / f".u-{upload_id}.publish"
        try:
            _create(content_path, raw)
            created.append(content_path)
            pending = {**metadata, "status": "pending-audit"}
            _create(metadata_path, (json.dumps(pending, sort_keys=True) + "\n").encode("utf-8"))
            created.append(metadata_path)
            audit("knowledge.upload-staged", by, {"uploadId": upload_id, "sha256": digest,
                                                  "bytes": len(raw), "status": "draft"})
            if _regular(publish_path):
                raise UploadConflict("stale upload publication marker needs operator review")
            _create(publish_path, (json.dumps(metadata, sort_keys=True) + "\n").encode("utf-8"))
            os.replace(publish_path, metadata_path)
        except Exception as exc:
            publish_path.unlink(missing_ok=True)
            for item in reversed(created):
                item.unlink(missing_ok=True)
            raise UploadUnavailable("upload was not accepted; storage or audit unavailable") from exc
        return output
    finally:
        os.close(fd)

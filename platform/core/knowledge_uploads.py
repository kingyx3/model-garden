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
import secrets
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


def _retention(days: int) -> int:
    if type(days) is not int or not 1 <= days <= 3650:
        raise UploadError("retention_days must be 1 to 3650")
    return days


def _expires(metadata: dict[str, Any], days: int) -> int:
    created = metadata.get("createdAt")
    stored = metadata.get("expiresAt")
    if type(created) is not int or created < 0:
        raise UploadConflict("upload creation time is invalid")
    if stored is not None and (type(stored) is not int or stored < created):
        raise UploadConflict("upload expiry is invalid")
    limit = created + _retention(days) * 86400
    return min(limit, stored) if stored is not None else limit


def stage(
    root: pathlib.Path, *, tenant: str, by: str, filename: str, content: str,
    request_id: str, now: int, audit: Callable[[str, str, dict[str, Any]], None],
    retention_days: int = 365,
) -> dict[str, Any]:
    """Persist one verified person's file, idempotently; require a ledger receipt."""
    retention_days = _retention(retention_days)
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
        if _regular(directory / f".deleted-{upload_id}.json"):
            raise UploadConflict("deleted requestId cannot be reused")
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
            if int(now) >= _expires(existing, retention_days):
                raise UploadConflict("draft expired; operator reconciliation required")
            output["alreadyStaged"] = True
            return output
        if _regular(content_path):
            raise UploadConflict("upload file exists without its receipt")
        if len(list(directory.glob("u-*.json"))) >= MAX_PENDING:
            raise UploadConflict("pending upload quota reached; review existing drafts")
        metadata = {"uploadId": upload_id, "tenant": tenant, "by": by, "filename": filename,
                    "status": "draft", "sha256": digest, "bytes": len(raw),
                    "sectionCount": report["sectionCount"], "createdAt": int(now),
                    "expiresAt": int(now) + retention_days * 86400}
        # A crash before the ledger receipt must not leave an apparently
        # accepted draft. Pending-audit files fail closed on any retry.
        created: list[pathlib.Path] = []
        publish_path = directory / f".u-{upload_id}-{secrets.token_hex(8)}.publish"
        published_temp = False
        try:
            _create(content_path, raw)
            created.append(content_path)
            pending = {**metadata, "status": "pending-audit"}
            _create(metadata_path, (json.dumps(pending, sort_keys=True) + "\n").encode("utf-8"))
            created.append(metadata_path)
            audit("knowledge.upload-staged", by, {"uploadId": upload_id, "sha256": digest,
                                                  "bytes": len(raw), "status": "draft"})
            _create(publish_path, (json.dumps(metadata, sort_keys=True) + "\n").encode("utf-8"))
            published_temp = True
            os.replace(publish_path, metadata_path)
        except Exception as exc:
            if published_temp:
                publish_path.unlink(missing_ok=True)
            for item in reversed(created):
                item.unlink(missing_ok=True)
            raise UploadUnavailable("upload was not accepted; storage or audit unavailable") from exc
        return output
    finally:
        os.close(fd)


def attest_owner_review(
    root: pathlib.Path, *, tenant: str, upload_id: str, expected_sha256: str,
    decision: str, owner: str, now: int, retention_days: int,
    audit: Callable[[str, str, dict[str, Any]], None],
) -> dict[str, Any]:
    """Record a separately authenticated owner's exact-digest review, never promote.

    Caller MUST verify the signed session, mapped approver role and configured
    client-owner identity. The review marker is not a KnowledgeSource approval.
    A pending marker after an interrupted audit is deliberately non-replayable.
    """
    days = _retention(retention_days)
    if not isinstance(tenant, str) or not _TENANT.fullmatch(tenant):
        raise UploadError("invalid tenant")
    if not isinstance(upload_id, str) or not _REQUEST.fullmatch(upload_id):
        raise UploadError("invalid upload identifier")
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise UploadError("invalid source digest")
    if decision not in ("approve", "reject"):
        raise UploadError("invalid review decision")
    if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9._+@-]{3,254}", owner):
        raise UploadError("invalid verified owner")
    directory = pathlib.Path(root) / tenant
    if not pathlib.Path(root).is_dir() or not directory.is_dir():
        raise UploadConflict("staged draft not found")
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
        if not _regular(metadata_path) or metadata_path.stat().st_size > 4096:
            raise UploadConflict("draft receipt missing or invalid")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise UploadConflict("draft receipt invalid") from exc
        if (not isinstance(metadata, dict) or metadata.get("tenant") != tenant or
                metadata.get("uploadId") != upload_id or metadata.get("status") != "draft" or
                metadata.get("sha256") != expected_sha256 or metadata.get("by") == owner or
                int(now) >= _expires(metadata, days)):
            raise UploadConflict("draft cannot be reviewed by this owner")
        filename = metadata.get("filename")
        if not isinstance(filename, str) or not _NAME.fullmatch(filename) or ".." in filename:
            raise UploadConflict("draft filename invalid")
        content_path = directory / f"u-{upload_id}{pathlib.Path(filename).suffix.lower()}"
        if (not _regular(content_path) or content_path.stat().st_size > MAX_BYTES or
                content_path.stat().st_size != metadata.get("bytes") or
                hashlib.sha256(content_path.read_bytes()).hexdigest() != expected_sha256):
            raise UploadConflict("draft content no longer matches the reviewed digest")
        if _regular(directory / f".deleted-{upload_id}.json"):
            raise UploadConflict("draft has been deleted")
        reviewed = directory / f"r-{upload_id}.json"
        pending = directory / f".r-{upload_id}.pending"
        owner_hash = hashlib.sha256(owner.encode("utf-8")).hexdigest()
        record = {"tenant": tenant, "uploadId": upload_id, "sha256": expected_sha256,
                  "decision": decision, "ownerHash": owner_hash, "reviewedAt": int(now)}
        status = "reviewed-for-import" if decision == "approve" else "reviewed-rejected"
        if _regular(pending):
            raise UploadConflict("interrupted owner review needs operator reconciliation")
        if _regular(reviewed):
            try:
                existing = json.loads(reviewed.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise UploadConflict("owner review receipt invalid") from exc
            if (not isinstance(existing, dict) or
                    any(existing.get(k) != record[k] for k in ("tenant", "uploadId", "sha256", "decision", "ownerHash"))):
                raise UploadConflict("owner review is immutable")
            return {"uploadId": upload_id, "status": status, "alreadyReviewed": True,
                    "next": "Private workspace import and protected client-owner promotion remain separate."}
        # A crashed or failed audit must never create an apparently approved
        # source. Pending markers are poison until an operator reconciles them.
        try:
            _create(pending, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
            audit("knowledge.upload-owner-reviewed", owner,
                  {"uploadId": upload_id, "sha256": expected_sha256, "decision": decision})
            _create(reviewed, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
            pending.unlink()
        except Exception as exc:
            raise UploadUnavailable("owner review not accepted; inspect pending marker and ledger") from exc
        return {"uploadId": upload_id, "status": status, "alreadyReviewed": False,
                "next": "Private workspace import and protected client-owner promotion remain separate."}
    finally:
        os.close(fd)


def purge_expired(
    root: pathlib.Path, *, tenant: str, now: int, retention_days: int,
    audit: Callable[[str, str, dict[str, Any]], None], actor: str,
    dry_run: bool = True, erase_id: str | None = None, reason: str | None = None,
) -> list[str]:
    """Operator-only reconciliation; dry-run by default, never follow foreign paths.

    The caller must already hold host/operator authorization. A deletion-intent
    ledger receipt precedes file removal; a minimal tombstone prevents replay.
    If a later unlink or completion receipt fails, fail closed for operator repair.
    Backups and imported KnowledgeSources are deliberately outside this scope.
    """
    days = _retention(retention_days)
    if not isinstance(tenant, str) or not _TENANT.fullmatch(tenant):
        raise UploadError("invalid tenant")
    if not isinstance(actor, str) or not re.fullmatch(r"operator:[A-Za-z0-9._@-]{1,100}", actor):
        raise UploadError("a named operator identity is required")
    if erase_id is not None:
        if not isinstance(erase_id, str) or not re.fullmatch(r"[0-9a-f]{32}", erase_id):
            raise UploadError("erase_id must be a full opaque upload identifier")
        if reason not in ("owner-request", "reconciliation"):
            raise UploadError("explicit erasure requires owner-request or reconciliation reason")
    elif reason is not None:
        raise UploadError("reason is only accepted with erase_id")
    root = pathlib.Path(root)
    directory = root / tenant
    if not root.exists() and not root.is_symlink():
        if erase_id:
            raise UploadError("tenant upload store does not exist")
        return []
    _private_dir(root)
    if not directory.exists() and not directory.is_symlink():
        if erase_id:
            raise UploadError("tenant upload store does not exist")
        return []
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
        selected: list[tuple[str, pathlib.Path, pathlib.Path, dict[str, Any], str]] = []
        for metadata_path in sorted(directory.glob("u-*.json")):
            if not _regular(metadata_path) or metadata_path.stat().st_size > 4096:
                raise UploadConflict("upload receipt is not a bounded regular file")
            upload_id = metadata_path.stem[2:]
            if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
                raise UploadConflict("unexpected upload receipt name")
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise UploadConflict("upload receipt is invalid") from exc
            if not isinstance(metadata, dict) or metadata.get("tenant") != tenant or metadata.get("uploadId") != upload_id:
                raise UploadConflict("upload receipt tenant or identifier mismatch")
            filename = metadata.get("filename")
            if not isinstance(filename, str) or not _NAME.fullmatch(filename) or ".." in filename:
                raise UploadConflict("upload filename is invalid")
            if metadata.get("status") not in ("draft", "pending-audit"):
                raise UploadConflict("upload state needs operator review")
            suffix = pathlib.Path(filename).suffix.lower()
            content_path = directory / f"u-{upload_id}{suffix}"
            if not _regular(content_path) or content_path.stat().st_size > MAX_BYTES:
                raise UploadConflict("upload content missing or invalid")
            for other in (".txt", ".md"):
                if other != suffix and _regular(directory / f"u-{upload_id}{other}"):
                    raise UploadConflict("upload has conflicting content files")
            if (hashlib.sha256(content_path.read_bytes()).hexdigest() != metadata.get("sha256") or
                    content_path.stat().st_size != metadata.get("bytes")):
                raise UploadConflict("upload content digest or size mismatch")
            review_path = directory / f"r-{upload_id}.json"
            if _regular(directory / f".r-{upload_id}.pending"):
                raise UploadConflict("interrupted owner review needs operator reconciliation")
            if _regular(review_path):
                try:
                    review = json.loads(review_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise UploadConflict("owner review receipt invalid") from exc
                if (not isinstance(review, dict) or review.get("tenant") != tenant or
                        review.get("uploadId") != upload_id or review.get("sha256") != metadata["sha256"] or
                        review.get("decision") not in ("approve", "reject")):
                    raise UploadConflict("owner review receipt mismatch")
            expired = int(now) >= _expires(metadata, days)
            if not expired and upload_id != erase_id:
                continue
            tombstone = directory / f".deleted-{upload_id}.json"
            if _regular(tombstone):
                raise UploadConflict("upload deletion needs operator reconciliation")
            selected.append((upload_id, metadata_path, content_path, metadata,
                             reason if upload_id == erase_id else "retention"))
        if erase_id and not any(item[0] == erase_id for item in selected):
            raise UploadError("requested upload not found in this tenant")
        if dry_run:
            return [item[0] for item in selected]
        deleted: list[str] = []
        for upload_id, metadata_path, content_path, metadata, why in selected:
            details = {"uploadId": upload_id, "sha256": metadata["sha256"],
                       "reason": why, "status": metadata["status"]}
            tombstone = directory / f".deleted-{upload_id}.json"
            try:
                audit("knowledge.upload-delete-intent", actor, details)
            except Exception as exc:
                raise UploadUnavailable("audit unavailable; no upload was deleted") from exc
            try:
                _create(tombstone, (json.dumps({"uploadId": upload_id, "sha256": metadata["sha256"],
                                               "deletedAt": int(now), "reason": why},
                                              sort_keys=True) + "\n").encode("utf-8"))
                content_path.unlink()
                metadata_path.unlink()
                review_path = directory / f"r-{upload_id}.json"
                if _regular(review_path):
                    review_path.unlink()
                audit("knowledge.upload-deleted", actor, details)
            except Exception as exc:
                raise UploadUnavailable("upload deletion incomplete; inspect tombstone and ledger") from exc
            deleted.append(upload_id)
        return deleted
    finally:
        os.close(fd)

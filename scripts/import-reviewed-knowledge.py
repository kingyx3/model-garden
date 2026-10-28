#!/usr/bin/env python3
"""Operator-only reviewed Tier 0 upload -> draft KnowledgeSource import (#77).

This is an explicitly confirmed, private-host step, not a remote approval or
production promotion. It reuses the existing signed-owner review and importer.
The client must separately approve the protected workspace diff and run evals.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import stat
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
HEX32 = re.compile(r"^[0-9a-f]{32}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
ACTOR = re.compile(r"^operator:[A-Za-z0-9._@-]{1,100}$")
FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}\.(?:md|txt)$", re.I)


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


IMPORT = _load("reviewed_import_core", ROOT / "scripts/import-knowledge.py")
GOVERN = _load("reviewed_import_audit", ROOT / "scripts/govern-action.py")


def _private_directory(path: pathlib.Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("upload directory is not private to the operator")


def _read_private(path: pathlib.Path, limit: int) -> bytes:
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
            or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o077
            or not 0 < before.st_size <= limit):
        raise ValueError("upload entry is not a bounded private regular file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(fd)
        if ((info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)
                or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size != before.st_size):
            raise ValueError("upload entry changed during inspection")
        raw = os.read(fd, limit + 1)
        after = os.fstat(fd)
        if (len(raw) != info.st_size or len(raw) > limit
                or (after.st_mtime_ns, after.st_ctime_ns, after.st_size) !=
                   (info.st_mtime_ns, info.st_ctime_ns, info.st_size)):
            raise ValueError("upload entry changed during read")
        return raw
    finally:
        os.close(fd)


def _json(path: pathlib.Path) -> dict:
    try:
        value = json.loads(_read_private(path, 4096).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("upload receipt is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("upload receipt is invalid")
    return value


def import_reviewed(
    data_root: pathlib.Path, workspace: pathlib.Path, *, tenant: str,
    upload_id: str, expected_sha256: str, owner: str, actor: str,
    name: str, agent: str, classification: str, retention_days: int,
    now: int, audit, dry_run: bool = True,
) -> dict:
    """Inspect exact owner-reviewed bytes and optionally import them as DRAFT.

    The upload lock prevents cooperating stage/review/delete operations during
    inspection and import. The imported source is a private pinned copy.
    """
    if not TENANT.fullmatch(tenant) or not HEX32.fullmatch(upload_id) or not HEX64.fullmatch(expected_sha256):
        raise ValueError("invalid tenant, upload ID or expected digest")
    if not ACTOR.fullmatch(actor) or not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9._+@-]{3,254}", owner):
        raise ValueError("named operator and separately verified owner are required")
    if type(retention_days) is not int or not 1 <= retention_days <= 3650 or type(now) is not int:
        raise ValueError("invalid retention or inspection time")
    workspace = pathlib.Path(workspace)
    if workspace.is_symlink() or not workspace.is_dir() or workspace.name != tenant:
        raise ValueError("workspace must be the explicit tenant-named private workspace")
    directory = pathlib.Path(data_root) / "knowledge-uploads" / tenant
    _private_directory(pathlib.Path(data_root))
    _private_directory(directory.parent)
    _private_directory(directory)
    lock_path = directory / ".upload.lock"
    lock_info = lock_path.lstat()
    if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1
            or lock_info.st_uid != os.geteuid() or stat.S_IMODE(lock_info.st_mode) & 0o077):
        raise ValueError("upload lock is not private")
    fd = os.open(lock_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if os.fstat(fd).st_ino != lock_info.st_ino:
            raise ValueError("upload lock changed")
        fcntl.flock(fd, fcntl.LOCK_EX)
        for marker in (f".r-{upload_id}.pending", f".deleted-{upload_id}.json"):
            if (directory / marker).exists() or (directory / marker).is_symlink():
                raise ValueError("interrupted or deleted upload cannot be imported")
        metadata = _json(directory / f"u-{upload_id}.json")
        review = _json(directory / f"r-{upload_id}.json")
        filename = metadata.get("filename")
        if not isinstance(filename, str) or not FILENAME.fullmatch(filename) or ".." in filename:
            raise ValueError("invalid uploaded filename")
        raw = _read_private(directory / f"u-{upload_id}{pathlib.Path(filename).suffix.lower()}", 64 * 1024)
        digest = hashlib.sha256(raw).hexdigest()
        created, expiry, reviewed = metadata.get("createdAt"), metadata.get("expiresAt"), review.get("reviewedAt")
        if (metadata.get("tenant") != tenant or metadata.get("uploadId") != upload_id
                or metadata.get("status") != "draft" or metadata.get("by") == owner
                or not isinstance(metadata.get("by"), str)
                or metadata.get("sha256") != expected_sha256 or digest != expected_sha256
                or metadata.get("bytes") != len(raw) or type(metadata.get("sectionCount")) is not int
                or type(created) is not int or type(expiry) is not int
                or type(reviewed) is not int or created > reviewed or reviewed > now + 60
                or now >= min(expiry, created + retention_days * 86400)
                or review.get("tenant") != tenant or review.get("uploadId") != upload_id
                or review.get("sha256") != expected_sha256 or review.get("decision") != "approve"
                or review.get("ownerHash") != hashlib.sha256(owner.encode("utf-8")).hexdigest()):
            raise ValueError("draft and independently reviewed receipt do not match or have expired")
        if not dry_run and audit is None:
            raise ValueError("apply requires an available Action Ledger")
        # The importer consumes a private, immutable-for-this-call copy, not
        # the original path that another host operator could replace.
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".reviewed-import-",
                                         suffix=pathlib.Path(filename).suffix.lower(),
                                         dir=directory, delete=False) as stream:
            temporary = pathlib.Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            options = dict(name=name, owner=owner, agent=agent, classification=classification)
            preview = IMPORT.import_knowledge(workspace, temporary, dry_run=True, **options)
            if preview["alreadyImported"]:
                # A previously approved KnowledgeSource is not proof of this
                # particular review/promotion. Never claim to approve it.
                import yaml
                doc = yaml.safe_load(pathlib.Path(preview["declaration"]).read_text())
                if doc.get("spec", {}).get("status") != "draft":
                    raise ValueError("existing source is no longer a draft")
            result = {**preview, "tenant": tenant, "uploadId": upload_id,
                      "dryRun": dry_run,
                      "next": "Protected client-owner workspace approval, eval and DEV promotion are still required."}
            if dry_run or preview["alreadyImported"]:
                return result
            details = {"uploadId": upload_id, "sha256": digest, "source": name, "status": "draft"}
            audit("knowledge.reviewed-import-intent", actor, details)
            imported = IMPORT.import_knowledge(workspace, temporary, dry_run=False, **options)
            audit("knowledge.reviewed-imported", actor, details)
            return {**result, "alreadyImported": imported["alreadyImported"], "dryRun": False}
        finally:
            temporary.unlink(missing_ok=True)
    finally:
        os.close(fd)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=pathlib.Path)
    parser.add_argument("--data-root", type=pathlib.Path, required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--upload-id", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--owner", required=True, help="independently verified owner address; not authentication")
    parser.add_argument("--actor", required=True, help="operator:<named-person>; host authorization required")
    parser.add_argument("--name", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--classification", choices=("public", "internal", "confidential"), required=True)
    parser.add_argument("--retention-days", type=int, required=True)
    parser.add_argument("--audit-path", type=pathlib.Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-tenant")
    args = parser.parse_args(argv)
    if args.apply and (args.confirm_tenant != args.tenant or args.audit_path is None
                       or args.audit_path.is_symlink() or not args.audit_path.is_file()):
        parser.error("--apply requires matching --confirm-tenant and an existing regular --audit-path")
    try:
        result = import_reviewed(
            args.data_root, args.workspace, tenant=args.tenant, upload_id=args.upload_id,
            expected_sha256=args.expected_sha256, owner=args.owner, actor=args.actor,
            name=args.name, agent=args.agent, classification=args.classification,
            retention_days=args.retention_days, now=int(time.time()), dry_run=not args.apply,
            audit=(lambda action, by, details: GOVERN.record_operator_action(
                args.audit_path, action, by, details)) if args.apply else None,
        )
    except (ValueError, OSError) as exc:
        print(f"Reviewed knowledge import refused: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(json.dumps({k: result[k] for k in ("tenant", "uploadId", "name", "agent", "status",
                                           "sha256", "dryRun", "alreadyImported", "next")},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

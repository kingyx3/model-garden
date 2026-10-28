#!/usr/bin/env python3
"""Read-only, fail-closed age-recipient rotation preflight for retained snapshots.

This checks only the operator-supplied, privately downloaded snapshot inventory.
It never rotates a key, deletes an old identity, changes a backup policy, accesses
GCS, or proves a live restore. Retain the old identity until independent cloud
inventory and client-approved recovery verification establish safe retirement.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import pathlib
import stat
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("client_volume_restore_for_rotation", ROOT / "scripts" / "client-volume-restore.py")
RESTORE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(RESTORE)

MAX_SNAPSHOTS = 4096


class RotationError(ValueError):
    pass


def _private_manifest(path: pathlib.Path) -> tuple[dict[str, Any], bytes]:
    """Read bounded private JSON without following links; return exact bytes for recheck."""
    RESTORE._private_input(path.parent, label="snapshot directory", directory=True)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_nlink != 1 or info.st_mode & 0o077
                    or not 0 < info.st_size <= RESTORE.MAX_MANIFEST_BYTES):
                raise RotationError("unsafe or oversized snapshot manifest")
            payload = stream.read(RESTORE.MAX_MANIFEST_BYTES + 1)
    except OSError as exc:
        raise RotationError("snapshot manifest missing or unsafe") from exc
    if not 0 < len(payload) <= RESTORE.MAX_MANIFEST_BYTES:
        raise RotationError("snapshot manifest invalid size")
    try:
        doc = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise RotationError("snapshot manifest invalid") from exc
    if not isinstance(doc, dict):
        raise RotationError("snapshot manifest must be an object")
    return doc, payload


def plan_rotation(*, compose: pathlib.Path, project: str, snapshots: pathlib.Path,
                  old_recipient: str, new_recipient: str,
                  old_identity: pathlib.Path) -> dict[str, Any]:
    """Authenticate every supplied old-key snapshot without touching client data."""
    if not isinstance(project, str) or not RESTORE.SNAPSHOT.NAME.fullmatch(project):
        raise RotationError("invalid client project")
    if (not isinstance(old_recipient, str) or not isinstance(new_recipient, str)
            or not RESTORE.SNAPSHOT.RECIPIENT.fullmatch(old_recipient)
            or not RESTORE.SNAPSHOT.RECIPIENT.fullmatch(new_recipient)
            or old_recipient == new_recipient):
        raise RotationError("distinct valid old and proposed age recipients required")
    if (compose.is_symlink() or not compose.is_file()
            or compose.stat().st_mode & 0o022):
        raise RotationError("unsafe Compose inventory")
    try:
        RESTORE._private_identity(old_identity)
        RESTORE._private_input(snapshots, label="snapshot inventory", directory=True)
        entries = sorted(snapshots.iterdir(), key=lambda p: p.name)
        if not entries or len(entries) > MAX_SNAPSHOTS:
            raise RotationError("snapshot inventory empty or exceeds limit")
        old_fingerprint = hashlib.sha256(old_recipient.encode("ascii")).hexdigest()
        new_fingerprint = hashlib.sha256(new_recipient.encode("ascii")).hexdigest()
        total_volumes = 0
        for source in entries:
            # Do not skip incomplete, foreign, linked or unrecognized material.
            if (not source.name.startswith(project + "-")
                    or not source.name[len(project) + 1:].replace("-", "").isalnum()):
                raise RotationError("unexpected snapshot run in inventory")
            RESTORE._private_input(source, label="snapshot run", directory=True)
            doc, original = _private_manifest(source / "manifest.json")
            if (type(doc.get("schemaVersion")) is not int or doc["schemaVersion"] != 1
                    or doc.get("type") != "stopped-compose-volume-snapshot"
                    or doc.get("clientProject") != project
                    or doc.get("ageRecipientSha256") != old_fingerprint):
                raise RotationError("snapshot tenant or old-recipient binding mismatch")
            verified = RESTORE.verify(compose, project, source, old_identity)
            if not verified:
                raise RotationError("snapshot has no verified volumes")
            # An operator swapping a manifest during decryption cannot cause a
            # successful rotation review for a different retained archive.
            _, after = _private_manifest(source / "manifest.json")
            if after != original:
                raise RotationError("snapshot manifest changed during verification")
            total_volumes += len(verified)
        return {
            "status": "review-required-not-cloud-proof",
            "clientProject": project,
            "oldRecipientSha256": old_fingerprint,
            "proposedRecipientSha256": new_fingerprint,
            "verifiedLocalSnapshots": len(entries),
            "verifiedLocalVolumes": total_volumes,
            "retainOldIdentity": True,
            "cloudInventoryComplete": False,
            "liveRestoreProven": False,
            "policyUpdated": False,
        }
    except (RESTORE.RestoreError, RESTORE.SNAPSHOT.SnapshotError, OSError, ValueError) as exc:
        raise RotationError("retained snapshot or identity verification failed") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", type=pathlib.Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--snapshots", type=pathlib.Path, required=True)
    parser.add_argument("--old-recipient", required=True)
    parser.add_argument("--new-recipient", required=True)
    parser.add_argument("--old-identity", type=pathlib.Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = plan_rotation(compose=args.compose, project=args.project,
                               snapshots=args.snapshots, old_recipient=args.old_recipient,
                               new_recipient=args.new_recipient, old_identity=args.old_identity)
    except (RotationError, OSError, ValueError):
        # Never echo paths, age material, ciphertext, manifest content or subprocess stderr.
        print(json.dumps({"event": "backup.rotation.blocked", "status": "unverified"}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

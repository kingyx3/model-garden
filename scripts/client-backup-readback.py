#!/usr/bin/env python3
"""Read back a completed encrypted GCS backup using a separate read-only identity.

No write, decryption or Docker permission is needed for ciphertext verification.
Gcloud must impersonate the exact Terraform-derived, bucket-scoped reader; the
caller needs separate client-approved permission to impersonate it. This is NOT
a recovery drill or proof of signed backup provenance.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("model_garden_readback_upload", ROOT / "scripts" / "client-backup-upload.py")
UPLOAD = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(UPLOAD)
RESTORE = UPLOAD.RESTORE
SNAPSHOT = RESTORE.SNAPSHOT
MAX_ARCHIVE_BYTES = 100 * 1024**3


class ReadbackError(ValueError):
    pass


def expected_reader_identity(gcp_project: str, client_slug: str, environment: str) -> str:
    """Match the Terraform read-only recovery principal for one tenant/environment."""
    try:
        UPLOAD.expected_bucket(gcp_project, client_slug, environment)
    except UPLOAD.UploadError as exc:
        raise ReadbackError("invalid client GCP recovery reader binding") from exc
    import hashlib
    name = f"{client_slug}-{environment}-mg"
    account_id = "mg-bkr-" + hashlib.md5(name.encode("ascii"), usedforsecurity=False).hexdigest()[:8]
    return f"{account_id}@{gcp_project}.iam.gserviceaccount.com"


def _run_gcloud(args: list[str], *, timeout: int, reader_identity: str) -> str:
    try:
        result = subprocess.run(["gcloud", f"--impersonate-service-account={reader_identity}", *args, "--quiet"], capture_output=True,
                                text=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReadbackError("GCS read-back unavailable or timed out") from exc
    if result.returncode:
        raise ReadbackError("GCS object unavailable or access denied; backup not verified")
    return result.stdout


def _cloud_size(uri: str, *, reader_identity: str) -> int:
    raw = _run_gcloud(["storage", "objects", "describe", uri,
                       "--format=json(size)"], timeout=120, reader_identity=reader_identity)
    try:
        size = json.loads(raw)["size"]
        if isinstance(size, bool) or not isinstance(size, (int, str)):
            raise ValueError("unexpected size")
        number = int(size)
        if number < 1 or number > MAX_ARCHIVE_BYTES:
            raise ValueError("invalid object size")
        return number
    except (ValueError, KeyError, TypeError) as exc:
        raise ReadbackError("GCS object size metadata is missing or invalid") from exc


def _cloud_copy(uri: str, target: pathlib.Path, *, reader_identity: str) -> None:
    _run_gcloud(["storage", "cp", uri, str(target)], timeout=3600, reader_identity=reader_identity)
    if target.is_symlink() or not target.is_file():
        raise ReadbackError("GCS download did not create a safe regular file")
    target.chmod(0o600)


def _entries(doc: Any, compose: pathlib.Path, project: str) -> list[dict[str, Any]]:
    if not isinstance(doc, dict) or doc.get("schemaVersion") != 1 or \
       doc.get("type") != "stopped-compose-volume-snapshot" or doc.get("clientProject") != project:
        raise ReadbackError("snapshot manifest schema or client binding is invalid")
    if not isinstance(doc.get("ageRecipientSha256"), str) or \
       not RESTORE.SHA256.fullmatch(doc["ageRecipientSha256"]):
        raise ReadbackError("invalid snapshot recipient fingerprint")
    expected = SNAPSHOT.inventory(compose)
    rows = doc.get("volumes")
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise ReadbackError("incomplete cloud backup volume inventory")
    names: set[str] = set()
    for entry in rows:
        if not isinstance(entry, dict):
            raise ReadbackError("invalid cloud backup manifest entry")
        name, size, digest = entry.get("volume"), entry.get("bytes"), entry.get("sha256")
        if not isinstance(name, str) or name not in expected or name in names or \
           entry.get("object") != f"{name}.tar.age":
            raise ReadbackError("wrong, duplicate or unsafe cloud backup object")
        if isinstance(size, bool) or not isinstance(size, int) or not (0 < size <= MAX_ARCHIVE_BYTES) or \
           not isinstance(digest, str) or not RESTORE.SHA256.fullmatch(digest):
            raise ReadbackError("invalid cloud backup object integrity metadata")
        names.add(name)
    return sorted(rows, key=lambda entry: entry["volume"])


def _verify_download(path: pathlib.Path, size: int, digest: str | None = None) -> None:
    if path.is_symlink() or not path.is_file():
        raise ReadbackError("unsafe or missing downloaded backup file")
    measured, bytes_received = SNAPSHOT._digest(path)
    if bytes_received != size or (digest is not None and measured != digest):
        raise ReadbackError("cloud read-back integrity mismatch")


def retrieve(*, compose: pathlib.Path, project: str, destination: pathlib.Path,
             gcp_project: str, client_slug: str, environment: str, run_id: str,
             identity: pathlib.Path | None = None) -> dict[str, Any]:
    try:
        bucket = UPLOAD.expected_bucket(gcp_project, client_slug, environment)
    except UPLOAD.UploadError as exc:
        raise ReadbackError("invalid client GCP backup binding") from exc
    reader_identity = expected_reader_identity(gcp_project, client_slug, environment)
    if project != f"{client_slug}-{environment}" or \
       not UPLOAD.RUN_ID.fullmatch(run_id) or not run_id.startswith(project + "-"):
        raise ReadbackError("snapshot run is not bound to the exact client and environment")
    # Inventory is verified before touching the cloud; do not accept arbitrary
    # destinations or a cloud-supplied filename/path.
    SNAPSHOT.inventory(compose)
    parent = destination.parent
    if parent.is_symlink() or not parent.is_dir() or parent.stat().st_mode & 0o077:
        raise ReadbackError("destination parent must be a private existing directory")
    if destination.exists() or destination.is_symlink():
        raise ReadbackError("destination must not exist; never overwrite local backup material")
    if identity is not None:
        RESTORE._private_identity(identity)
    if shutil.which("gcloud") is None:
        raise ReadbackError("gcloud not available for authorized read-back")
    prefix = f"gs://{bucket}/v1/{project}/{run_id}/"
    # Size-bound the untrusted manifest BEFORE downloading it.
    manifest_size = _cloud_size(prefix + "manifest.json", reader_identity=reader_identity)
    if manifest_size > RESTORE.MAX_MANIFEST_BYTES:
        raise ReadbackError("cloud backup manifest exceeds permitted size")
    previous_umask = os.umask(0o077)
    try:
        destination.mkdir(mode=0o700)
        pending = destination / ".manifest.partial"
        _cloud_copy(prefix + "manifest.json", pending, reader_identity=reader_identity)
        _verify_download(pending, manifest_size)
        try:
            doc = json.loads(pending.read_text(encoding="utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise ReadbackError("cloud backup manifest is malformed") from exc
        rows = _entries(doc, compose, project)
        for row in rows:
            uri = prefix + row["object"]
            remote_size = _cloud_size(uri, reader_identity=reader_identity)
            if remote_size != row["bytes"]:
                raise ReadbackError("cloud object size differs from signed-off snapshot inventory")
            target = destination / row["object"]
            _cloud_copy(uri, target, reader_identity=reader_identity)
            _verify_download(target, row["bytes"], row["sha256"])
        # Commit the manifest last; an interrupted download must not masquerade
        # as a complete recovery candidate.
        manifest = destination / "manifest.json"
        pending.rename(manifest)
        try:
            verified = RESTORE._validated_manifest(compose, project, destination)
            if identity is not None:
                RESTORE.verify(compose, project, destination, identity)
        except (RESTORE.RestoreError, SNAPSHOT.SnapshotError, OSError, ValueError) as exc:
            manifest.unlink(missing_ok=True)
            raise ReadbackError("retrieved snapshot failed independent verification") from exc
        return {
            "event": "backup.readback.verified",
            "clientProject": project,
            "volumes": len(verified),
            "status": ("cloud-ciphertext-and-age-verified-not-recovery-proven"
                       if identity is not None else "cloud-ciphertext-verified-not-recovery-proven"),
        }
    finally:
        os.umask(previous_umask)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", required=True, type=pathlib.Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--destination", required=True, type=pathlib.Path)
    parser.add_argument("--gcp-project-id", required=True)
    parser.add_argument("--client-slug", required=True)
    parser.add_argument("--environment", required=True, choices=sorted(UPLOAD.ENVIRONMENTS))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--identity", type=pathlib.Path, help="optional offline/private age key to authenticate archives")
    args = parser.parse_args(argv)
    try:
        status = retrieve(compose=args.compose, project=args.project,
                          destination=args.destination, gcp_project=args.gcp_project_id,
                          client_slug=args.client_slug, environment=args.environment,
                          run_id=args.run_id, identity=args.identity)
    except (ReadbackError, UPLOAD.UploadError, RESTORE.RestoreError,
            SNAPSHOT.SnapshotError, OSError, ValueError) as exc:
        # Caller-controlled source, path and cloud exception text are not printed.
        print(json.dumps({"event": "backup.readback.failed",
                          "status": "unverified", "clientProject": args.project}), file=sys.stderr)
        return 2
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

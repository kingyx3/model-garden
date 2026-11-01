#!/usr/bin/env python3
"""Operator-initiated, create-only upload of a complete encrypted client snapshot.

The manifest is published LAST as the completion marker. This does not schedule
snapshots, run a cloud recovery drill, or grant a client new cloud permissions.
Authenticate gcloud as the approved client backup writer outside this program.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import pathlib
import re
import shutil
import subprocess
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("model_garden_upload_restore", ROOT / "scripts" / "client-volume-restore.py")
RESTORE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(RESTORE)

SLUG = re.compile(r"^[a-z][a-z0-9-]{0,30}[a-z0-9]$")
GCP_PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")
ENVIRONMENTS = {"dev", "uat", "prod"}


class UploadError(ValueError):
    pass


def expected_bucket(gcp_project: str, client_slug: str, environment: str) -> str:
    """Match infra/docker-host/gcp/runtime_backups.tf exactly; no arbitrary bucket."""
    if not GCP_PROJECT.fullmatch(gcp_project):
        raise UploadError("invalid GCP project ID")
    if not SLUG.fullmatch(client_slug) or environment not in ENVIRONMENTS:
        raise UploadError("invalid client slug or environment")
    prefix = hashlib.md5(gcp_project.encode("ascii"), usedforsecurity=False).hexdigest()[:10]
    return f"{prefix}-{client_slug}-{environment}-mg-backups"


def expected_writer_identity(gcp_project: str, client_slug: str, environment: str) -> str:
    """Match the opt-in Terraform backup writer; never accept a caller-selected principal."""
    expected_bucket(gcp_project, client_slug, environment)  # same validated tenant/cloud boundary
    name = f"{client_slug}-{environment}-mg"
    account_id = "mg-bkp-" + hashlib.md5(name.encode("ascii"), usedforsecurity=False).hexdigest()[:8]
    return f"{account_id}@{gcp_project}.iam.gserviceaccount.com"


def _private(path: pathlib.Path, *, is_dir: bool) -> None:
    if path.is_symlink() or (not path.is_dir() if is_dir else not path.is_file()):
        raise UploadError("backup input is missing or a symlink")
    if path.stat().st_mode & 0o077:
        raise UploadError("backup input must be private (mode 0700 directory / 0600 file)")


def _snapshot_files(compose: pathlib.Path, project: str, source: pathlib.Path) -> list[pathlib.Path]:
    _private(source, is_dir=True)
    manifest = source / "manifest.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise UploadError("backup manifest is missing or unsafe")
    _private(manifest, is_dir=False)
    try:
        volumes = RESTORE._validated_manifest(compose, project, source)
    except (RESTORE.RestoreError, RESTORE.SNAPSHOT.SnapshotError, OSError, ValueError) as exc:
        raise UploadError("backup integrity verification failed") from exc
    for _name, path in volumes:
        _private(path, is_dir=False)
    return [path for _name, path in volumes] + [manifest]


def _fingerprint(path: pathlib.Path) -> tuple[str, int]:
    # Reuse the independent restore-side digest so the same encrypted integrity
    # contract governs both upload and recovery.
    _private(path, is_dir=False)
    return RESTORE.SNAPSHOT._digest(path)


def _gcloud_copy(path: pathlib.Path, dest: str, *, writer_identity: str) -> None:
    # Explicit scoped impersonation prevents an ambient operator/runtime gcloud
    # identity from performing this write. Generation 0 is atomic create-only.
    # Suppress provider output: it may contain client paths, metadata, or secrets.
    command = [
        "gcloud", f"--impersonate-service-account={writer_identity}",
        "storage", "cp", str(path), dest,
        "--if-generation-match=0", "--quiet",
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=3600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UploadError("cloud upload failed or timed out") from exc
    if result.returncode:
        raise UploadError("cloud upload failed; object may already exist or credentials may lack access")


def upload(
    *, compose: pathlib.Path, project: str, source: pathlib.Path,
    gcp_project: str, client_slug: str, environment: str,
) -> dict[str, Any]:
    bucket = expected_bucket(gcp_project, client_slug, environment)
    writer_identity = expected_writer_identity(gcp_project, client_slug, environment)
    if project != f"{client_slug}-{environment}":
        raise UploadError("client project does not match the intended bucket owner")
    if not RUN_ID.fullmatch(source.name) or not source.name.startswith(project + "-"):
        raise UploadError("snapshot run directory must be client-scoped and uniquely named")
    files = _snapshot_files(compose, project, source)
    if shutil.which("gcloud") is None:
        raise UploadError("gcloud CLI not installed; no alternative insecure upload")
    # Pin the verified files (including manifest) before touching cloud.
    baselines = {path.name: _fingerprint(path) for path in files}
    # Manifest must stay LAST: a reader should treat absence as an incomplete
    # snapshot, and verify all file digests independently during recovery.
    root = f"gs://{bucket}/v1/{project}/{source.name}/"
    for path in files:
        if _fingerprint(path) != baselines[path.name]:
            raise UploadError("backup integrity changed during upload")
        _gcloud_copy(path, root + path.name, writer_identity=writer_identity)
        if _fingerprint(path) != baselines[path.name]:
            raise UploadError("backup integrity changed during upload")
    return {
        "event": "backup.upload.completed",
        "clientProject": project,
        "bucket": bucket,
        "objects": len(files),
        "status": "uploaded-not-recovery-proven",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", type=pathlib.Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--source", type=pathlib.Path, required=True)
    parser.add_argument("--gcp-project-id", required=True)
    parser.add_argument("--client-slug", required=True)
    parser.add_argument("--environment", choices=sorted(ENVIRONMENTS), required=True)
    args = parser.parse_args(argv)
    try:
        result = upload(
            compose=args.compose, project=args.project, source=args.source,
            gcp_project=args.gcp_project_id, client_slug=args.client_slug,
            environment=args.environment,
        )
    except (UploadError, OSError, ValueError) as exc:
        # No client content, underlying cloud stdout/stderr or credentials in
        # failure event. The operator can inspect separate restricted audit logs.
        print(json.dumps({
            "event": "backup.upload.failed",
            "clientProject": args.project,
            "reason": str(exc) if isinstance(exc, UploadError) else "backup verification failed",
        }, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

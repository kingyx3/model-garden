#!/usr/bin/env python3
"""Fail-closed structural gate for client-bound production acceptance evidence.

This tool checks submitted artifacts, not cloud/provider truth. A passing report
means "ready for independent review", never "production approved".
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import stat
import sys

ID = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
SHA = re.compile(r"^[0-9a-f]{64}$")
RELEASE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
REQUIRED = (
    "keyless-redeploy", "credential-revocation", "rollback", "model-turn",
    "governed-business-action", "calendar-transaction", "inbound-voice",
    "human-fallback", "monitoring-alert", "emergency-pause",
    "encrypted-backup", "independent-restore", "audit-export",
    "client-isolation", "cost-attribution",
)
MAX_AGE = dt.timedelta(days=30)
MAX_BYTES = 2 * 1024 * 1024


def _time(value: object) -> dt.datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO-8601 string")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(dt.timezone.utc)


def _open_root(root: pathlib.Path) -> int:
    """Pin one owner-private evidence directory for manifest and all artifacts."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(root, flags)
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o022):
        os.close(fd)
        raise ValueError("evidence directory must be owner-controlled and not writable by others")
    return fd


def _file(root_fd: int, name: object) -> bytes:
    """Read a pinned, single-link regular file; never follow a substituted path."""
    if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", name):
        raise ValueError("artifact must be a single safe basename")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                 dir_fd=root_fd)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("artifact must be a single-link regular file")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError("artifact must not be group/world writable")
        if info.st_size == 0 or info.st_size > MAX_BYTES:
            raise ValueError("artifact size invalid")
        data = stream.read(MAX_BYTES + 1)
        if len(data) != info.st_size:
            raise ValueError("artifact changed during bounded read")
        return data


def check(manifest: dict, root: pathlib.Path, *, client: str, environment: str,
          project: str, release: str, now: dt.datetime | None = None,
          root_fd: int | None = None) -> dict:
    """Check one pinned evidence snapshot; never promote submitted claims to truth."""
    if root_fd is None:
        try:
            fd = _open_root(root)
        except (OSError, ValueError):
            return {"status": "blocked", "errors": ["unsafe evidence directory"],
                    "reviewRequired": True}
        try:
            return check(manifest, root, client=client, environment=environment,
                         project=project, release=release, now=now, root_fd=fd)
        finally:
            os.close(fd)
    errors: list[str] = []
    now = now or dt.datetime.now(dt.timezone.utc)
    if not all(isinstance(v, str) and ID.fullmatch(v) for v in (client, environment, project)):
        return {"status": "blocked", "errors": ["invalid expected identity"]}
    if not isinstance(release, str) or not RELEASE.fullmatch(release):
        return {"status": "blocked", "errors": ["release must be a pinned MAJOR.MINOR.PATCH"]}
    expected = {"schemaVersion": 1, "client": client, "environment": environment,
                "gcpProject": project, "release": release}
    for key, value in expected.items():
        if type(manifest.get(key)) is not type(value) or manifest.get(key) != value:
            errors.append("manifest identity mismatch: " + key)
    if set(manifest) != set(expected) | {"evidence"}:
        errors.append("unknown or missing manifest fields")
    records = manifest.get("evidence")
    if not isinstance(records, list):
        records = []
        errors.append("evidence must be an array")
    seen: set[str] = set()
    used_artifacts: set[str] = set()
    for index, row in enumerate(records):
        prefix = f"evidence[{index}]"
        if not isinstance(row, dict):
            errors.append(prefix + ": invalid record")
            continue
        kind = row.get("kind")
        if kind not in REQUIRED or kind in seen:
            errors.append(prefix + ": unknown or duplicate kind")
            continue
        seen.add(kind)
        artifact_name = row.get("artifact")
        if isinstance(artifact_name, str):
            if artifact_name in used_artifacts:
                errors.append(prefix + ": artifact reused for multiple controls")
            used_artifacts.add(artifact_name)
        if set(row) != {"kind", "observedAt", "artifact", "sha256", "source", "result"}:
            errors.append(prefix + ": unexpected or missing fields")
        if row.get("source") != "live" or row.get("result") != "passed":
            errors.append(prefix + ": simulation, missing or failed result")
        try:
            observed = _time(row.get("observedAt"))
            if observed > now + dt.timedelta(minutes=5) or now - observed > MAX_AGE:
                errors.append(prefix + ": stale or future evidence")
        except ValueError as exc:
            errors.append(prefix + ": " + str(exc))
        try:
            digest = row.get("sha256")
            if not isinstance(digest, str) or not SHA.fullmatch(digest):
                raise ValueError("invalid sha256")
            actual = hashlib.sha256(_file(root_fd, row.get("artifact"))).hexdigest()
            if actual != digest:
                raise ValueError("artifact digest mismatch")
        except (OSError, ValueError) as exc:
            errors.append(prefix + ": " + str(exc))
    for kind in REQUIRED:
        if kind not in seen:
            errors.append("missing evidence: " + kind)
    return {
        "status": "blocked" if errors else "review_required",
        "errors": sorted(errors),
        "reviewRequired": True,
        "claim": "Structural completeness only; independent live-provider, identity and authorization verification required.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=pathlib.Path)
    parser.add_argument("--client", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--gcp-project", required=True)
    parser.add_argument("--release", required=True)
    args = parser.parse_args()
    try:
        root_fd = _open_root(args.manifest.parent)
        try:
            value = json.loads(_file(root_fd, args.manifest.name).decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("manifest must be an object")
            result = check(value, args.manifest.parent, client=args.client,
                           environment=args.environment, project=args.gcp_project,
                           release=args.release, root_fd=root_fd)
        finally:
            os.close(root_fd)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        result = {"status": "blocked", "errors": ["invalid manifest: " + str(exc)], "reviewRequired": True}
    print(json.dumps(result, sort_keys=True))
    # 0 is deliberately never an automatic production approval.
    return 2 if result["status"] == "blocked" else 3


if __name__ == "__main__":
    raise SystemExit(main())

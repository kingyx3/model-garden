#!/usr/bin/env python3
"""Execute one explicitly approved, bounded, stopped-client encrypted backup (#230).

No scheduler, service stop/start, cloud privilege escalation, retry, key storage or
recovery proof. The deployment freeze must be enforced outside this process.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import re
import secrets
import signal
import stat
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import client_operation_lock
SLUG = re.compile(r"^[a-z][a-z0-9-]{0,30}[a-z0-9]$")
GCP_PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
AGE_RECIPIENT = re.compile(r"^age1[a-z0-9]{50,}$")
IMAGE = re.compile(r"^[a-zA-Z0-9_.:/-]+@sha256:[0-9a-f]{64}$")
REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{2,127}$")
ENVIRONMENTS = {"dev", "uat", "prod"}
MAX_WINDOW_SECONDS = 3600
MIN_REMAINING_SECONDS = 30
MAX_PROJECT_CONTAINERS = 64
CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
INSPECT_FORMAT = '{{.Id}}|{{.State.Status}}|{{.State.Running}}|{{.HostConfig.RestartPolicy.Name}}|{{index .Config.Labels "com.docker.compose.project"}}'


class WindowError(ValueError):
    pass


def _private_dir(path: pathlib.Path) -> None:
    if path.is_symlink() or not path.is_dir() or path.stat().st_mode & 0o077:
        raise WindowError("approval and backup directories must exist and be private (0700)")


def _private_json(path: pathlib.Path) -> dict:
    _private_dir(path.parent)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise WindowError("approval file is unavailable or unsafe") from exc
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096:
            raise WindowError("approval file must be regular, private (0600) and bounded")
        try:
            doc = json.loads(stream.read(4097).decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise WindowError("approval file is invalid") from exc
    if not isinstance(doc, dict):
        raise WindowError("approval must be a JSON object")
    return doc


def _utc(value: str) -> dt.datetime:
    if not isinstance(value, str):
        raise WindowError("approval times must be UTC seconds with Z suffix")
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise WindowError("approval times must be UTC seconds with Z suffix") from exc


def _approved(approval: pathlib.Path, *, project: str, slug: str, environment: str,
              gcp_project: str, recipient: str, now: dt.datetime) -> float:
    if not SLUG.fullmatch(slug) or environment not in ENVIRONMENTS or \
       not GCP_PROJECT.fullmatch(gcp_project) or project != f"{slug}-{environment}" or \
       not AGE_RECIPIENT.fullmatch(recipient):
        raise WindowError("invalid or mismatched client/environment/cloud/age binding")
    doc = _private_json(approval)
    required = {"schemaVersion", "clientProject", "clientSlug", "environment", "gcpProjectId",
                "ageRecipientSha256", "approvedBy", "changeRef", "validFrom", "expiresAt"}
    if set(doc) != required or type(doc["schemaVersion"]) is not int or doc["schemaVersion"] != 1:
        raise WindowError("unsupported or incomplete approval schema")
    if (doc["clientProject"], doc["clientSlug"], doc["environment"], doc["gcpProjectId"]) != \
       (project, slug, environment, gcp_project):
        raise WindowError("approval is for a different client/environment/cloud project")
    if doc["ageRecipientSha256"] != hashlib.sha256(recipient.encode("ascii")).hexdigest():
        raise WindowError("age recipient is not the approved key")
    if not isinstance(doc["approvedBy"], str) or not REFERENCE.fullmatch(doc["approvedBy"]) or \
       not isinstance(doc["changeRef"], str) or not REFERENCE.fullmatch(doc["changeRef"]):
        raise WindowError("named operator approval and change reference are required")
    start, end = _utc(doc["validFrom"]), _utc(doc["expiresAt"])
    duration = (end - start).total_seconds()
    remaining = (end - now).total_seconds()
    if not 0 < duration <= MAX_WINDOW_SECONDS or not start <= now < end or \
       remaining < MIN_REMAINING_SECONDS:
        raise WindowError("approved backup window is expired, not active or too long")
    return remaining


def _lock(parent: pathlib.Path, project: str) -> int:
    name = parent / f".{project}.backup.lock"
    try:
        fd = os.open(name, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise WindowError("backup lock is not a private regular file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (OSError, BlockingIOError) as exc:
        if "fd" in locals():
            os.close(fd)
        raise WindowError("another backup is active or the lock is unsafe") from exc
    except WindowError:
        os.close(fd)
        raise


def _run(args: list[str], timeout: float, *, capture: bool = False) -> str:
    if timeout < 1:
        raise WindowError("approved backup deadline reached")
    try:
        with subprocess.Popen(args, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, start_new_session=True) as process:
            try:
                stdout, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                raise WindowError("backup step exceeded approved deadline") from exc
            if process.returncode:
                raise WindowError("backup step failed; see restricted operator diagnostics")
            return (stdout or b"").decode("utf-8", errors="replace") if capture else ""
    except OSError as exc:
        raise WindowError("required backup executable unavailable") from exc


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining < 1:
        raise WindowError("approved backup deadline reached")
    return remaining


def _stopped(project: str, deadline: float) -> None:
    """Refuse live, restarting, mislabelled or uninspectable project containers.

    This read-only guard checks every existing Compose-labelled container, not
    just running ones. It is not an atomic cross-host deployment freeze.
    """
    output = _run(["docker", "ps", "--all", "--no-trunc",
                   "--filter", f"label=com.docker.compose.project={project}",
                   "--format", "{{.ID}}"], min(20, _remaining(deadline)), capture=True)
    ids = output.splitlines()
    if len(ids) > MAX_PROJECT_CONTAINERS or len(set(ids)) != len(ids) or \
       any(not CONTAINER_ID.fullmatch(cid) for cid in ids):
        raise WindowError("Docker project container inventory is invalid or unbounded")
    for cid in ids:
        # Select only safe fields; never read Config.Env or Docker credentials.
        fields = _run(["docker", "inspect", "--type", "container", "--format",
                       INSPECT_FORMAT, cid], min(20, _remaining(deadline)),
                      capture=True).strip().split("|")
        if len(fields) != 5 or fields[0] != cid or fields[4] != project:
            raise WindowError("Docker container inspection or tenant binding failed")
        if fields[1] not in ("created", "exited") or fields[2] != "false":
            raise WindowError("client containers are running or not safely stopped")
        if fields[3] != "no":
            raise WindowError("client container automatic restart policy must be disabled")


def run(*, compose: pathlib.Path, project: str, slug: str, environment: str,
        gcp_project: str, recipient: str, image: str, approval: pathlib.Path,
        output_parent: pathlib.Path, confirm_project: str,
        not_after: dt.datetime | None = None) -> dict:
    if confirm_project != project or not IMAGE.fullmatch(image):
        raise WindowError("explicit exact-project confirmation and pinned helper image are required")
    if compose.is_symlink() or not compose.is_file() or compose.stat().st_mode & 0o022:
        raise WindowError("Compose input must be a regular, non-writable-by-others file")
    _private_dir(output_parent)
    remaining = _approved(approval, project=project, slug=slug, environment=environment,
                          gcp_project=gcp_project, recipient=recipient,
                          now=dt.datetime.now(dt.timezone.utc))
    # A scheduled occurrence cannot borrow time from its longer per-slot
    # approval after the independently authorized recurrence has expired.
    # Use a monotonic deadline so wall-clock changes cannot extend work.
    if not_after is not None:
        if not isinstance(not_after, dt.datetime) or not_after.tzinfo is None or \
           not_after.utcoffset() is None:
            raise WindowError("scheduled authorization deadline must be timezone-aware")
        remaining = min(remaining, (not_after - dt.datetime.now(dt.timezone.utc)).total_seconds())
        if remaining < MIN_REMAINING_SECONDS:
            raise WindowError("scheduled authorization deadline is too close or expired")
    deadline = time.monotonic() + remaining
    with client_operation_lock.project_operation_lock(project):
        lock_fd = _lock(output_parent, project)
        try:
            _stopped(project, deadline)
            snapshot_script = ROOT / "scripts" / "client-volume-snapshot.py"
            upload_script = ROOT / "scripts" / "client-backup-upload.py"
            _run([sys.executable, str(snapshot_script), "inventory", "--compose", str(compose),
                  "--project", project], _remaining(deadline))
            _stopped(project, deadline)
            run_id = f"{project}-{int(time.time())}-{secrets.token_hex(5)}"
            destination = output_parent / run_id
            _run([sys.executable, str(snapshot_script), "snapshot", "--compose", str(compose),
                  "--project", project, "--output", str(destination), "--recipient", recipient,
                  "--image", image], _remaining(deadline))
            _stopped(project, deadline)
            _run([sys.executable, str(upload_script), "--compose", str(compose), "--project", project,
                  "--source", str(destination), "--gcp-project-id", gcp_project,
                  "--client-slug", slug, "--environment", environment], _remaining(deadline))
            _stopped(project, deadline)
            _remaining(deadline)
            return {"event": "backup.window.completed", "clientProject": project,
                    "status": "uploaded-not-recovery-proven", "runId": run_id}
        finally:
            os.close(lock_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("compose", "project", "client-slug", "environment", "gcp-project-id",
                 "recipient", "image", "approval", "output-parent", "confirm-project"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    try:
        result = run(compose=pathlib.Path(args.compose), project=args.project,
                     slug=args.client_slug, environment=args.environment,
                     gcp_project=args.gcp_project_id, recipient=args.recipient,
                     image=args.image, approval=pathlib.Path(args.approval),
                     output_parent=pathlib.Path(args.output_parent), confirm_project=args.confirm_project)
    except (WindowError, OSError, ValueError) as exc:
        # No cloud stderr, payloads, credentials, recipient, paths or approval details.
        print(json.dumps({"event": "backup.window.failed", "status": "not-recovery-proven",
                          "reason": "window-refused" if isinstance(exc, (WindowError, client_operation_lock.OperationLockError)) else "unexpected-failure"},
                         sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

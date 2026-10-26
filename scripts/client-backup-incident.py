#!/usr/bin/env python3
"""Durable, tenant-bound backup incident transitions for approved host monitoring.

Emits redacted OPENED/RECOVERED transitions; does not deliver notifications,
retry backups, or prove recovery. A separately authorized supervisor is needed.
"""
from __future__ import annotations

import argparse
import datetime as dt
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

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "client_backup_audit", ROOT / "scripts" / "client-backup-audit.py"
)
AUDIT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(AUDIT)
SCHEDULE = AUDIT.SCHEDULE
STAMP = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")
GOOD = "uploaded-not-recovery-proven"
BAD = {"missed", "unknown", "failed-not-recovery-proven"}
MAX_STATE_BYTES = 4096


class IncidentError(ValueError):
    pass


def _binding(*, project: str, slug: str, environment: str,
             gcp_project: str, recipient: str) -> str:
    if (not SCHEDULE.WINDOW.SLUG.fullmatch(slug) or
            environment not in SCHEDULE.WINDOW.ENVIRONMENTS or
            project != f"{slug}-{environment}" or
            not SCHEDULE.WINDOW.GCP_PROJECT.fullmatch(gcp_project) or
            not SCHEDULE.WINDOW.AGE_RECIPIENT.fullmatch(recipient)):
        raise IncidentError("invalid tenant binding")
    fields = [project, slug, environment, gcp_project,
              hashlib.sha256(recipient.encode("ascii")).hexdigest()]
    return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()


def _lock(directory: pathlib.Path, project: str) -> int:
    path = directory / f"{project}.incident.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) |
                 getattr(os, "O_NONBLOCK", 0), 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise IncidentError("unsafe incident lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (OSError, IncidentError):
        os.close(fd)
        raise


def _state(path: pathlib.Path, binding: str) -> dict | None:
    value = AUDIT._record(path)
    if value is AUDIT.UNKNOWN:
        raise IncidentError("unsafe incident state")
    if value is None:
        return None
    if (not {"schemaVersion", "binding", "condition", "lastSlot"} <= set(value) or
            set(value) - {"schemaVersion", "binding", "condition", "lastSlot"} -
            {"pendingRecoverySlot"} or
            type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1 or
            value["binding"] != binding or
            value["condition"] not in ("healthy", "unhealthy") or
            not isinstance(value["lastSlot"], str) or
            not STAMP.fullmatch(value["lastSlot"])):
        raise IncidentError("invalid or cross-tenant incident state")
    try:
        dt.datetime.strptime(value["lastSlot"], "%Y%m%dT%H%M%SZ")
        pending = value.get("pendingRecoverySlot")
        if pending is not None:
            if (value["condition"] != "healthy" or not isinstance(pending, str) or
                    not STAMP.fullmatch(pending) or pending > value["lastSlot"]):
                raise IncidentError("invalid pending recovery notification")
            dt.datetime.strptime(pending, "%Y%m%dT%H%M%SZ")
    except ValueError as exc:
        raise IncidentError("invalid incident slot") from exc
    return value


def _persist(path: pathlib.Path, value: dict) -> None:
    """Atomic owner-private state update while the per-client lock is held."""
    directory = path.parent
    fd, temporary = tempfile.mkstemp(prefix=".backup-incident-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(value, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Recheck before replace; never overwrite a newly introduced unsafe file.
        existing = AUDIT._record(path)
        if existing is AUDIT.UNKNOWN:
            raise IncidentError("unsafe incident state")
        os.replace(temporary, path)
        dirfd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
                        getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def monitor(*, schedule: pathlib.Path, claims: pathlib.Path,
            state_dir: pathlib.Path, project: str, slug: str, environment: str,
            gcp_project: str, recipient: str, confirm_project: str,
            since: dt.datetime, now: dt.datetime | None = None) -> dict:
    binding = _binding(project=project, slug=slug, environment=environment,
                       gcp_project=gcp_project, recipient=recipient)
    report = AUDIT.audit(schedule=schedule, claims=claims, project=project, slug=slug,
                         environment=environment, gcp_project=gcp_project,
                         recipient=recipient, confirm_project=confirm_project,
                         since=since, now=now)
    SCHEDULE._private_owned(state_dir, directory=True)
    lock_fd = _lock(state_dir, project)
    try:
        path = state_dir / f"{project}.incident.json"
        prior = _state(path, binding)
        slots = report["slots"]
        for slot in slots:
            if (set(slot) != {"slotUTC", "status"} or
                    not isinstance(slot["slotUTC"], str) or
                    not STAMP.fullmatch(slot["slotUTC"]) or
                    slot["status"] not in BAD | {GOOD}):
                raise IncidentError("invalid audit slot")
        if prior is not None:
            last = dt.datetime.strptime(prior["lastSlot"], "%Y%m%dT%H%M%SZ").replace(
                tzinfo=dt.timezone.utc)
            if since.astimezone(dt.timezone.utc) > last:
                raise IncidentError("monitoring interval has an unobserved gap")
        if not slots:
            return {"event": "backup.incident.indeterminate", "clientProject": project,
                    "incidentActive": prior is not None and prior["condition"] == "unhealthy",
                    "recoveryProven": False, "notificationsDelivered": False}
        latest = max(slot["slotUTC"] for slot in slots)
        if prior is not None and latest < prior["lastSlot"]:
            raise IncidentError("stale monitoring interval")
        fresh = [slot for slot in slots if prior is None or
                 slot["slotUTC"] > prior["lastSlot"]]
        if fresh:
            condition = "healthy" if all(slot["status"] == GOOD for slot in fresh) else "unhealthy"
        else:
            # Only the latest observed slot may deteriorate on a repeated poll.
            # Older missed slots are already accounted for by a newer successful
            # recovery; re-reading them must not reopen a resolved incident.
            # Same-slot receipt changes still cannot clear an active incident.
            latest_bad = any(slot["slotUTC"] == latest and slot["status"] in BAD
                             for slot in slots)
            condition = "unhealthy" if (prior["condition"] == "unhealthy" or
                                      latest_bad) else "healthy"
        previous = prior["condition"] if prior is not None else "healthy"
        event = ("backup.incident.opened" if condition == "unhealthy" and previous == "healthy"
                 else "backup.incident.recovered" if condition == "healthy" and
                      previous == "unhealthy" else "backup.incident.unchanged")
        # Persist a recovery delivery outbox atomically with the incident
        # transition. The Cloud Logging command may fail or the host may crash
        # after this point; only an acknowledged write may clear the outbox.
        pending = (latest if event == "backup.incident.recovered" else
                   prior.get("pendingRecoverySlot") if prior is not None and
                   condition == "healthy" else None)
        if prior is None or latest > prior["lastSlot"] or condition != previous:
            next_state = {"schemaVersion": 1, "binding": binding,
                          "condition": condition, "lastSlot": latest}
            if pending is not None:
                next_state["pendingRecoverySlot"] = pending
            _persist(path, next_state)
        result = {"event": event, "clientProject": project, "slotUTC": latest,
                "incidentActive": condition == "unhealthy",
                "severity": "ERROR" if condition == "unhealthy" else "INFO",
                "operatorAction": ("inspect-backup-claims-and-receipts" if
                                   condition == "unhealthy" else "verify-upload-and-restore"),
                "recoveryProven": False, "notificationsDelivered": False}
        if pending is not None:
            result["pendingRecoverySlot"] = pending
        return result
    finally:
        os.close(lock_fd)


def ack_recovery(*, state_dir: pathlib.Path, project: str, slug: str,
                 environment: str, gcp_project: str, recipient: str, slot: str) -> None:
    """Acknowledge an accepted Cloud Logging recovery write, never its delivery.

    A crash after cloud acceptance but before this fsynced update may replay
    the same transition; provider-native notification rate limiting bounds it.
    """
    binding = _binding(project=project, slug=slug, environment=environment,
                       gcp_project=gcp_project, recipient=recipient)
    if not isinstance(slot, str) or not STAMP.fullmatch(slot):
        raise IncidentError("invalid recovery acknowledgement")
    SCHEDULE._private_owned(state_dir, directory=True)
    lock_fd = _lock(state_dir, project)
    try:
        path = state_dir / f"{project}.incident.json"
        state = _state(path, binding)
        if (state is None or state["condition"] != "healthy" or
                state.get("pendingRecoverySlot") != slot):
            raise IncidentError("recovery acknowledgement is stale or unauthorized")
        next_state = dict(state)
        del next_state["pendingRecoverySlot"]
        _persist(path, next_state)
    finally:
        os.close(lock_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("schedule", "claims", "state-dir", "project", "client-slug",
                "environment", "gcp-project-id", "recipient", "confirm-project", "since-utc"):
        parser.add_argument("--" + key, required=True)
    args = parser.parse_args(argv)
    try:
        result = monitor(schedule=pathlib.Path(args.schedule), claims=pathlib.Path(args.claims),
                         state_dir=pathlib.Path(args.state_dir), project=args.project,
                         slug=args.client_slug, environment=args.environment,
                         gcp_project=args.gcp_project_id, recipient=args.recipient,
                         confirm_project=args.confirm_project,
                         since=SCHEDULE.WINDOW._utc(args.since_utc))
    except (IncidentError, SCHEDULE.ScheduleError, SCHEDULE.WINDOW.WindowError,
            OSError, ValueError, TypeError, KeyError):
        print(json.dumps({"event": "backup.incident.refused",
                          "reason": "unsafe-or-invalid-input"}, sort_keys=True),
              file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 1 if result["incidentActive"] else (3 if result["event"].endswith("indeterminate") else 0)


if __name__ == "__main__":
    sys.exit(main())

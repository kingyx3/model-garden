#!/usr/bin/env python3
"""Dispatch one preapproved UTC backup slot from an external host timer (#230).

No implicit approvals, automatic retry, service stop/start or cloud permissions.
Every occurrence requires a distinct private, single-window approval document.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import pathlib
import stat
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("client_backup_window", ROOT / "scripts" / "client-backup-window.py")
WINDOW = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(WINDOW)

MAX_POLICY_SECONDS = 31 * 86400
SLOT_GRACE_SECONDS = 300


class ScheduleError(ValueError):
    pass


def _private_owned(path: pathlib.Path, *, directory: bool) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ScheduleError("private schedule path unavailable") from exc
    if (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)) is False or \
       info.st_uid != os.geteuid() or info.st_mode & 0o077 or \
       (not directory and info.st_nlink != 1):
        raise ScheduleError("schedule paths must be private, owned, and not symlinks")


def _policy(path: pathlib.Path, *, project: str, slug: str, environment: str,
            gcp_project: str, recipient: str) -> dict:
    _private_owned(path.parent, directory=True)
    _private_owned(path, directory=False)
    doc = WINDOW._private_json(path)
    required = {"schemaVersion", "clientProject", "clientSlug", "environment", "gcpProjectId",
                "ageRecipientSha256", "approvedBy", "changeRef", "validFrom", "expiresAt",
                "weekdaysUTC", "hourUTC", "minuteUTC"}
    if set(doc) != required or type(doc["schemaVersion"]) is not int or doc["schemaVersion"] != 1:
        raise ScheduleError("unsupported recurring schedule schema")
    if (doc["clientProject"], doc["clientSlug"], doc["environment"], doc["gcpProjectId"]) != \
       (project, slug, environment, gcp_project):
        raise ScheduleError("recurrence is for another tenant or environment")
    if not WINDOW.AGE_RECIPIENT.fullmatch(recipient) or \
       doc["ageRecipientSha256"] != hashlib.sha256(recipient.encode("ascii")).hexdigest():
        raise ScheduleError("recurrence age recipient mismatch")
    if not all(isinstance(doc[k], str) and WINDOW.REFERENCE.fullmatch(doc[k])
               for k in ("approvedBy", "changeRef")):
        raise ScheduleError("named approval and change reference required")
    start, end = WINDOW._utc(doc["validFrom"]), WINDOW._utc(doc["expiresAt"])
    if not 0 < (end - start).total_seconds() <= MAX_POLICY_SECONDS:
        raise ScheduleError("recurrence must be finite and at most 31 days")
    days = doc["weekdaysUTC"]
    if not isinstance(days, list) or not days or len(days) > 7 or \
       any(type(d) is not int or not 0 <= d <= 6 for d in days) or len(set(days)) != len(days):
        raise ScheduleError("invalid UTC weekdays")
    if type(doc["hourUTC"]) is not int or not 0 <= doc["hourUTC"] <= 23 or \
       type(doc["minuteUTC"]) is not int or not 0 <= doc["minuteUTC"] <= 59:
        raise ScheduleError("invalid UTC slot time")
    return doc


def _slot(policy: dict, now: dt.datetime) -> dt.datetime | None:
    if now.tzinfo is None:
        raise ScheduleError("timezone-aware clock required")
    now = now.astimezone(dt.timezone.utc)
    start, end = WINDOW._utc(policy["validFrom"]), WINDOW._utc(policy["expiresAt"])
    slot = now.replace(hour=policy["hourUTC"], minute=policy["minuteUTC"], second=0, microsecond=0)
    if not start <= slot < end or slot.weekday() not in policy["weekdaysUTC"]:
        return None
    if not 0 <= (now - slot).total_seconds() < SLOT_GRACE_SECONDS:
        return None
    return slot


def dispatch(*, schedule: pathlib.Path, approvals: pathlib.Path, claims: pathlib.Path,
             compose: pathlib.Path, project: str, slug: str, environment: str,
             gcp_project: str, recipient: str, image: str, output_parent: pathlib.Path,
             confirm_project: str, now: dt.datetime | None = None) -> dict:
    if confirm_project != project:
        raise ScheduleError("exact project confirmation required")
    doc = _policy(schedule, project=project, slug=slug, environment=environment,
                  gcp_project=gcp_project, recipient=recipient)
    slot = _slot(doc, now if now is not None else dt.datetime.now(dt.timezone.utc))
    if slot is None:
        return {"event": "backup.schedule.skipped", "reason": "outside-approved-slot"}
    _private_owned(approvals, directory=True)
    _private_owned(claims, directory=True)
    stamp = slot.strftime("%Y%m%dT%H%M%SZ")
    approval = approvals / f"{project}-{stamp}.json"
    _private_owned(approval, directory=False)
    occurrence = WINDOW._private_json(approval)
    if occurrence.get("validFrom") != slot.strftime("%Y-%m-%dT%H:%M:%SZ") or \
       occurrence.get("approvedBy") != doc["approvedBy"] or \
       occurrence.get("changeRef") != doc["changeRef"] or \
       WINDOW._utc(occurrence.get("expiresAt")) > slot + dt.timedelta(seconds=WINDOW.MAX_WINDOW_SECONDS):
        raise ScheduleError("per-slot approval does not match recurring authorization")
    WINDOW._approved(approval, project=project, slug=slug, environment=environment,
                     gcp_project=gcp_project, recipient=recipient,
                     now=now if now is not None else dt.datetime.now(dt.timezone.utc))
    claim = claims / f"{project}-{stamp}.claimed"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(claim, flags, 0o600)
    except FileExistsError as exc:
        raise ScheduleError("slot already attempted; no automatic retry") from exc
    except OSError as exc:
        raise ScheduleError("unable to durably claim backup slot") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"clientProject": project, "slotUTC": stamp,
                                     "status": "attempted"}, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        dirfd = os.open(claims, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except OSError as exc:
        raise ScheduleError("unable to durably record backup slot") from exc
    # A failed attempt is never silently retried. Review partial objects and issue a new approval.
    result = WINDOW.run(compose=compose, project=project, slug=slug, environment=environment,
                        gcp_project=gcp_project, recipient=recipient, image=image,
                        approval=approval, output_parent=output_parent,
                        confirm_project=confirm_project)
    return {"event": "backup.schedule.dispatched", "status": result["status"],
            "slotUTC": stamp, "runId": result["runId"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("schedule", "approvals", "claims", "compose", "project", "client-slug",
                "environment", "gcp-project-id", "recipient", "image", "output-parent", "confirm-project"):
        parser.add_argument("--" + key, required=True)
    args = parser.parse_args(argv)
    try:
        result = dispatch(schedule=pathlib.Path(args.schedule), approvals=pathlib.Path(args.approvals),
                          claims=pathlib.Path(args.claims), compose=pathlib.Path(args.compose),
                          project=args.project, slug=args.client_slug, environment=args.environment,
                          gcp_project=args.gcp_project_id, recipient=args.recipient, image=args.image,
                          output_parent=pathlib.Path(args.output_parent), confirm_project=args.confirm_project)
    except (ScheduleError, WINDOW.WindowError, OSError, ValueError, TypeError, KeyError):
        print(json.dumps({"event": "backup.schedule.failed", "reason": "schedule-refused",
                          "status": "not-recovery-proven"}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

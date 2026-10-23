#!/usr/bin/env python3
"""Read-only, tenant-bound audit of due recurring backup slots (#230).

This reports missed, failed and unknown local outcomes. It never sends an
alert, grants cloud access, retries a backup or treats upload as restore proof.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import pathlib
import stat
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "client_backup_schedule", ROOT / "scripts" / "client-backup-schedule.py"
)
SCHEDULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SCHEDULE)

MAX_RECORD_BYTES = 4096
MAX_LOOKBACK = dt.timedelta(days=31)
UNKNOWN = object()


def _record(path: pathlib.Path):
    """Return a bounded private JSON record, None if absent, UNKNOWN if unsafe."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        return UNKNOWN
    try:
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                return UNKNOWN
            raw = stream.read(MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES:
            return UNKNOWN
        doc = json.loads(raw)
        return doc if type(doc) is dict else UNKNOWN
    except (OSError, ValueError, UnicodeError):
        return UNKNOWN


def _status(claims: pathlib.Path, project: str, stamp: str) -> str:
    claim = _record(claims / f"{project}-{stamp}.claimed")
    receipt = _record(claims / f"{project}-{stamp}.result.json")
    if claim is None and receipt is None:
        return "missed"
    if claim is UNKNOWN or receipt is UNKNOWN:
        return "unknown"
    if claim != {"clientProject": project, "slotUTC": stamp, "status": "attempted"}:
        return "unknown"
    if receipt is None:
        return "unknown"
    if set(receipt) != {"clientProject", "slotUTC", "status"} or \
       receipt["clientProject"] != project or receipt["slotUTC"] != stamp:
        return "unknown"
    if receipt["status"] in ("uploaded-not-recovery-proven", "failed-not-recovery-proven"):
        return receipt["status"]
    return "unknown"


def audit(*, schedule: pathlib.Path, claims: pathlib.Path, project: str,
          slug: str, environment: str, gcp_project: str, recipient: str,
          confirm_project: str, since: dt.datetime,
          now: dt.datetime | None = None) -> dict:
    if confirm_project != project:
        raise SCHEDULE.ScheduleError("exact project confirmation required")
    policy = SCHEDULE._policy(schedule, project=project, slug=slug,
                              environment=environment, gcp_project=gcp_project,
                              recipient=recipient)
    SCHEDULE._private_owned(claims, directory=True)
    now = now if now is not None else dt.datetime.now(dt.timezone.utc)
    if since.tzinfo is None or now.tzinfo is None:
        raise SCHEDULE.ScheduleError("UTC-aware inspection bounds required")
    since, now = since.astimezone(dt.timezone.utc), now.astimezone(dt.timezone.utc)
    start = SCHEDULE.WINDOW._utc(policy["validFrom"])
    end = SCHEDULE.WINDOW._utc(policy["expiresAt"])
    if since < start or now < since or now - since > MAX_LOOKBACK:
        raise SCHEDULE.ScheduleError("inspection must be bounded within approved recurrence")
    slots = []
    day = since.date()
    while day <= now.date():
        slot = dt.datetime.combine(day, dt.time(policy["hourUTC"], policy["minuteUTC"]),
                                   tzinfo=dt.timezone.utc)
        if (day.weekday() in policy["weekdaysUTC"] and
                since <= slot < end and
                now >= slot + dt.timedelta(seconds=SCHEDULE.SLOT_GRACE_SECONDS)):
            stamp = slot.strftime("%Y%m%dT%H%M%SZ")
            slots.append({"slotUTC": stamp, "status": _status(claims, project, stamp)})
        day += dt.timedelta(days=1)
    return {"event": "backup.schedule.audit", "clientProject": project,
            "slots": slots, "recoveryProven": False, "notificationsDelivered": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("schedule", "claims", "project", "client-slug", "environment",
                "gcp-project-id", "recipient", "confirm-project", "since-utc"):
        parser.add_argument("--" + key, required=True)
    args = parser.parse_args(argv)
    try:
        result = audit(schedule=pathlib.Path(args.schedule), claims=pathlib.Path(args.claims),
                       project=args.project, slug=args.client_slug, environment=args.environment,
                       gcp_project=args.gcp_project_id, recipient=args.recipient,
                       confirm_project=args.confirm_project,
                       since=SCHEDULE.WINDOW._utc(args.since_utc))
    except (SCHEDULE.ScheduleError, SCHEDULE.WINDOW.WindowError,
            OSError, ValueError, TypeError, KeyError):
        print(json.dumps({"event": "backup.schedule.audit-refused",
                          "reason": "unsafe-or-invalid-input"}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

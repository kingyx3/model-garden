#!/usr/bin/env python3
"""Opt-in, bounded Cloud Logging level signal for tenant-bound backup incidents.

A successful log write is NOT proof of alert delivery or recovery.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "client_backup_incident", ROOT / "scripts" / "client-backup-incident.py"
)
INCIDENT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(INCIDENT)
SCHEDULE = INCIDENT.SCHEDULE
LOG_NAME = "model-garden-backup-health"
TIMEOUT_SECONDS = 20
EVENTS = {"backup.incident.opened", "backup.incident.recovered",
          "backup.incident.unchanged", "backup.incident.indeterminate"}


class CloudLogError(ValueError):
    pass


def _signal(report: dict, project: str) -> tuple[dict, str]:
    """Allowlist only non-sensitive fields; never forward raw monitor output."""
    if (not isinstance(report, dict) or report.get("event") not in EVENTS or
            report.get("clientProject") != project or
            type(report.get("incidentActive")) is not bool or
            report.get("recoveryProven") is not False or
            report.get("notificationsDelivered") is not False):
        raise CloudLogError("invalid backup incident signal")
    event = report["event"]
    slot = report.get("slotUTC")
    if event == "backup.incident.indeterminate":
        if slot is not None:
            raise CloudLogError("indeterminate event must not assert a slot")
        health = "UNHEALTHY" if report["incidentActive"] else "UNKNOWN"
    else:
        if not isinstance(slot, str) or not INCIDENT.STAMP.fullmatch(slot):
            raise CloudLogError("invalid incident slot")
        health = "UNHEALTHY" if report["incidentActive"] else "HEALTHY"
    severity = "ERROR" if health == "UNHEALTHY" else (
        "WARNING" if health == "UNKNOWN" else "INFO")
    payload = {"schemaVersion": 1, "component": "client-backup",
               "clientProject": project, "health": health,
               "incidentEvent": event, "recoveryProven": False,
               "notificationsDelivered": False}
    if slot is not None:
        payload["slotUTC"] = slot
    return payload, severity


def emit(*, schedule: pathlib.Path, claims: pathlib.Path, state_dir: pathlib.Path,
         project: str, slug: str, environment: str, gcp_project: str,
         confirm_gcp_project: str, recipient: str, confirm_project: str,
         since, now=None) -> dict:
    if gcp_project != confirm_gcp_project:
        raise CloudLogError("exact GCP project confirmation required")
    # Check the tenant binding before any cloud command is constructed.
    INCIDENT._binding(project=project, slug=slug, environment=environment,
                      gcp_project=gcp_project, recipient=recipient)
    report = INCIDENT.monitor(schedule=schedule, claims=claims, state_dir=state_dir,
                              project=project, slug=slug, environment=environment,
                              gcp_project=gcp_project, recipient=recipient,
                              confirm_project=confirm_project, since=since, now=now)
    payload, severity = _signal(report, project)
    # Repeated LEVEL samples are deliberate: a lost transition cannot silently
    # suppress later unhealthy samples. No shell, unbounded wait or raw stderr.
    command = ["gcloud", "logging", "write", LOG_NAME,
               json.dumps(payload, sort_keys=True, separators=(",", ":")),
               "--project=" + gcp_project, "--payload-type=json",
               "--severity=" + severity, "--quiet"]
    try:
        completed = subprocess.run(command, timeout=TIMEOUT_SECONDS, check=False,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CloudLogError("bounded Cloud Logging invocation failed") from exc
    if completed.returncode != 0:
        raise CloudLogError("Cloud Logging rejected level signal")
    return {"event": "backup.cloud-log.accepted", "clientProject": project,
            "health": payload["health"], "cloudLogCommandSucceeded": True,
            "notificationsDelivered": False, "recoveryProven": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("schedule", "claims", "state-dir", "project", "client-slug",
                "environment", "gcp-project-id", "confirm-gcp-project-id",
                "recipient", "confirm-project", "since-utc"):
        parser.add_argument("--" + key, required=True)
    args = parser.parse_args(argv)
    try:
        result = emit(schedule=pathlib.Path(args.schedule), claims=pathlib.Path(args.claims),
                      state_dir=pathlib.Path(args.state_dir), project=args.project,
                      slug=args.client_slug, environment=args.environment,
                      gcp_project=args.gcp_project_id,
                      confirm_gcp_project=args.confirm_gcp_project_id,
                      recipient=args.recipient, confirm_project=args.confirm_project,
                      since=SCHEDULE.WINDOW._utc(args.since_utc))
    except (CloudLogError, INCIDENT.IncidentError, SCHEDULE.ScheduleError,
            SCHEDULE.WINDOW.WindowError, OSError, ValueError, TypeError, KeyError):
        print(json.dumps({"event": "backup.cloud-log.refused",
                          "reason": "unsafe-input-or-log-write-failed",
                          "notificationsDelivered": False}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 1 if result["health"] == "UNHEALTHY" else (3 if result["health"] == "UNKNOWN" else 0)


if __name__ == "__main__":
    sys.exit(main())

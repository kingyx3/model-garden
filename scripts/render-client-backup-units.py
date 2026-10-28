#!/usr/bin/env python3
"""Render review-only, tenant-bound systemd backup units (#230).

Never installs, starts or enables anything. An authorized operator must independently
approve a recurrence and every slot, enforce an external deployment freeze, provision
least-privilege cloud identity and review the host's Docker privilege boundary.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

SLUG = re.compile(r"[a-z][a-z0-9-]{0,30}[a-z0-9]\Z")
GCP_PROJECT = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]\Z")
SERVICE_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
AGE_RECIPIENT = re.compile(r"age1[a-z0-9]{50,124}\Z")
SNAPSHOT_IMAGE = re.compile(r"[A-Za-z0-9_.:/-]+@sha256:[0-9a-f]{64}\Z")
ENVIRONMENTS = {"dev", "uat", "prod"}


class UnitError(ValueError):
    pass


def render_units(*, client_slug: str, environment: str, client_project: str,
                 confirm_client_project: str, gcp_project: str,
                 confirm_gcp_project: str, service_user: str, age_recipient: str,
                 snapshot_image: str) -> dict:
    """Return static systemd unit text with no filesystem/cloud side effects."""
    if (not isinstance(client_slug, str) or not SLUG.fullmatch(client_slug) or
            not isinstance(environment, str) or environment not in ENVIRONMENTS or
            not isinstance(client_project, str) or
            client_project != f"{client_slug}-{environment}" or
            client_project != confirm_client_project or
            not isinstance(gcp_project, str) or not GCP_PROJECT.fullmatch(gcp_project) or
            gcp_project != confirm_gcp_project):
        raise UnitError("client/environment/cloud confirmation failed")
    if (not isinstance(service_user, str) or not SERVICE_USER.fullmatch(service_user) or
            service_user in ("root", "nobody") or
            not isinstance(age_recipient, str) or not AGE_RECIPIENT.fullmatch(age_recipient) or
            not isinstance(snapshot_image, str) or
            not SNAPSHOT_IMAGE.fullmatch(snapshot_image)):
        raise UnitError("unsafe or unpinned host configuration")

    name = "model-garden-client-backup-" + client_project
    state = "/var/lib/model-garden-backup/" + client_project
    compose = "/srv/model-garden/" + client_project + "/compose.yaml"
    command = " ".join([
        "/usr/bin/python3", "/opt/model-garden/scripts/client-backup-schedule.py",
        "--schedule", state + "/policy/schedule.json",
        "--approvals", state + "/approvals",
        "--claims", state + "/claims",
        "--compose", compose,
        "--project", client_project,
        "--client-slug", client_slug,
        "--environment", environment,
        "--gcp-project-id", gcp_project,
        "--recipient", age_recipient,
        "--image", snapshot_image,
        "--output-parent", state + "/snapshots",
        "--confirm-project", client_project,
    ])
    service = "\n".join([
        "[Unit]",
        "Description=Approved Model Garden client backup dispatch for " + client_project,
        "Documentation=https://github.com/kingyx3/model-garden/blob/main/docs/client-backup-schedule.md",
        "After=network-online.target docker.service",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=oneshot",
        "User=" + service_user,
        "Group=" + service_user,
        "WorkingDirectory=/opt/model-garden",
        "ExecStart=" + command,
        "TimeoutStartSec=3700s",
        "KillMode=control-group",
        "UMask=0077",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectControlGroups=yes",
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "ReadOnlyPaths=" + compose,
        "ReadWritePaths=" + state + " /run/lock/model-garden",
        "",
    ])
    timer = "\n".join([
        "[Unit]",
        "Description=Poll approved Model Garden backup slots for " + client_project,
        "",
        "[Timer]",
        # Polling is not authorization. The dispatcher validates both policy and
        # per-slot owner approval and durably claims each attempt before upload.
        "OnCalendar=*-*-* *:*:00",
        "AccuracySec=1s",
        "Persistent=true",
        "Unit=" + name + ".service",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ])
    return {
        "schemaVersion": 1,
        "clientProject": client_project,
        "installed": False,
        "backupProven": False,
        "notificationsDelivered": False,
        "requiresOperatorReview": True,
        "units": {name + ".service": service, name + ".timer": timer},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("client-slug", "environment", "client-project", "confirm-client-project",
                "gcp-project", "confirm-gcp-project", "service-user", "age-recipient",
                "snapshot-image"):
        parser.add_argument("--" + key, required=True)
    args = parser.parse_args(argv)
    try:
        bundle = render_units(**vars(args))
    except UnitError:
        print(json.dumps({"event": "backup.units.refused",
                          "reason": "unsafe-or-unconfirmed-configuration"}), file=sys.stderr)
        return 2
    print(json.dumps(bundle, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

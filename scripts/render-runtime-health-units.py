#!/usr/bin/env python3
"""Render review-only systemd units for private GCE runtime heartbeat sampling (#231).

This is a local configuration generator, NOT an installer, IAM grant, Terraform
apply, uptime proof, or authorization to access a client Docker daemon.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

CLIENT = re.compile(r"[a-z0-9][a-z0-9-]{1,19}\\Z")
PROJECT = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]\\Z")
COMPOSE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\\Z")
USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}\\Z")


class UnitError(ValueError):
    """Unsafe or mismatched host/tenant configuration."""


def render_units(*, client: str, environment: str, compose_project: str,
                 confirm_compose_project: str, project_id: str,
                 confirm_project_id: str, service_user: str) -> dict:
    """Produce static, single-tenant unit text without filesystem or cloud effects."""
    if (not isinstance(client, str) or not CLIENT.fullmatch(client) or
            environment not in ("dev", "prod") or
            not isinstance(compose_project, str) or
            not COMPOSE.fullmatch(compose_project) or
            compose_project != f"{client}-{environment}" or
            compose_project != confirm_compose_project or
            not isinstance(project_id, str) or
            not PROJECT.fullmatch(project_id) or
            project_id != confirm_project_id):
        raise UnitError("client, environment, compose or project confirmation failed")
    if (not isinstance(service_user, str) or not USER.fullmatch(service_user) or
            service_user in ("root", "nobody")):
        raise UnitError("dedicated host sampler user required")

    name = "model-garden-runtime-health-" + compose_project
    command = " ".join([
        "/usr/bin/python3", "/opt/model-garden/scripts/runtime-health-metric.py",
        "--project-id", project_id, "--client", client,
        "--environment", environment, "--compose-project", compose_project,
    ])
    # The fixed script invokes Docker by argv and publishes via the GCE
    # metadata identity. Access to docker.sock is root-equivalent; this
    # dedicated host identity must be approved and strictly controlled.
    service = "\n".join([
        "[Unit]",
        "Description=Private Model Garden runtime readiness sampler for " + compose_project,
        "Documentation=https://github.com/kingyx3/model-garden/blob/main/docs/runtime-private-monitoring.md",
        "After=network-online.target docker.service",
        "Wants=network-online.target",
        "Requires=docker.service",
        "",
        "[Service]",
        "Type=oneshot",
        "User=" + service_user,
        "Group=" + service_user,
        "SupplementaryGroups=docker",
        "WorkingDirectory=/opt/model-garden",
        "ExecStart=" + command,
        "TimeoutStartSec=60s",
        "UMask=0077",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectControlGroups=yes",
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "",
    ])
    timer = "\n".join([
        "[Unit]",
        "Description=Poll private Model Garden gateway and worker readiness for " + compose_project,
        "",
        "[Timer]",
        "OnBootSec=60s",
        "OnUnitInactiveSec=60s",
        "AccuracySec=10s",
        "Unit=" + name + ".service",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ])
    return {
        "schemaVersion": 1,
        "clientProject": compose_project,
        "installed": False,
        "monitoringProven": False,
        "notificationsDelivered": False,
        "requiresOperatorReview": True,
        "dockerAccessIsRootEquivalent": True,
        "units": {name + ".service": service, name + ".timer": timer},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("client", "environment", "compose-project",
                 "confirm-compose-project", "project-id",
                 "confirm-project-id", "service-user"):
        parser.add_argument("--" + flag, required=True)
    args = parser.parse_args(argv)
    try:
        bundle = render_units(**vars(args))
    except UnitError:
        print(json.dumps({"event": "runtime.monitor.units.refused",
                          "reason": "unsafe-or-unconfirmed-configuration"}), file=sys.stderr)
        return 2
    print(json.dumps(bundle, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

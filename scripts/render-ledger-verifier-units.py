#!/usr/bin/env python3
"""Render reviewed, tenant-bound systemd verifier units without installing them (#80).

The actual verifier must run on an independently administered host with a
separate read identity and a trusted, current ledger snapshot. Unit generation
does not grant IAM, lock storage, enable timers or deliver notifications.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

PROJECT = re.compile(r"[a-z][a-z0-9-]{2,62}")
BUCKET = re.compile(r"[a-z0-9][a-z0-9._-]{2,222}")
IDENTITY = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9.-]*\.iam\.gserviceaccount\.com"
)
SERVICE_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
PREFIX = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")


class UnitError(ValueError):
    pass


def render_units(*, client_project: str, confirm_client_project: str,
                 gcp_project: str, confirm_gcp_project: str, bucket: str,
                 prefix: str, verifier_identity: str, runtime_identity: str,
                 service_user: str) -> dict:
    """Return only reviewable unit text; never touch the filesystem or cloud."""
    if (not isinstance(client_project, str) or
            not PROJECT.fullmatch(client_project) or
            client_project != confirm_client_project or
            not isinstance(gcp_project, str) or
            not PROJECT.fullmatch(gcp_project) or
            gcp_project != confirm_gcp_project):
        raise UnitError("client/environment or GCP project confirmation failed")
    if (not isinstance(bucket, str) or not BUCKET.fullmatch(bucket) or
            ".." in bucket or not isinstance(prefix, str) or
            len(prefix) > 256 or not PREFIX.fullmatch(prefix) or
            any(segment in (".", "..") for segment in prefix.split("/")) or
            not (prefix == client_project or prefix.startswith(client_project + "/"))):
        raise UnitError("bucket or tenant export prefix is invalid")
    if (not isinstance(verifier_identity, str) or
            not IDENTITY.fullmatch(verifier_identity) or
            not isinstance(runtime_identity, str) or
            not IDENTITY.fullmatch(runtime_identity) or
            verifier_identity == runtime_identity):
        raise UnitError("separate verifier and runtime service accounts required")
    if (not isinstance(service_user, str) or
            not SERVICE_USER.fullmatch(service_user) or
            service_user in ("root", "nobody")):
        raise UnitError("dedicated unprivileged verifier host user required")

    name = "model-garden-ledger-verifier-" + client_project
    ledger = "/srv/model-garden-verifier/" + client_project + "/audit.jsonl"
    state = "/var/lib/model-garden-verifier/" + client_project
    # All values are allow-listed as single safe systemd ExecStart tokens;
    # never invoke a shell or use a variable-expanded EnvironmentFile.
    command = " ".join([
        "/usr/bin/python3", "/opt/model-garden/scripts/ledger-verifier-health.py",
        "--ledger", ledger, "--state-dir", state,
        "--gcs-bucket", bucket, "--gcs-prefix", prefix,
        "--client-project", client_project,
        "--confirm-client-project", confirm_client_project,
        "--gcp-project", gcp_project,
        "--confirm-gcp-project", confirm_gcp_project,
        "--verifier-identity", verifier_identity,
        "--runtime-identity", runtime_identity,
    ])
    service = "\n".join([
        "[Unit]",
        "Description=Independent Model Garden ledger verifier for " + client_project,
        "Documentation=https://github.com/kingyx3/model-garden/blob/main/docs/independent-ledger-verifier.md",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=oneshot",
        "User=" + service_user,
        "Group=" + service_user,
        "WorkingDirectory=/opt/model-garden",
        "Environment=HOME=" + state,
        "ExecStart=" + command,
        "TimeoutStartSec=120s",
        "UMask=0077",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "ProtectKernelTunables=yes",
        "ProtectControlGroups=yes",
        "ProtectKernelModules=yes",
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "ReadOnlyPaths=" + ledger,
        "ReadWritePaths=" + state,
        "",
    ])
    timer = "\n".join([
        "[Unit]",
        "Description=Poll independent Model Garden ledger verifier for " + client_project,
        "",
        "[Timer]",
        "OnBootSec=5min",
        "OnCalendar=*-*-* *:0/5:00",
        "Persistent=true",
        "Unit=" + name + ".service",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ])
    return {
        "schemaVersion": 1, "clientProject": client_project,
        "installed": False, "notificationsDelivered": False,
        "units": {name + ".service": service, name + ".timer": timer},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("client-project", "confirm-client-project", "gcp-project",
                 "confirm-gcp-project", "bucket", "prefix", "verifier-identity",
                 "runtime-identity", "service-user"):
        parser.add_argument("--" + flag, required=True)
    args = parser.parse_args(argv)
    try:
        result = render_units(**vars(args))
    except UnitError:
        print(json.dumps({"event": "ledger.verifier.units.refused",
                          "reason": "unsafe-or-unconfirmed-configuration"}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

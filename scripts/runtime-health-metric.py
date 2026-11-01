#!/usr/bin/env python3
"""Publish private gateway-loop and worker readiness; no public probes (#231).

An approved host timer executes fixed loopback checks inside the gateway
container. Only two boolean metrics leave the host. Not live acceptance proof.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("model_garden_voice_metric_shared", ROOT / "scripts" / "voice-health-metric.py")
VOICE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(VOICE)

PROBE = (
    "import sys, urllib.request; "
    "opener=urllib.request.build_opener(urllib.request.ProxyHandler({}), "
    "type('NoRedirect',(urllib.request.HTTPRedirectHandler,),"
    "{'redirect_request':lambda self,*args,**kwargs:None})()); "
    "r=opener.open('http://127.0.0.1:8080/health/{component}', timeout=3); "
    "sys.exit(0 if r.status == 200 else 1)"
)
METRICS = {"gateway": "custom.googleapis.com/model_garden/gateway_ready",
           "worker": "custom.googleapis.com/model_garden/worker_ready"}
CONTAINER = re.compile(r"^[0-9a-f]{12,64}$")


def sample(compose_project: str) -> dict[str, bool]:
    """One exact Compose gateway; each loopback readiness probe is independent."""
    ready = {"gateway": False, "worker": False}
    try:
        listing = subprocess.run(
            ["docker", "ps", "-a", "--no-trunc",
             "--filter", f"label=com.docker.compose.project={compose_project}",
             "--filter", "label=com.docker.compose.service=gateway",
             "--format", "{{.ID}}"],
            capture_output=True, text=True, check=True, timeout=8,
        )
        ids = listing.stdout.splitlines()
        if len(ids) != 1 or not CONTAINER.fullmatch(ids[0]):
            return ready
        for component in ready:
            try:
                subprocess.run(["docker", "exec", ids[0], "python3", "-c",
                                PROBE.replace("{component}", component)],
                               capture_output=True, check=True, timeout=8)
                ready[component] = True
            except (OSError, subprocess.SubprocessError):
                pass
    except (OSError, subprocess.SubprocessError):
        pass
    return ready


def build_timeseries(project_id: str, instance_id: str, zone: str, client: str,
                     environment: str, ready: dict[str, bool], at: datetime) -> dict:
    if set(ready) != set(METRICS) or any(type(value) is not bool for value in ready.values()):
        raise ValueError("exact boolean runtime readiness required")
    series = []
    for component, metric in METRICS.items():
        item = VOICE.build_timeseries(project_id, instance_id, zone, client,
                                      environment, ready[component], at)["timeSeries"][0]
        item["metric"]["type"] = metric
        series.append(item)
    return {"timeSeries": series}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One private GCE gateway and worker readiness sample")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--client", required=True)
    parser.add_argument("--environment", required=True, choices=("dev", "prod"))
    parser.add_argument("--compose-project", required=True)
    args = parser.parse_args(argv)
    try:
        VOICE.validate_inputs(args.project_id, args.client, args.environment, args.compose_project)
        if VOICE.metadata("project/project-id") != args.project_id:
            raise ValueError("VM identity does not match target project")
        instance_id = VOICE.metadata("instance/id")
        zone = VOICE.metadata("instance/zone").rsplit("/", 1)[-1]
        if not re.fullmatch(r"[0-9]+", instance_id) or not re.fullmatch(r"[a-z0-9-]+", zone):
            raise ValueError("invalid GCE instance identity")
        payload = build_timeseries(args.project_id, instance_id, zone, args.client,
                                   args.environment, sample(args.compose_project), datetime.now(timezone.utc))
        VOICE.publish_sample(args.project_id, payload)
    except Exception:
        print("private runtime health metric publication unavailable", file=sys.stderr)
        return 1
    print("private runtime readiness samples published (not notification proof)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

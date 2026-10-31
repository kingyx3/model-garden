#!/usr/bin/env python3
"""Independent, read-only ledger verifier and redacted Cloud Logging level signal (#80).

Run on an approved verifier host/identity, not the runtime or deploy host. Every
invocation checks the exported chain, then publishes a bounded health level;
Cloud Logging acceptance is not notification delivery or immutable-export proof.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import stat
import pathlib
import re
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOG_NAME = "model-garden-ledger-verifier"
TIMEOUT_SECONDS = 20
PROJECT = re.compile(r"^[a-z][a-z0-9-]{2,62}$")
IDENTITY = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*@[a-zA-Z0-9][a-zA-Z0-9.-]*\.iam\.gserviceaccount\.com$")


class VerifierError(ValueError):
    pass


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def check_and_publish(*, ledger: pathlib.Path, state_dir: pathlib.Path, bucket: str,
                      prefix: str, client_project: str, confirm_client_project: str,
                      gcp_project: str, confirm_gcp_project: str,
                      verifier_identity: str, runtime_identity: str,
                      now: int | None = None, monitor_factory=None, sink_factory=None,
                      publish=None) -> dict:
    """Emit a non-sensitive level even when verification itself fails.

    The configured verifier must be distinct from the runtime identity. Actual
    IAM separation and independent host execution remain deployment proof gates.
    """
    if (not PROJECT.fullmatch(client_project) or client_project != confirm_client_project or
            not PROJECT.fullmatch(gcp_project) or gcp_project != confirm_gcp_project or
            not IDENTITY.fullmatch(verifier_identity) or
            not IDENTITY.fullmatch(runtime_identity) or
            verifier_identity == runtime_identity or
            not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,222}", bucket) or
            not isinstance(prefix, str) or prefix.startswith("/") or ".." in prefix.split("/") or
            any(not re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in prefix.split("/") if part) or
            len(prefix) > 256 or type(now) not in (int, type(None))):
        raise VerifierError("invalid or unconfirmed verifier tenant binding")
    # State must live in a private verifier-owned directory; never follow a
    # symlink into another tenant or repair a directory with unsafe ownership.
    try:
        state_info = state_dir.lstat()
    except FileNotFoundError:
        state_dir.mkdir(mode=0o700, parents=False)
        state_info = state_dir.lstat()
    if (not stat.S_ISDIR(state_info.st_mode) or state_info.st_uid != os.geteuid()
            or state_info.st_mode & 0o077):
        raise VerifierError("unsafe verifier state directory")
    if now is None:
        now = int(time.time())
    if monitor_factory is None:
        monitor_factory = _load("mg_independent_export_monitor", "platform/core/export_monitor.py").ExportMonitor
    monitor = monitor_factory(state_dir, name=client_project)
    # No implicit healthy result on failed reads, credentials, or malformed
    # reports. Never print exception text: provider errors may contain secrets.
    try:
        if sink_factory is None:
            export = _load("mg_independent_ledger_export", "platform/core/ledger_export.py")
            token = export.impersonated_token(
                verifier_identity, export.metadata_token(),
                scope="https://www.googleapis.com/auth/devstorage.read_only")
            sink = export.GcsSink(bucket, token, prefix=prefix)
        else:
            sink = sink_factory()
        result = monitor.check(ledger, sink, now=now, owner=None,
                               max_lag_events=100, max_head_age_seconds=6 * 3600)
        report = result["report"]
        if type(report.get("ok")) is not bool or not isinstance(report.get("problems"), list):
            raise VerifierError("invalid verification report")
        healthy = report["ok"] and not report["problems"]
        if healthy:
            # verify_export intentionally permits an empty local ledger and
            # empty export as a structurally consistent pair. An independent
            # health signal must NOT promote that absence of evidence to
            # successful verification or a green go-live/alert heartbeat.
            # The chained-event count differs from sequence when legacy v1
            # records precede the chain, so do not compare the two directly.
            evidence = ("localChainedEvents", "exportedRecords", "exportedHeadSequence")
            if any(type(report.get(field)) is not int or report[field] <= 0
                   for field in evidence):
                raise VerifierError("independent ledger verification has no substantive chain evidence")
        kinds = sorted({n["kind"] for n in result["notices"] if n.get("kind") != "export-recovered"})
    except Exception:  # noqa: BLE001 - failure to verify MUST be an unhealthy signal
        healthy = False
        kinds = ["export-failure"]
        # Persist an export-failure condition when verification cannot run.
        # The monitor's notification state survives a verifier restart.
        monitor.evaluate({"ok": False, "problems": ["export failure: independent verification unavailable"]},
                         now=now, owner=None)
    payload = {"schemaVersion": 1, "component": "ledger-independent-verifier",
               "clientProject": client_project, "health": "HEALTHY" if healthy else "UNHEALTHY",
               "conditions": kinds, "independentVerificationPassed": bool(healthy),
               "notificationsDelivered": False}
    command = ["gcloud", "logging", "write", LOG_NAME,
               json.dumps(payload, sort_keys=True, separators=(",", ":")),
               "--project=" + gcp_project, "--payload-type=json",
               "--severity=" + ("INFO" if healthy else "ERROR"), "--quiet"]
    if publish is None:
        def publish(cmd):
            return subprocess.run(cmd, timeout=TIMEOUT_SECONDS, check=False,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    try:
        if publish(command) != 0:
            raise VerifierError("Cloud Logging rejected verifier health")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VerifierError("bounded verifier health publication failed") from exc
    return {"clientProject": client_project, "health": payload["health"],
            "cloudLogCommandSucceeded": True, "notificationsDelivered": False,
            "independentVerificationPassed": bool(healthy)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("ledger", "state-dir", "gcs-bucket", "client-project",
                 "confirm-client-project", "gcp-project", "confirm-gcp-project",
                 "verifier-identity", "runtime-identity"):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--gcs-prefix", default="")
    args = parser.parse_args(argv)
    try:
        result = check_and_publish(ledger=pathlib.Path(args.ledger),
            state_dir=pathlib.Path(args.state_dir), bucket=args.gcs_bucket,
            prefix=args.gcs_prefix, client_project=args.client_project,
            confirm_client_project=args.confirm_client_project,
            gcp_project=args.gcp_project, confirm_gcp_project=args.confirm_gcp_project,
            verifier_identity=args.verifier_identity, runtime_identity=args.runtime_identity)
    except (VerifierError, OSError, ValueError):
        print(json.dumps({"event": "ledger.verifier.refused", "reason": "unsafe-input-or-publication-failed"}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["independentVerificationPassed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

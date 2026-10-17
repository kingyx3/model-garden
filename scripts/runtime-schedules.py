#!/usr/bin/env python3
"""Plan or apply the workspace's schedules on a governed runtime (run inside governed-tools).

  runtime-schedules.py /opt/data/.modelgarden/schedules plan  [--definitions defs.json | --desired-state desired-state.json]
  runtime-schedules.py /opt/data/.modelgarden/schedules apply --by jordan@client.example [--definitions defs.json]
  runtime-schedules.py /opt/data/.modelgarden/schedules export

`plan` prints what would be created, updated, retired or left unchanged and changes nothing. `apply` makes the managed
schedules match the definitions; a managed schedule no longer listed is paused (`removed-from-workspace`), never deleted, and a
schedule someone created from chat is never touched. Each change is a chained ledger event under the person named by `--by`,
holding identifiers and a digest of the goal, never the goal. Definitions come from `--definitions` (the JSON
`compile-workspace.py --schedules` prints) or from the `schedules` of the compiled desired state, which is how the sidecar's own
deployment carries them; with neither, the compiled desired state at `MODEL_GARDEN_DESIRED_STATE` is read. Nothing here runs a
schedule: the runtime worker (`platform/runtime/worker.py`) applies the compiled definitions on start and runs them on its clock.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_DESIRED_STATE = pathlib.Path(os.environ.get("MODEL_GARDEN_DESIRED_STATE", "/opt/profile-seed/.modelgarden/desired-state.json"))


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


GOVERN = _load("runtime_schedules_govern", ROOT / "scripts" / "govern-action.py")
SCHEDULES = _load("runtime_schedules_store", ROOT / "platform" / "core" / "schedules.py")


def tenant_of(desired_state: pathlib.Path) -> str:
    try:
        client = json.loads(desired_state.read_text(encoding="utf-8")).get("source", {}).get("client")
    except (OSError, json.JSONDecodeError, AttributeError):
        client = None
    return re.sub(r"[^a-z0-9-]+", "-", str(client or "").lower()).strip("-")[:63] or "default"


def definitions_from(args: argparse.Namespace) -> list[dict]:
    if args.definitions is not None:
        document = json.loads(args.definitions.read_text(encoding="utf-8"))
    else:
        state = json.loads((args.desired_state or DEFAULT_DESIRED_STATE).read_text(encoding="utf-8"))
        document = state.get("schedules", [])
    if not isinstance(document, list):
        raise ValueError("schedule definitions are a JSON list")
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store", type=pathlib.Path, help="schedule store root")
    parser.add_argument("--tenant", help="client-environment slug (default: from the compiled desired state)")
    parser.add_argument("--audit-path", type=pathlib.Path, help="ledger path (default: <store>/../audit.jsonl)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (("plan", "show what applying would change; changes nothing"), ("apply", "make the managed schedules match")):
        command = sub.add_parser(name, help=text)
        group = command.add_mutually_exclusive_group()
        group.add_argument("--definitions", type=pathlib.Path, help="JSON list of definitions (compile-workspace.py --schedules)")
        group.add_argument("--desired-state", type=pathlib.Path, help="compiled desired-state.json carrying `schedules`")
        if name == "apply":
            command.add_argument("--by", required=True, help="the named person applying the change")
    sub.add_parser("export", help="print the managed schedules as definitions (round-trips through plan and apply)")
    args = parser.parse_args(argv)
    audit_path = args.audit_path or args.store.parent / "audit.jsonl"
    try:
        store = SCHEDULES.ScheduleStore(
            args.store, tenant=args.tenant or tenant_of(DEFAULT_DESIRED_STATE),
            audit=lambda action, by, details: GOVERN.record_operator_action(audit_path, action, by, details),
        )
        if args.command == "export":
            print(json.dumps(store.export_desired(), indent=2, sort_keys=True))
            return 0
        definitions = definitions_from(args)
        if args.command == "plan":
            print(json.dumps(store.plan(definitions), indent=2, sort_keys=True))
            return 0
        print(json.dumps(store.reconcile(definitions, by=args.by), indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Runtime schedules failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

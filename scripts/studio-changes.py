#!/usr/bin/env python3
"""List, review, approve, roll back and export Employee Studio change requests from the host (#130).

  studio-changes.py list
  studio-changes.py show cr-0123456789ab
  studio-changes.py propose skill --params '{"skill": "take-message", "enabled": false}' --by owner@client.example
  studio-changes.py approve cr-0123456789ab --by owner@client.example --digest <digest shown by show> [--signoff-file signed.json]
  studio-changes.py reject cr-0123456789ab --by owner@client.example
  studio-changes.py rollback cr-0123456789ab --by owner@client.example
  studio-changes.py signoff cr-0123456789ab --owner owner@client.example --key-file owner.key > signed.json
  studio-changes.py export cr-0123456789ab [--output patch.json]

It reads the same data root, desired state, capability manifest and ``studio`` configuration (``gateway.json``) as the gateway, runs the
same gates (``platform/core/change_requests.py``) and writes the same ledger events, so a change approved here is indistinguishable from
one approved in the Studio. ``approve`` applies a change only when ``--by`` is the configured owner, ``--digest`` is the digest of the
exact diff reviewed and every required evidence item is met; the identity is as asserted on the command line (it is not authenticated),
which is why a relaxing Policy change additionally needs the owner's Ed25519 signature (``signoff``). Applying records the decision and a
rollback snapshot and prints the workspace patch to commit with ``export``; it does not edit the client repository or restart anything.
The private key is read from a file or ``MODEL_GARDEN_OWNER_KEY``, never a command-line argument. No network calls.
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


def _load(name: str, relative: str):
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, ROOT / relative)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


CORE = _load("model_garden_studio_cli_change_requests", "platform/core/change_requests.py")
POLICY = _load("model_garden_studio_cli_policy", "platform/core/policy.py")
STUDIO = _load("model_garden_studio_cli_runtime", "platform/runtime/studio.py")


def _tenant(desired: dict) -> str:
    client = desired.get("source", {}).get("client") if isinstance(desired.get("source"), dict) else None
    return re.sub(r"[^a-z0-9-]+", "-", str(client or "").lower()).strip("-")[:63] or "default"


def _store(args: argparse.Namespace):
    desired = json.loads(args.desired_state.read_text(encoding="utf-8"))
    config = json.loads(args.config.read_text(encoding="utf-8")) if args.config.is_file() else {}
    studio = STUDIO.check_config(config.get("studio") or {})
    govern = _load("model_garden_studio_cli_govern", "scripts/govern-action.py")
    schedules = _load("model_garden_studio_cli_schedules", "platform/core/schedules.py")
    tenant = _tenant(desired)
    audit_path = args.data_root / "audit.jsonl"
    return CORE.load_store(
        args.data_root, desired, tenant=tenant, studio=studio, manifest_path=args.manifest or None,
        audit=lambda action, by, details: govern.record_operator_action(audit_path, action, by, details),
        schedules=schedules.ScheduleStore(args.data_root / "schedules", tenant=tenant).list,
    )


def _print(document) -> None:
    print(json.dumps(document, indent=2, sort_keys=True))


def _shown(change: dict) -> dict:
    return {k: v for k, v in change.items() if k != "rollback"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--desired-state", type=pathlib.Path,
                        default=pathlib.Path(os.environ.get("MODEL_GARDEN_DESIRED_STATE", "/opt/profile-seed/.modelgarden/desired-state.json")))
    parser.add_argument("--data-root", type=pathlib.Path, default=pathlib.Path(os.environ.get("MODEL_GARDEN_DATA_ROOT", "/opt/data/.modelgarden")))
    parser.add_argument("--config", type=pathlib.Path,
                        default=pathlib.Path(os.environ.get("MODEL_GARDEN_GATEWAY_CONFIG", "/opt/profile-seed/.modelgarden/gateway.json")))
    parser.add_argument("--manifest", default=os.environ.get("MODEL_GARDEN_CAPABILITY_MANIFEST", ""), help="capability manifest (readiness evidence)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="every change request, newest first")
    for name in ("show", "export"):
        each = commands.add_parser(name)
        each.add_argument("id")
        if name == "export":
            each.add_argument("--output", type=pathlib.Path)
    propose = commands.add_parser("propose")
    propose.add_argument("kind", choices=CORE.KINDS)
    propose.add_argument("--params", required=True, help="JSON details of the change")
    propose.add_argument("--by", required=True)
    approve = commands.add_parser("approve", help="apply a change; the named owner, the exact digest and all required evidence")
    approve.add_argument("id")
    approve.add_argument("--by", required=True)
    approve.add_argument("--digest", required=True)
    approve.add_argument("--signoff-file", type=pathlib.Path)
    for name in ("reject", "rollback"):
        each = commands.add_parser(name)
        each.add_argument("id")
        each.add_argument("--by", required=True)
    signoff = commands.add_parser("signoff", help="sign a relaxing Policy change with the owner's key and print it")
    signoff.add_argument("id")
    signoff.add_argument("--owner", required=True)
    signoff.add_argument("--key-file", type=pathlib.Path)
    args = parser.parse_args(argv)
    try:
        store = _store(args)
        if args.command == "list":
            _print([store.summary(c) for c in store.list()])
        elif args.command == "show":
            change = store.get(args.id)
            _print({**_shown(change), **({"liveEvidence": store.evidence(change)} if change.get("state") == "proposed" else {})})
        elif args.command == "propose":
            _print(_shown(store.propose(args.kind, json.loads(args.params), by=args.by)))
        elif args.command == "approve":
            attachments = {}
            if args.signoff_file:
                attachments["signoff"] = json.loads(args.signoff_file.read_text(encoding="utf-8"))
            _print(_shown(store.approve(args.id, by=args.by, digest=args.digest, attachments=attachments)))
        elif args.command == "reject":
            _print(_shown(store.reject(args.id, by=args.by)))
        elif args.command == "rollback":
            _print(_shown(store.rollback(args.id, by=args.by)))
        elif args.command == "signoff":
            subject = store.get(args.id).get("signoffSubject")
            if subject is None:
                print("this change does not need a sign-off", file=sys.stderr)
                return 1
            private = args.key_file.read_text(encoding="utf-8").strip() if args.key_file else os.environ.get("MODEL_GARDEN_OWNER_KEY", "")
            print(json.dumps(POLICY.sign_rule(subject, args.owner, private), indent=2, sort_keys=True))
        else:
            patch = store.export_patch(args.id)
            if args.output:
                args.output.write_text(json.dumps(patch, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            else:
                _print(patch)
    except CORE.ChangeRefused as exc:
        print(f"refused: {', '.join(exc.reasons)}", file=sys.stderr)
        return 1
    except (CORE.ChangeError, STUDIO.StudioConfigError, ValueError, OSError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

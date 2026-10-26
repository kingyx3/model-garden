#!/usr/bin/env python3
"""Pause or resume the AI coworker, or show whether it is paused (run inside governed-tools).

  runtime-pause.py ROOT status
  runtime-pause.py ROOT pause --by jordan@client.example --reason "wrong customer data in the CRM"
  runtime-pause.py ROOT resume --by jordan@client.example

ROOT is the governance directory. While paused, every non-read Tool is denied at the governed boundary and ledgered, the worker runs no
scheduled, event, mailbox or CRM work, and the gateway answers with a holding message. A call already past the check finishes; nothing
new starts. Pause and resume are ledgered (``coworker.pause`` / ``coworker.resume``) with who, when and why.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PAUSE = _load("runtime_pause_store", ROOT / "platform" / "core" / "pause.py")
GOVERN = _load("runtime_pause_govern", ROOT / "scripts" / "govern-action.py")


def main(argv: list[str] | None = None, *, now: int | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=pathlib.Path)
    parser.add_argument("--audit-path", type=pathlib.Path, default=pathlib.Path(
        os.environ.get("MODEL_GARDEN_AUDIT_PATH", "/opt/data/.modelgarden/audit.jsonl")))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    pausing = commands.add_parser("pause")
    pausing.add_argument("--by", required=True)
    pausing.add_argument("--reason", default="")
    pausing.add_argument("--wait-seconds", type=float, default=PAUSE.DEFAULT_OWNER_LOCK_WAIT_SECONDS,
                         help="maximum seconds to wait for admitted material effects (default: 5, max: 30)")
    resuming = commands.add_parser("resume")
    resuming.add_argument("--by", required=True)
    resuming.add_argument("--wait-seconds", type=float, default=PAUSE.DEFAULT_OWNER_LOCK_WAIT_SECONDS,
                          help="maximum seconds to wait for admitted material effects (default: 5, max: 30)")
    args = parser.parse_args(argv)
    store = PAUSE.PauseStore(args.root)
    moment = int(time.time()) if now is None else now
    try:
        if args.command == "status":
            print(json.dumps(store.status(), sort_keys=True))
            return 0
        if args.command == "pause":
            state = store.pause(args.by, args.reason, moment, lock_timeout_seconds=args.wait_seconds)
            GOVERN.record_operator_action(args.audit_path, "coworker.pause", state["by"], {"reason": state.get("reason", ""), "via": "operator", "at": moment})
        else:
            state = store.resume(args.by, moment, lock_timeout_seconds=args.wait_seconds)
            GOVERN.record_operator_action(args.audit_path, "coworker.resume", state["by"], {"via": "operator", "at": moment})
    except TimeoutError:
        # A pending pause is NOT a pause. Record an explicit unconfirmed
        # attempt without implying provider cancellation or a successful hold.
        try:
            GOVERN.record_operator_action(args.audit_path, f"coworker.{args.command}-unconfirmed",
                                          args.by.strip(), {"via": "operator", "at": moment, "reason": "inflight-lock-timeout"})
        except Exception:
            print("error: pause/resume not confirmed; audit unavailable; isolate runtime if urgent", file=sys.stderr)
            return 3
        print("error: pause/resume not confirmed; in-flight work may continue. Check status, isolate runtime if urgent, then retry.", file=sys.stderr)
        return 3
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(state, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

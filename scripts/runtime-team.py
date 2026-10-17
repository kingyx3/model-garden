#!/usr/bin/env python3
"""Prepare a team of Agents for a governed runtime: per-Agent state, readiness and one sidecar per Agent (run on the host).

  runtime-team.py prepare compiled.json --base /opt/data/.modelgarden --environment-binding env.yaml
  runtime-team.py prepare compiled.json --base DIR --environment-binding env.yaml --grants-root DIR/provider-grants --tenant acme-prod
  runtime-team.py prepare compiled.json --base DIR --environment-binding env.yaml --base-port 9120 --ledger-deployments owner@client.example

`compiled.json` is `compile-workspace.py` output (several Agents, or one). The command writes each Agent's desired state and capability
manifest under `<base>/agents/<name>/`, checks readiness and prints the launch plan: one governed sidecar per Agent with its own state,
approval root and port, all appending to the one ledger at `<base>/audit.jsonl` (`platform/runtime/team_runtime.py`).

More than one Agent starts only when each needing an external connector family holds a usable grant of its own (give `--grants-root`
and `--tenant`); otherwise it exits 1 naming each blocker and writes nothing. One Agent is unchanged. `--ledger-deployments` also
records each Agent's policy and knowledge changes since last applied, under the named person. No credential is read or printed.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def prepare(args: argparse.Namespace) -> int:
    team_runtime = _load("modelgarden_team_runtime_cli", "platform/runtime/team_runtime.py")
    compiler = _load("modelgarden_compile_workspace_team_cli", "scripts/compile-workspace.py")
    try:
        states = team_runtime.load_states(args.compiled)
        binding = None
        if args.environment_binding:
            binding = compiler.load_environment_binding(args.environment_binding)
            binding = binding if "connectors" in binding else (binding.get("spec") or binding)
        registry = None
        if args.grants_root:
            if not args.tenant:
                raise ValueError("--grants-root needs --tenant")
            registry = _load("modelgarden_provider_grants_team_cli", "platform/core/provider_grants.py").GrantRegistry(args.grants_root, tenant=args.tenant)
        team = team_runtime.TeamRuntime(states, args.base, environment_binding=binding, registry=registry)
        output = {"mode": team.report["mode"], "ready": team.report["ready"], "agents": team.report["agents"],
                  "launch": team.launch_plan(base_port=args.base_port)}
        if args.ledger_deployments:
            output["deployments"] = team.ledger_deployments(args.ledger_deployments)
    except (OSError, ValueError, PermissionError) as exc:
        print(f"Team not prepared: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("prepare", help="write per-Agent state and print the readiness and launch plan")
    command.add_argument("compiled", type=pathlib.Path)
    command.add_argument("--base", type=pathlib.Path, required=True, help="governance directory holding the shared ledger")
    command.add_argument("--environment-binding", type=pathlib.Path)
    command.add_argument("--grants-root", type=pathlib.Path, help="per-coworker provider grant registry directory")
    command.add_argument("--tenant", help="client-environment slug the grant registry belongs to, such as acme-prod")
    command.add_argument("--base-port", type=int, default=9120)
    command.add_argument("--ledger-deployments", metavar="PERSON", help="ledger each Agent's policy and knowledge changes under this person")
    command.set_defaults(handler=prepare)
    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Prepare a team of Agents for a governed runtime: per-Agent state, readiness and one sidecar per Agent (run on the host).

  runtime-team.py prepare compiled.json --base /opt/data/.modelgarden --environment-binding env.yaml
  runtime-team.py prepare compiled.json --base DIR --environment-binding env.yaml --grants-root DIR/provider-grants --tenant acme-prod
  runtime-team.py prepare compiled.json --base DIR --environment-binding env.yaml --base-port 9120 --ledger-deployments owner@client.example
  runtime-team.py assign compiled.json --base DIR --environment-binding env.yaml --owners owners.json --person jordan@client.example \\
      --agent account-researcher --goal "Find the customer's open enquiries" --capability crm.read --budget-steps 10
  runtime-team.py inbox compiled.json --base DIR --environment-binding env.yaml --agent account-researcher
  runtime-team.py result compiled.json --base DIR --environment-binding env.yaml --person jordan@client.example --task task-1a2b3c

`compiled.json` is `compile-workspace.py` output (several Agents, or one). The command writes each Agent's desired state and capability
manifest under `<base>/agents/<name>/`, checks readiness and prints the launch plan: one governed sidecar per Agent with its own state,
approval root and port, all appending to the one ledger at `<base>/audit.jsonl` (`platform/runtime/team_runtime.py`).

More than one Agent starts only when each needing an external connector family holds a usable grant of its own (give `--grants-root`
and `--tenant`); otherwise it exits 1 naming each blocker and writes nothing. One Agent is unchanged. `--ledger-deployments` also
records each Agent's policy and knowledge changes since last applied, under the named person. No credential is read or printed.

``assign`` is a person handing a task to any Agent of the team: the Agent can do only what it holds and the person may ask for
(``--owners`` is a JSON file of ``{person: [Tool names]}``, or a worker config with an ``owners`` object). The task is saved under
``--base`` and lands in the Agent's inbox, where the process running the Agents finds it. On the host the person is as asserted, like the
other operator commands; ``result`` shows the report only to that person. Nothing here runs the Agent.
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


def _team(args: argparse.Namespace, **extra):
    team_runtime = _load("modelgarden_team_runtime_cli", "platform/runtime/team_runtime.py")
    compiler = _load("modelgarden_compile_workspace_team_cli", "scripts/compile-workspace.py")
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
    return team_runtime, team_runtime.TeamRuntime(states, args.base, environment_binding=binding, registry=registry, **extra)


def _owners(path: pathlib.Path) -> dict[str, list[str]]:
    document = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    owners = document.get("owners") if isinstance(document, dict) and isinstance(document.get("owners"), dict) else document
    if not isinstance(owners, dict) or not all(isinstance(k, str) and isinstance(v, list) for k, v in owners.items()):
        raise ValueError("--owners is a JSON object of person -> list of Tool names (or a worker config with owners)")
    return {person: [str(tool) for tool in tools] for person, tools in owners.items()}


def assign(args: argparse.Namespace) -> int:
    try:
        owners = _owners(args.owners)
        _, team = _team(args, permissions_for=lambda person: owners.get(person, []))
        budget = {key: value for key, value in (("steps", args.budget_steps), ("tokens", args.budget_tokens), ("costCents", args.budget_cost_cents)) if value}
        envelope = team.assign_task(args.person, args.agent, goal=args.goal, task_id=args.task_id, required_capabilities=args.capability or None,
                                    budget=budget or None)
    except (OSError, ValueError, PermissionError) as exc:
        print(f"Task not assigned: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"taskId": envelope["taskId"], "agent": envelope["agent"], "authority": envelope["authority"], "budget": envelope["budget"]}, indent=2, sort_keys=True))
    return 0


def inbox(args: argparse.Namespace) -> int:
    try:
        _, team = _team(args)
        rows = team.inbox(args.agent)
    except (OSError, ValueError, PermissionError) as exc:
        print(f"Inbox not read: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(rows, indent=2, sort_keys=True))
    return 0


def result(args: argparse.Namespace) -> int:
    try:
        _, team = _team(args)
        status = team.task_result(args.person, args.task)
    except (OSError, ValueError, PermissionError) as exc:
        print(f"No such task: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0


def prepare(args: argparse.Namespace) -> int:
    try:
        team_runtime, team = _team(args)
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

    def team_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("compiled", type=pathlib.Path)
        command.add_argument("--base", type=pathlib.Path, required=True, help="governance directory holding the shared ledger and task state")
        command.add_argument("--environment-binding", type=pathlib.Path)
        command.add_argument("--grants-root", type=pathlib.Path)
        command.add_argument("--tenant")

    command = sub.add_parser("assign", help="a person hands a task to any Agent of the team")
    team_arguments(command)
    command.add_argument("--owners", type=pathlib.Path, required=True, help="JSON of person -> Tool names the person may ask for")
    command.add_argument("--person", required=True)
    command.add_argument("--agent", required=True)
    command.add_argument("--goal", required=True)
    command.add_argument("--task-id")
    command.add_argument("--capability", action="append", help="a Tool the task needs (repeatable); default is everything the person and Agent share")
    command.add_argument("--budget-steps", type=int)
    command.add_argument("--budget-tokens", type=int)
    command.add_argument("--budget-cost-cents", type=int)
    command.set_defaults(handler=assign)
    command = sub.add_parser("inbox", help="what is waiting for an Agent")
    team_arguments(command)
    command.add_argument("--agent", required=True)
    command.set_defaults(handler=inbox)
    command = sub.add_parser("result", help="the state and report of a task a person handed over")
    team_arguments(command)
    command.add_argument("--person", required=True)
    command.add_argument("--task", required=True)
    command.set_defaults(handler=result)
    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())

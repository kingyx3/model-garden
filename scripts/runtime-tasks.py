#!/usr/bin/env python3
"""Submit, run and inspect multi-Agent task graphs on a governed runtime (run inside governed-tools or the worker).

  runtime-tasks.py /opt/data/.modelgarden submit --spec graph.json --authority authority.json [--by alice@client.example]
  runtime-tasks.py /opt/data/.modelgarden submit --spec graph.json --desired-state a.json --desired-state b.json --permissions people.json
  runtime-tasks.py /opt/data/.modelgarden validate --spec graph.json --authority authority.json   # same checks, stores nothing
  runtime-tasks.py /opt/data/.modelgarden advance <graph> --authority authority.json --coworker-url-template 'http://{agent}-coworker-api:8642/v1'
  runtime-tasks.py /opt/data/.modelgarden advance <graph> --authority authority.json     # only opens and resumes human steps
  runtime-tasks.py /opt/data/.modelgarden status <graph>
  runtime-tasks.py /opt/data/.modelgarden list
  runtime-tasks.py /opt/data/.modelgarden timeline <graph>
  runtime-tasks.py /opt/data/.modelgarden cancel <graph> --by alice@client.example

The first argument is the runtime data root (`/opt/data/.modelgarden`): graphs live in `<root>/tasks`, the human steps of a graph are
ordinary Tier 4 hand-offs in `<root>/handoffs` (so the worker's notice delivery, `runtime-handoff.py` and the reply path tell and
answer the person), and every step is ledgered in `<root>/audit.jsonl` (`--audit-path` to change it).

Authority is credential-free JSON: `--authority` is `{"agents": {"writer": ["docs.create"]}, "principals": {"alice@x": ["crm.read"]}}`,
and each `--desired-state` adds the Agent named by its `source.name` with the Tools it selected; `--permissions` adds
`{"principal": [Tool...]}`. A graph is rejected whole when any node would hold more than its Agent, its delegator and the requester
all allow (see `platform/core/task_graph.py`). `advance` needs the same authority again because a node's authority is re-narrowed to
what is held when it starts. Running Agent steps needs a reachable coworker API per Agent and `MODEL_GARDEN_COWORKER_API_KEY`
(the deployment's API_SERVER_KEY); without `--coworker-url*` `advance` runs no Agent step. The owner is told about a failed or
out-of-budget graph through the worker's notice queue. Exit status is 0 for a stored/valid/running or succeeded graph, 1 for an
error, rejection or a graph that failed or was cancelled.
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


GOVERN = _load("runtime_tasks_govern", ROOT / "scripts" / "govern-action.py")
GRAPH = _load("runtime_tasks_graph", ROOT / "platform" / "core" / "task_graph.py")
TEAM = _load("runtime_tasks_team", ROOT / "platform" / "core" / "team.py")


def authority_from(args: argparse.Namespace) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    agents: dict[str, list[str]] = {}
    principals: dict[str, list[str]] = {}
    if args.authority:
        document = json.loads(args.authority.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or set(document) - {"agents", "principals"}:
            raise ValueError("authority is an object with agents and principals")
        agents.update(document.get("agents", {}))
        principals.update(document.get("principals", {}))
    for path in args.desired_state or []:
        state = json.loads(path.read_text(encoding="utf-8"))
        agents[state["source"]["name"]] = TEAM.tool_names(state)
    if args.permissions:
        principals.update(json.loads(args.permissions.read_text(encoding="utf-8")))
    for label, table in (("agents", agents), ("principals", principals)):
        if not all(isinstance(key, str) and isinstance(value, list) and all(isinstance(item, str) for item in value) for key, value in table.items()):
            raise ValueError(f"authority {label} map a name to a list of Tool names")
    return agents, principals


def build_store(args: argparse.Namespace, *, need_authority: bool = True):
    data = args.root
    audit_path = args.audit_path or data / "audit.jsonl"
    agents, principals = authority_from(args) if need_authority else ({}, {})

    def grants_for(agent: str):
        if agent not in agents:
            raise GRAPH.TaskGraphError(f"no authority is declared for Agent {agent!r}")
        return agents[agent]

    def notify(notice: dict) -> None:
        worker = _load("runtime_tasks_worker", ROOT / "platform" / "runtime" / "worker.py")
        worker.NoticeQueue(data / "worker").add(notice["kind"], notice.get("to"), notice["text"], reference=str(notice.get("graphId", ""))[:40])

    return GRAPH.TaskGraphStore(
        data / "tasks", grants_for=grants_for, permissions_for=lambda who: principals.get(who, []), handoff_root=data / "handoffs",
        audit=lambda action, by, details: GOVERN.record_operator_action(audit_path, action, by, details), notify=notify,
        clock=lambda: int(time.time()),
    ), audit_path


def coworker_runner(args: argparse.Namespace):
    if not args.coworker_url and not args.coworker_url_template:
        return None
    key = os.environ.get("MODEL_GARDEN_COWORKER_API_KEY", "")
    client_module = _load("runtime_tasks_coworker", ROOT / "platform" / "runtime" / "coworker_client.py")

    def client_for(agent: str):
        url = args.coworker_url_template.format(agent=agent) if args.coworker_url_template else args.coworker_url
        return client_module.CoworkerClient(url, key)

    return GRAPH.coworker_runner(client_for)


def timeline(audit_path: pathlib.Path, graph_id: str) -> list[dict]:
    events = []
    if audit_path.is_file():
        for line in audit_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            details = event.get("details") if isinstance(event.get("details"), dict) else {}
            if details.get("graphId") == graph_id or details.get("taskId") == graph_id:
                events.append({"sequence": event.get("sequence"), "timestamp": event.get("timestamp"), "action": event.get("action"),
                               "by": event.get("performedBy"), "node": details.get("node"), "reason": details.get("reason"),
                               "taskId": details.get("taskId")})
    return events


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=pathlib.Path, help="runtime data root, for example /opt/data/.modelgarden")
    parser.add_argument("--audit-path", type=pathlib.Path, help="ledger path (default: <root>/audit.jsonl)")
    sub = parser.add_subparsers(dest="command", required=True)

    def authority(command: argparse.ArgumentParser) -> None:
        command.add_argument("--authority", type=pathlib.Path, help='JSON {"agents": {name: [Tool]}, "principals": {name: [Tool]}}')
        command.add_argument("--desired-state", type=pathlib.Path, action="append", help="a compiled Agent desired state (repeatable)")
        command.add_argument("--permissions", type=pathlib.Path, help='JSON {"principal": [Tool]}')

    for name, help_text in (("submit", "validate and store a task graph"), ("validate", "run the submission checks and store nothing")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--spec", type=pathlib.Path, required=True)
        command.add_argument("--by", help="who submitted it (default: the requesting principal)")
        authority(command)
    advance = sub.add_parser("advance", help="run what is ready (in parallel) and resume human steps")
    advance.add_argument("graph")
    advance.add_argument("--workers", type=int, default=4)
    advance.add_argument("--coworker-url")
    advance.add_argument("--coworker-url-template", help="URL with {agent}, for a coworker API per Agent")
    authority(advance)
    for name in ("status", "timeline"):
        sub.add_parser(name).add_argument("graph")
    sub.add_parser("list")
    cancel = sub.add_parser("cancel", help="cancel everything not finished")
    cancel.add_argument("graph")
    cancel.add_argument("--by", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            store, _ = build_store(args, need_authority=False)
            print(json.dumps(store.graphs(), indent=2, sort_keys=True))
            return 0
        if args.command in ("status", "timeline", "cancel"):
            store, audit_path = build_store(args, need_authority=False)
            if args.command == "timeline":
                store.status(args.graph)
                print(json.dumps(timeline(audit_path, args.graph), indent=2, sort_keys=True))
                return 0
            result = store.cancel(args.graph, by=args.by) if args.command == "cancel" else store.status(args.graph)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 1 if result["status"] in ("failed", "cancelled") else 0
        if args.command in ("submit", "validate"):
            document = json.loads(args.spec.read_text(encoding="utf-8"))
            store, _ = build_store(args)
            if args.command == "validate":
                # A throwaway store: the checks and the rejection ledger entry are real, nothing is kept.
                import tempfile
                with tempfile.TemporaryDirectory() as scratch:
                    probe = GRAPH.TaskGraphStore(pathlib.Path(scratch), grants_for=store._grants_for, permissions_for=store._permissions_for,
                                                 audit=lambda action, by, details: store._audit(action, by, details) if action == "taskgraph.rejected" else None,
                                                 clock=store._clock)
                    result = probe.submit(document, submitted_by=args.by)
                result["validated"] = True
            else:
                result = store.submit(document, submitted_by=args.by)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        store, _ = build_store(args)
        result = store.advance(args.graph, coworker_runner(args), max_workers=args.workers)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1 if result["status"] in ("failed", "cancelled") else 0
    except (OSError, ValueError, KeyError, PermissionError, json.JSONDecodeError) as exc:
        print(f"Runtime tasks failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

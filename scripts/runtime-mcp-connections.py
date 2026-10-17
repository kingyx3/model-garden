#!/usr/bin/env python3
"""Stage, review, approve, activate, test or immediately disable a tenant-private MCP or custom API connection.

Runs only in the governed sidecar container (it shares the private state and, for ``review``/``approve``/``activate``/``test`` of an
MCP server, uses the sidecar's pinned transport and credentials). No credential is supplied through this CLI and none is printed.

  stage     record a reviewed declaration as inactive (no network)
  review    list each declared operation as the server (or, for a custom API, the definition) offers it now: risk, fingerprint,
            approval state and blocking metadata findings; nothing is changed
  approve   pin the fingerprints of the declared operations exactly as offered now (refused while any has a blocking finding)
  activate  record the named client owner's consent and activate; needs every operation approved and the coworker's own grant,
            and the owner and the operator must be different people
  test      a connection test: the declared operations are offered at their approved pins, and with ``--operation`` one *read*
            operation runs through the governed executor (grant, pin and address checks). The coworker's grant health is set to
            healthy or failed; the result body is never printed, only its size and receipt
  disable   disable one coworker's connection immediately

Every change is ledgered under the named actor (#87, #110, #159).
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
MAX_MANIFEST_BYTES = 65536


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CONNECTIONS = _load("modelgarden_mcp_operator_connections", ROOT / "platform/core/mcp_connections.py")
GRANTS = _load("modelgarden_mcp_operator_grants", ROOT / "platform/core/provider_grants.py")
GOVERN = _load("modelgarden_mcp_operator_governance", ROOT / "scripts/govern-action.py")
EXECUTOR = CONNECTIONS._gateway()
MCP = CONNECTIONS._mcp()
REVIEW = MCP._review()


def _transports():
    return _load("modelgarden_mcp_operator_upstream_http", ROOT / "platform/runtime/upstream_http.py")


def _offered(server: dict, approvals, *, environ=None, transports=None) -> list[dict]:
    """The operations as offered now: a custom API's definition, or an MCP server's live listing through the pinned transport."""
    if "api" in server:
        return MCP._custom_api().operation_tools(server["api"])
    transports = transports or _transports()
    environ = os.environ if environ is None else environ
    qualified = EXECUTOR._endpoint().qualify(server["url"], transports.system_dns_resolver)
    credential = transports.secret_resolver(environ)(server["credentialRef"])
    with transports.transport_factory(server["url"], qualified["pinned"][0], credential) as session:
        return session.list_tools()


def _review(server: dict, offered: list[dict], approvals) -> list[dict]:
    rows = []
    for upstream, entry in sorted(server["tools"].items()):
        matches = [tool for tool in offered if isinstance(tool, dict) and tool.get("name") == upstream]
        if len(matches) != 1:
            rows.append({"operation": upstream, "tool": entry["tool"], "risk": entry["risk"], "state": "absent" if not matches else "duplicated"})
            continue
        tool = matches[0]
        rows.append({"operation": upstream, "tool": entry["tool"], "risk": entry["risk"], "fingerprint": MCP.fingerprint(tool),
                     "state": approvals.status(server["name"], tool), "blocking": REVIEW.blocking_codes(REVIEW.review(tool)),
                     "description": str(tool.get("description", ""))[:300]})
    return rows


def _declaration(path: pathlib.Path) -> dict:
    """Read an exact non-secret declaration; refuse special files and symlinks."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("declaration must be a regular JSON file of at most 64 KiB")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("declaration must be a JSON object")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store", type=pathlib.Path, help="private governance root, e.g. /opt/data/.modelgarden")
    parser.add_argument("--tenant", required=True, help="exact client-environment slug, e.g. acme-prod")
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="list non-secret connection status for this coworker")
    status.add_argument("--agent", required=True)
    status.add_argument("--check-grants", action="store_true", help="redacted per-Tool readiness; no network test or live assertion")
    stage = sub.add_parser("stage", help="record reviewed manifest as inactive; never makes a network connection")
    stage.add_argument("--agent", required=True)
    stage.add_argument("--id", required=True, dest="connection_id")
    stage.add_argument("--file", type=pathlib.Path, required=True, dest="declaration_file")
    stage.add_argument("--actor", required=True)
    stage.add_argument("--replace", action="store_true", help="replace a staged or active declaration and deactivate it")
    review = sub.add_parser("review", help="show each declared operation as offered now, with approval state and findings")
    approve = sub.add_parser("approve", help="pin the declared operations' fingerprints as offered now")
    activate = sub.add_parser("activate", help="activate with the named client owner's consent")
    activate.add_argument("--owner-consent", required=True, help="the client owner named in the declaration, who consented")
    test = sub.add_parser("test", help="connection test: pins hold, and optionally run one read operation")
    test.add_argument("--operation", help="a declared read operation to run")
    test.add_argument("--arguments", default="{}", help="JSON arguments for --operation (no secrets)")
    for each in (review, approve, activate, test):
        each.add_argument("--agent", required=True)
        each.add_argument("--id", required=True, dest="connection_id")
    for each in (approve, activate, test):
        each.add_argument("--actor", required=True)
    disable = sub.add_parser("disable", help="disable one coworker's connection immediately")
    disable.add_argument("--agent", required=True)
    disable.add_argument("--id", required=True, dest="connection_id")
    disable.add_argument("--actor", required=True)
    args = parser.parse_args(argv)

    try:
        store = CONNECTIONS.McpConnectionStore(
            args.store / "mcp-connections", tenant=args.tenant,
            audit=lambda action, by, details: GOVERN.record_operator_action(
                args.store / "audit.jsonl", action, by, details
            ),
        )
        if args.command == "status":
            if args.check_grants:
                registry = GRANTS.GrantRegistry(args.store / "provider-grants", tenant=args.tenant)
                outcome = {"connections": store.list_readiness(agent=args.agent, grants=registry, now=int(time.time()))}
            else:
                outcome = {"connections": store.list_public(agent=args.agent)}
        elif args.command == "stage":
            declaration = _declaration(args.declaration_file)
            outcome = store.stage(args.connection_id, agent=args.agent, declaration=declaration,
                                  by=args.actor, now=int(time.time()), replace=args.replace)
        elif args.command == "disable":
            outcome = store.disable(args.connection_id, agent=args.agent, by=args.actor)
        else:
            outcome = _lifecycle(args, store)
        print(json.dumps(outcome, indent=2, sort_keys=True))
        return 0
    except (CONNECTIONS.ConnectionRefused, ValueError, OSError, json.JSONDecodeError, PermissionError, RuntimeError) as exc:
        # Never print the manifest or raw credential; only controlled error descriptions. Refusals raised by our own governance
        # checks (permission errors) carry fixed wording and operation names, which the operator needs to act on.
        detail = f": {str(exc)[:300]}" if isinstance(exc, PermissionError) else ""
        print(f"MCP operator command failed: {type(exc).__name__}{detail}", file=sys.stderr)
        return 2


def _lifecycle(args: argparse.Namespace, store, *, environ=None, transports=None) -> dict:
    audit = lambda action, by, details: GOVERN.record_operator_action(args.store / "audit.jsonl", action, by, details)  # noqa: E731
    approvals = MCP.SchemaApprovals(args.store / "mcp-schema-approvals", tenant=args.tenant, audit=audit)
    grants = GRANTS.GrantRegistry(args.store / "provider-grants", tenant=args.tenant, audit=audit)
    staged = store.staged(args.connection_id, agent=args.agent)
    server = MCP.validate_server(staged["declaration"])
    offered = _offered(server, approvals, environ=environ, transports=transports)
    now = int(time.time())
    if args.command == "review":
        return {"connection": args.connection_id, "active": staged["active"], "operations": _review(server, offered, approvals)}
    if args.command == "approve":
        rows = _review(server, offered, approvals)
        unusable = [row["operation"] for row in rows if row["state"] in ("absent", "duplicated") or row.get("blocking")]
        if unusable:
            raise PermissionError("these operations are absent, duplicated or have unsafe metadata: " + ", ".join(unusable))
        for upstream in sorted(server["tools"]):
            approvals.approve(server["name"], next(tool for tool in offered if tool.get("name") == upstream), by=args.actor, now=now)
        return {"connection": args.connection_id, "approved": sorted(server["tools"])}
    if args.command == "activate":
        return store.activate(args.connection_id, agent=args.agent, consented_by=args.owner_consent, by=args.actor,
                              offered=offered, approvals=approvals, grants=grants, now=now)
    return _test(args, store, server, offered, approvals, grants, audit, environ=environ, transports=transports, now=now)


def _test(args, store, server, offered, approvals, grants, audit, *, environ, transports, now) -> dict:
    rows = _review(server, offered, approvals)
    problems = [row["operation"] for row in rows if row["state"] != "approved" or row.get("blocking")]
    report: dict = {"connection": args.connection_id, "operations": {row["operation"]: row["state"] for row in rows}}
    grant_id = None
    ok = not problems
    if ok and args.operation:
        entry = server["tools"].get(args.operation)
        if entry is None or entry["risk"] != "read":
            raise PermissionError("a connection test runs only a declared read operation")
        arguments = json.loads(args.arguments)
        connection = store.resolve(args.connection_id, agent=args.agent)

        def check():
            prepared = EXECUTOR.prepare(connection["declaration"], registry=grants, tenant=args.tenant, attested_agent=args.agent,
                                        upstream=args.operation, enabled_tools=(entry["tool"],), originator_permissions=(entry["tool"],),
                                        now=now, connection_id=args.connection_id)
            current = store.resolve(args.connection_id, agent=args.agent)
            return {**prepared, "activationId": current["activationId"], "connectionRevision": current["revision"],
                    "reviewedFingerprints": current["reviewedFingerprints"]}

        route = check()
        grant_id = route["grant"]["grantId"]
        transports = transports or _transports()
        environ = os.environ if environ is None else environ
        transport = transports.transport_factory
        if "api" in server:
            def transport(url, pinned_ip, credential):  # noqa: E306 - the reviewed definition, never a typed URL
                return transports.rest_transport_factory(server["api"], pinned_ip, credential, approvals)
        try:
            result = EXECUTOR.invoke(route, arguments, approvals=approvals, transport_factory=transport,
                                     secret_resolver=transports.secret_resolver(environ), dns_resolver=transports.system_dns_resolver, recheck=check)
            report.update(called=args.operation, resultBytes=len(json.dumps(result)), receipt=EXECUTOR.receipt_reference(result))
        except Exception as exc:  # noqa: BLE001 - report the class only; upstream text can carry data or credentials
            ok = False
            report.update(called=args.operation, error=type(exc).__name__)
            # Only a credential or transport failure says the connection is broken; a mistyped argument or an upstream
            # "not found" is a failed test, never a reason to cut off the coworker's live grant.
            unreachable = (isinstance(exc, RuntimeError) and not isinstance(exc, EXECUTOR.UpstreamToolError)) or (
                isinstance(exc, EXECUTOR.GatewayError) and str(exc) == "upstream credential is unavailable")
            if unreachable:
                grants.set_health(grant_id, "failed", by=args.actor)
                report["grantHealth"] = "failed"
    if problems:
        report["notReady"] = problems
    audit("mcp.connection-test", args.actor, {"tenant": args.tenant, "agent": args.agent, "connectionId": args.connection_id,
                                              "operation": args.operation, "outcome": "passed" if ok else "failed",
                                              **({"receipt": report["receipt"]} if report.get("receipt") else {})})
    report["outcome"] = "passed" if ok else "failed"
    return report


if __name__ == "__main__":
    raise SystemExit(main())

"""Credential-free checks for ledger query, task timeline, policy replay, sandbox dry-run, export and the operator CLI."""
from __future__ import annotations

import copy
import csv
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


audit = _load("audit_query_under_test", "platform/core/audit_query.py")
govern = _load("govern_for_audit_query_tests", "scripts/govern-action.py")
TASK = {"taskId": "task-1", "correlationId": "corr-1", "channel": "slack", "originatingPrincipal": "alice@acme.example"}


def build_ledger(root: pathlib.Path):
    path = root / "desired-state.json"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "compile-workspace.py"), str(ROOT / "examples" / "workspace"),
                    "--agent", "receptionist", "--output", str(path)], check=True, capture_output=True)
    state = json.loads(path.read_text())
    ledger = root / "audit.jsonl"
    govern.execute_action(state, "kb.search", {"query": "hours"}, lambda a: {"documents": []}, audit_path=ledger,
                          initiating_user="alice@acme.example", governance_context=TASK, principal_access="member",
                          ledger={"provider": "text-knowledge", "tier": 0})
    request = govern.build_action_request(state, "crm.create", {"name": "Acme"}, initiating_user="alice@acme.example", governance_context=TASK)
    govern.execute_action(state, "crm.create", {"name": "Acme"}, lambda a: {"id": "c-1"}, audit_path=ledger, initiating_user="alice@acme.example",
                          governance_context=TASK, principal_access="member",
                          approval={"requestId": request["requestId"], "approved": True, "approver": "bob@acme.example"},
                          external_object_ref=lambda r: "crm:c-1")
    govern.execute_action(state, "kb.search", {"query": "other"}, lambda a: {"documents": []}, audit_path=ledger,
                          initiating_user="unmapped:slack:abc", governance_context={"taskId": "task-2", "channel": "slack"}, principal_access="public")
    return state, ledger


class AuditQueryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.state, self.ledger = build_ledger(self.root)
        self.events = audit.read_ledger(self.ledger)

    def test_filters_by_agent_principal_approver_capability_object_and_time(self):
        found = lambda **kw: audit.query(self.events, audit.Filter(**kw))
        self.assertTrue(found(agent="receptionist"))
        self.assertEqual({e["tool"] for e in found(approver="bob@acme.example")}, {"crm.create"})
        self.assertEqual({e["tool"] for e in found(external_object="crm:c-1")}, {"crm.create"})
        self.assertTrue(all(e["ledger"]["capability"] == "kb" for e in found(capability="kb")))
        self.assertEqual({e["initiatingUser"] for e in found(principal="unmapped:slack:abc")}, {"unmapped:slack:abc"})
        self.assertEqual(found(since="2999-01-01"), [])
        self.assertTrue(found(until="2999-01-01"))
        self.assertEqual(found(agent="someone-else"), [])
        with self.assertRaises(audit.AuditQueryError):
            found(since="yesterday-ish")

    def test_timeline_joins_one_task_and_totals_usage(self):
        result = audit.timeline(self.events, task="task-1")
        self.assertEqual(result["agents"], ["receptionist"])
        self.assertEqual([r["tool"] for r in result["requests"]], ["kb.search", "crm.create"])
        order = [(s["tool"], s["event"]) for s in result["steps"]]
        self.assertLess(order.index(("crm.create", "approval")), order.index(("crm.create", "execution")))
        self.assertIn(("kb.search", "read"), order)
        self.assertNotIn("unmapped:slack:abc", json.dumps(result))
        with self.assertRaises(audit.AuditQueryError):
            audit.timeline(self.events)
        synthetic = [{**e, "details": {**(e.get("details") or {}), "usage": {"tokens": 10, "costCents": 2}}} for e in self.events if e["event"] == "execution"]
        self.assertEqual(audit.timeline(synthetic, task="task-1")["usage"], {"tokens": 20, "costCents": 4, "steps": 0})

    def test_timeline_includes_the_operator_events_that_name_the_task(self):
        for action, by, details in (
            ("delegation.hop", "agent:coordinator", {"fromTask": "task-1", "toTask": "task-1.1", "fromAgent": "coordinator", "toAgent": "receptionist",
                                                     "depth": 1, "chain": ["coordinator", "receptionist"], "goalDigest": "a" * 64}),
            ("task.human-step.resolved", "jordan@acme.example", {"taskId": "task-1", "step": "approve", "outcome": "done", "noteDigest": "b" * 64}),
            ("task.human-step.opened", "agent:coordinator", {"taskId": "task-2", "step": "other", "outcome": "opened"}),
        ):
            govern.record_operator_action(self.ledger, action, by, details)
        events = audit.read_ledger(self.ledger)
        result = audit.timeline(events, task="task-1")
        named = [(step["action"], step["by"]) for step in result["steps"] if step.get("action")]
        self.assertEqual(named, [("delegation.hop", "agent:coordinator"), ("task.human-step.resolved", "jordan@acme.example")])
        self.assertEqual(result["agents"], ["coordinator", "receptionist"])
        hop = next(step for step in result["steps"] if step.get("action") == "delegation.hop")
        self.assertEqual(hop["details"]["toTask"], "task-1.1")
        self.assertNotIn("goalDigest", hop["details"])  # only the identifying fields are shown
        self.assertEqual([step["action"] for step in audit.timeline(events, task="task-1.1")["steps"] if step.get("action")], ["delegation.hop"])
        other = audit.query(events, audit.Filter(task="task-2"))
        self.assertIn("task.human-step.opened", [event.get("action") for event in other])
        self.assertNotIn("delegation.hop", [event.get("action") for event in other])

    def test_policy_replay_reports_only_real_differences(self):
        same = audit.replay_policy(self.events, self.state)
        self.assertEqual(same["different"], 0)
        self.assertEqual(same["checked"], 3)
        loosened = copy.deepcopy(self.state)
        loosened["policies"][0]["spec"]["rules"] = [r for r in loosened["policies"][0]["spec"]["rules"] if r["id"] != "approve-external-write"]
        # the tool still needs approval by its own flag, so tighten instead: deny the write outright
        loosened["policies"][0]["spec"]["rules"].append({"id": "deny-writes", "effect": "deny", "actions": ["*.create"]})
        changed = audit.replay_policy(self.events, loosened)
        self.assertEqual(changed["different"], 1)
        difference = changed["differences"][0]
        self.assertEqual((difference["tool"], difference["recorded"]["decision"], difference["replayed"]["decision"]),
                         ("crm.create", "require-approval", "deny"))
        self.assertNotEqual(changed["replayedPolicyVersion"], same["replayedPolicyVersion"])

    def test_policy_replay_uses_the_recorded_public_access_level(self):
        public = [e for e in self.events if e.get("initiatingUser") == "unmapped:slack:abc" and e["event"] == "authorization"]
        self.assertEqual(public[0]["ledger"]["principalAccess"], "public")
        self.assertEqual(audit.replay_policy(public, self.state)["different"], 0)

    def test_dry_run_refuses_live_connectors_and_never_writes_the_ledger(self):
        before = self.ledger.read_bytes()
        with self.assertRaises(audit.LiveConnectorRefused):
            audit.dry_run_replay(self.events, {"crm.create": lambda tool, args: "live"})
        with self.assertRaises(audit.LiveConnectorRefused):
            audit.dry_run_replay(self.events, {}, default=object())
        mock = audit.MockConnector()
        report = audit.dry_run_replay(self.events, {}, default=mock)
        self.assertEqual(report["mode"], "sandbox")
        self.assertEqual(report["replayed"], len([e for e in self.events if e["event"] == "execution"]))
        self.assertTrue(all(step["sandboxResult"]["sandbox"] for step in report["steps"] if step["status"] == "replayed"))
        strict = audit.dry_run_replay(self.events, {}, default=type("Strict", (audit.SandboxConnector,), {"run": lambda s, t, a: {}})())
        self.assertIn("arguments-redacted", {step.get("reason") for step in strict["steps"]})
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_csv_export_neutralises_formulas_and_json_drops_internal_fields(self):
        rows = audit.event_rows([{"event": "execution", "agent": "=cmd|' /C calc'!A0", "tool": "+1"}])
        parsed = list(csv.DictReader(io.StringIO(audit.to_csv(rows))))
        self.assertEqual((parsed[0]["agent"][0], parsed[0]["tool"][0]), ("'", "'"))
        self.assertNotIn("_line", audit.to_json({"events": self.events}))

    def test_verify_reports_tampering(self):
        self.assertTrue(audit.verify(self.ledger)["valid"])
        lines = self.ledger.read_text().splitlines()
        tampered = json.loads(lines[1])
        tampered["outcome"] = "allow-everything"
        lines[1] = json.dumps(tampered)
        self.ledger.write_text("\n".join(lines) + "\n")
        self.assertFalse(audit.verify(self.ledger)["valid"])


class OperatorCliTests(unittest.TestCase):
    def test_audit_subcommand_queries_replays_and_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            state, ledger = build_ledger(root)

            def run(*args, ok=True):
                result = subprocess.run([sys.executable, str(ROOT / "scripts" / "client-operator.py"), "audit", *args, "--ledger", str(ledger)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, ok, result.stderr)
                return result.stdout

            matched = json.loads(run("query", "--approver", "bob@acme.example"))
            self.assertTrue(matched["chain"]["valid"])
            self.assertEqual({e["tool"] for e in matched["events"]}, {"crm.create"})
            timeline = json.loads(run("timeline", "--task", "task-1"))
            self.assertEqual(timeline["agents"], ["receptionist"])
            replay = json.loads(run("replay-policy", "--desired-state", str(root / "desired-state.json")))
            self.assertEqual(replay["different"], 0)
            self.assertIn("sequence", run("query", "--format", "csv").splitlines()[0])
            self.assertEqual(json.loads(run("replay-dry-run"))["mode"], "sandbox")
            lines = ledger.read_text().splitlines()
            event = json.loads(lines[1])
            event["outcome"] = "tampered"
            lines[1] = json.dumps(event)
            ledger.write_text("\n".join(lines) + "\n")
            run("query", ok=False)
            self.assertEqual(json.loads(run("query", "--allow-unverified"))["chain"]["valid"], False)


if __name__ == "__main__":
    unittest.main()

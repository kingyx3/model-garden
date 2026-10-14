from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


handoff = _load("handoff_under_test", "platform/core/handoff.py")
policy = _load("policy_for_handoff", "platform/core/policy.py")
govern = _load("govern_action_for_handoff", "scripts/govern-action.py")
compiler = _load("compile_workspace_for_handoff", "scripts/compile-workspace.py")
runtime_module = _load("governed_mcp_for_handoff", "platform/runtime/governed_mcp.py")
runtime_cli = _load("runtime_handoff_cli", "scripts/runtime-handoff.py")

ARGS = {"start": "2026-10-20T10:00:00+08:00", "end": "2026-10-20T10:30:00+08:00", "summary": "Boiler visit"}


def request(index: int = 1, tool: str = "calendar.book") -> dict:
    return {"agent": "receptionist", "tool": tool, "arguments": ARGS, "requestId": f"{index:064x}"}


class StoreTests(unittest.TestCase):
    def test_create_is_idempotent_and_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "handoffs"
            first = handoff.create(root, request(), capability="calendar", reason="unbound", assignee="jordan@client.example", now=1000)
            again = handoff.create(root, request(), capability="calendar", reason="unbound", assignee="someone-else", now=2000)
            self.assertEqual(first, again)
            self.assertEqual((first["status"], first["channel"], first["deadline"]), ("pending", "inbox", 1000 + 24 * 3600))
            self.assertEqual(first["request"]["arguments"], ARGS)
            path = root / "pending" / f"{request()['requestId']}.json"
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(root / "pending").st_mode), 0o700)
            self.assertEqual(len(handoff.pending(root)), 1)

    def test_completion_records_the_person_and_is_single_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            handoff.create(root, request(), capability="calendar", reason="unbound", assignee="jordan", deadline_hours=1, now=1000)
            document = handoff.complete(root, request()["requestId"], "done", " jordan@client.example ", note="booked in the paper diary", now=1000 + 7200)
            self.assertEqual((document["status"], document["completedBy"], document["late"]), ("done", "jordan@client.example", True))
            self.assertEqual(document["note"], "booked in the paper diary")
            self.assertEqual(handoff.pending(root), [])
            with self.assertRaisesRegex(handoff.HandoffError, "no pending hand-off"):
                handoff.complete(root, request()["requestId"], "done", "jordan")
            self.assertEqual(handoff.find(root, request()["requestId"])["status"], "done")

    def test_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            handoff.create(root, request(), capability=None, reason="human-only", assignee="jordan")
            for bad in ("done ", "finished"):
                with self.assertRaises(handoff.HandoffError):
                    handoff.complete(root, request()["requestId"], bad, "jordan")
            with self.assertRaises(handoff.HandoffError):
                handoff.complete(root, request()["requestId"], "done", "  ")
            with self.assertRaises(handoff.HandoffError):
                handoff.complete(root, "../../etc/passwd", "done", "jordan")
            with self.assertRaises(handoff.HandoffError):
                handoff.create(root, request(2), capability=None, reason="because", assignee="jordan")
            with self.assertRaises(handoff.HandoffError):
                handoff.create(root, request(2), capability=None, reason="unbound", assignee="")
            with self.assertRaises(handoff.HandoffError):
                handoff.create(root, request(2), capability=None, reason="unbound", assignee="jordan", deadline_hours=0)
            self.assertEqual(len(handoff.pending(root)), 1)

    def test_an_identical_action_after_closure_gets_a_fresh_pending_hand_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            handoff.create(root, request(), capability="calendar", reason="unbound", assignee="jordan", now=1)
            handoff.complete(root, request()["requestId"], "declined", "jordan", now=2)
            fresh = handoff.create(root, request(), capability="calendar", reason="unbound", assignee="jordan", now=3)
            self.assertEqual(fresh["status"], "pending")
            self.assertEqual(handoff.find(root, request()["requestId"])["status"], "pending")
            self.assertEqual(len(handoff.closed(root)), 1)

    def test_description_never_overclaims(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            document = handoff.create(root, request(), capability="calendar", reason="unbound", assignee="jordan", deadline_hours=1, now=0)
            pending = handoff.describe(document, now=10)
            self.assertIn("NOT been done", pending["message"])
            self.assertIsNone(pending["performedBy"])
            overdue = handoff.describe(document, now=99999)
            self.assertIn("time has passed", overdue["message"])
            done = handoff.describe(handoff.complete(root, request()["requestId"], "done", "jordan@client.example"))
            self.assertEqual(done["performedBy"], "jordan@client.example")
            self.assertIn("I did not do it myself", done["message"])
            handoff.create(root, request(2), capability="calendar", reason="unbound", assignee="jordan")
            declined = handoff.describe(handoff.complete(root, request(2)["requestId"], "declined", "jordan"))
            self.assertIn("NOT done", declined["message"])
            self.assertIsNone(declined["performedBy"])


def human_only(**overrides) -> dict:
    return {"id": "bookings-by-hand", "effect": "human-only", "actions": ["calendar.book"], "assignee": "front-desk@client.example", **overrides}


def state_with(*rules: dict) -> dict:
    return {
        "schemaVersion": 1,
        "source": {"name": "receptionist"},
        "agent": {"spec": {"approvals": {"externalActions": "none"}, "security": {"dataClassification": "confidential"}}},
        "tools": [
            {"metadata": {"name": "calendar.book"}, "spec": {"type": "internal", "risk": "write", "approvalRequired": True}},
            {"metadata": {"name": "calendar.availability"}, "spec": {"type": "internal", "risk": "read"}},
        ],
        **({"policies": [{"apiVersion": "modelgarden.ai/v1", "kind": "Policy", "metadata": {"name": "p", "owner": "o"}, "spec": {"rules": list(rules)}}]} if rules else {}),
    }


class PolicyEffectTests(unittest.TestCase):
    def evaluate(self, *rules, **kwargs):
        return policy.evaluate(state_with(*rules)["policies"], tool_name="calendar.book", risk="write", **kwargs)

    def test_human_only_hands_off_with_the_named_assignee(self):
        result = self.evaluate(human_only(deadlineHours=4))
        self.assertEqual((result["decision"], result["reason"], result["ruleId"]), ("hand-off", "policy-human-only", "bookings-by-hand"))
        self.assertEqual((result["assignee"], result["deadlineHours"]), ("front-desk@client.example", 4))

    def test_precedence_deny_beats_human_only_which_beats_approval_and_signed_allow(self):
        deny = {"id": "no", "effect": "deny", "actions": ["calendar.book"]}
        self.assertEqual(self.evaluate(human_only(), deny)["decision"], "deny")
        approve = {"id": "ask", "effect": "require-approval", "actions": ["calendar.book"]}
        allow = {"id": "ok", "effect": "allow", "actions": ["calendar.book"]}
        signed = {**allow, "signoff": {"owner": "owner", "ruleDigest": policy.rule_digest(allow)}}
        self.assertEqual(self.evaluate(approve, human_only(), signed, agent_requires_approval=True)["decision"], "hand-off")

    def test_public_principal_cannot_trigger_a_hand_off_of_a_write(self):
        result = self.evaluate(human_only(), principal="public")
        self.assertEqual((result["decision"], result["reason"]), ("deny", "principal-not-authorized"))

    def test_policy_checks_and_preview(self):
        tools = state_with()["tools"]
        self.assertEqual(policy.relaxation_errors([state_with(human_only())["policies"][0]], tools), [])
        problems = policy.relaxation_errors([state_with(human_only(assignee=" "))["policies"][0]], tools)
        self.assertTrue(any("must name the person" in problem for problem in problems))
        shown = policy.preview(state_with(human_only())["policies"], tool_name="calendar.book", risk="write", tool_requires_approval=True)
        self.assertEqual(shown, {"member": "human", "public": "denied"})
        before = state_with(human_only())["policies"]
        self.assertEqual(policy.diff_policies(before, [])["relaxed"], ["bookings-by-hand"])

    def test_compiled_workspace_rejects_a_human_only_rule_without_an_assignee(self):
        import shutil

        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "workspace"
            shutil.copytree(ROOT / "examples" / "workspace", workspace)
            path = workspace / "policies" / "default.yaml"
            document = yaml.safe_load(path.read_text())
            document["spec"]["rules"].append({"id": "by-hand", "effect": "human-only", "actions": ["calendar.book"]})
            path.write_text(yaml.safe_dump(document))
            with self.assertRaisesRegex(ValueError, "must name the person"):
                compiler.compile_workspace(workspace, "receptionist")
            document["spec"]["rules"][-1]["assignee"] = "front-desk@client.example"
            path.write_text(yaml.safe_dump(document))
            compiled = compiler.compile_workspace(workspace, "receptionist")[0]
            outcome = govern.authorize_action(compiled, "calendar.book", {"start": "x", "end": "y"})
            self.assertEqual((outcome["decision"], outcome["assignee"]), ("hand-off", "front-desk@client.example"))


class BoundaryTests(unittest.TestCase):
    def events(self, path: pathlib.Path) -> list[dict]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_human_only_action_is_prepared_not_executed_and_ledgered_at_tier_4(self):
        calls: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            audit = root / "audit.jsonl"
            result = govern.execute_action(
                state_with(human_only()), "calendar.book", ARGS, calls.append,
                audit_path=audit, handoff_store=root / "handoffs", initiating_user="call:trace-1",
            )
            self.assertEqual(calls, [])
            self.assertEqual(result["status"], "handed-off")
            self.assertEqual(result["handoff"]["assignee"], "front-desk@client.example")
            self.assertIn("NOT been done", result["handoff"]["message"])
            created = [event for event in self.events(audit) if event["event"] == "handoff"]
            self.assertEqual(len(created), 1)
            self.assertEqual(created[0]["outcome"], "created")
            self.assertEqual((created[0]["ledger"]["tier"], created[0]["ledger"]["provider"]), (4, "human"))
            self.assertEqual(created[0]["ledger"]["policyRuleId"], "bookings-by-hand")
            self.assertTrue(govern.verify_audit_chain(audit)["valid"])
            # The same exact request while pending is not ledgered twice.
            govern.execute_action(
                state_with(human_only()), "calendar.book", ARGS, calls.append,
                audit_path=audit, handoff_store=root / "handoffs", initiating_user="call:trace-1",
            )
            self.assertEqual(len([event for event in self.events(audit) if event["event"] == "handoff"]), 1)

    def test_no_store_or_no_assignee_fails_closed(self):
        calls: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            audit = root / "audit.jsonl"
            with self.assertRaisesRegex(PermissionError, "no hand-off inbox"):
                govern.execute_action(state_with(human_only()), "calendar.book", ARGS, calls.append, audit_path=audit)
            by_default = {"id": "h", "effect": "human-only", "actions": ["calendar.book"]}
            with self.assertRaisesRegex(PermissionError, "no named person"):
                govern.execute_action(state_with(by_default), "calendar.book", ARGS, calls.append, audit_path=audit, handoff_store=root / "h")
            result = govern.execute_action(
                state_with(by_default), "calendar.book", ARGS, calls.append,
                audit_path=audit, handoff_store=root / "h", handoff_assignee="owner@client.example",
            )
            self.assertEqual(result["handoff"]["assignee"], "owner@client.example")
        self.assertEqual(calls, [])

    def test_completion_is_ledgered_with_the_person_and_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            audit = root / "audit.jsonl"
            result = govern.execute_action(state_with(human_only()), "calendar.book", ARGS, lambda a: a, audit_path=audit, handoff_store=root / "handoffs")
            handoff_id = result["handoff"]["handoffId"]
            document = govern.complete_handoff(root / "handoffs", handoff_id, "done", "front-desk@client.example", audit_path=audit)
            self.assertEqual(document["status"], "done")
            last = self.events(audit)[-1]
            self.assertEqual((last["event"], last["outcome"]), ("handoff", "done"))
            self.assertEqual(last["details"]["completedBy"], "front-desk@client.example")
            self.assertEqual((last["ledger"]["tier"], last["ledger"]["provider"], last["ledger"]["capability"]), (4, "human", "calendar"))
            self.assertEqual(last["requestId"], handoff_id)
            self.assertTrue(govern.verify_audit_chain(audit)["valid"])
            with self.assertRaises(govern.HANDOFF.HandoffError):  # governance loads its own copy of the module
                govern.complete_handoff(root / "handoffs", handoff_id, "done", "someone-else", audit_path=audit)

    def test_create_handoff_respects_selection_denials_and_the_public_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            audit = root / "audit.jsonl"
            kwargs = {"reason": "unbound", "assignee": "front-desk@client.example", "audit_path": audit}
            with self.assertRaisesRegex(PermissionError, "tool-not-selected"):
                govern.create_handoff(state_with(), "crm.update", {}, root / "h", **kwargs)
            with self.assertRaisesRegex(PermissionError, "principal-not-authorized"):
                govern.create_handoff(state_with(), "calendar.book", ARGS, root / "h", principal_access="public", **kwargs)
            deny = {"id": "no", "effect": "deny", "actions": ["calendar.book"]}
            with self.assertRaisesRegex(PermissionError, "policy-deny"):
                govern.create_handoff(state_with(deny), "calendar.book", ARGS, root / "h", **kwargs)
            self.assertEqual(handoff.pending(root / "h"), [])
            ok = govern.create_handoff(state_with(), "calendar.book", ARGS, root / "h", **kwargs)
            self.assertEqual(ok["status"], "handed-off")


class RuntimeTests(unittest.TestCase):
    def runtime(self, root: pathlib.Path, *, bound: bool, assignee: str | None = "front-desk@client.example"):
        desired = compiler.compile_workspace(ROOT / "examples" / "workspace", "receptionist")[0]
        desired["source"]["client"] = "acme"
        desired_path = root / "desired.json"
        desired_path.write_text(json.dumps(desired), encoding="utf-8")
        environ = {"MODEL_GARDEN_GOOGLE_CALENDAR_TOKEN": "secret", "MODEL_GARDEN_INITIATING_IDENTITY": "call:trace-9"}
        if assignee:
            environ["MODEL_GARDEN_HANDOFF_ASSIGNEE"] = assignee
        if not bound:
            manifest = runtime_module.CAPABILITIES.build_manifest(desired, {"connectors": {}})
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            environ["MODEL_GARDEN_CAPABILITY_MANIFEST"] = str(root / "manifest.json")
        return runtime_module.GovernedToolRuntime(desired_path, root / "audit.jsonl", root / "approvals", environ=environ)

    def test_unbound_capability_completes_via_hand_off_and_is_reported_truthfully(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            runtime = self.runtime(root, bound=False)
            result = runtime.request_handoff(tool="calendar.book", arguments=ARGS, justification="calendar not connected")
            self.assertEqual(result["status"], "handed-off")
            handoff_id = result["handoff"]["handoffId"]
            self.assertIn("NOT been done", runtime.handoff_status(handoff_id)["message"])
            govern.complete_handoff(root / "handoffs", handoff_id, "done", "front-desk@client.example", audit_path=root / "audit.jsonl")
            final = runtime.handoff_status(handoff_id)
            self.assertEqual((final["status"], final["performedBy"]), ("done", "front-desk@client.example"))
            self.assertIn("I did not do it myself", final["message"])
            events = [json.loads(line) for line in (root / "audit.jsonl").read_text().splitlines()]
            tiers = [event["ledger"].get("tier") for event in events if event["event"] == "handoff"]
            self.assertEqual(tiers, [4, 4])
            self.assertEqual(events[0]["ledger"]["justification"], "calendar not connected")
            self.assertTrue(govern.verify_audit_chain(root / "audit.jsonl")["valid"])

    def test_revoked_connection_can_be_handed_off_but_direct_calls_still_deny(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            runtime = self.runtime(root, bound=True)
            runtime_module.CAPABILITIES.record_connection_health(root, "calendar", "revoked", actor="owner", reason="offboarding")
            with self.assertRaisesRegex(PermissionError, "revoked"):
                runtime.calendar_book(start=ARGS["start"], end=ARGS["end"])
            result = runtime.request_handoff(tool="calendar.book", arguments=ARGS)
            self.assertEqual(result["status"], "handed-off")

    def test_available_capability_must_be_called_directly(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.runtime(pathlib.Path(tmp), bound=True)
            with self.assertRaisesRegex(ValueError, "available here"):
                runtime.request_handoff(tool="calendar.book", arguments=ARGS)

    def test_reads_unselected_tools_and_incomplete_arguments_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.runtime(pathlib.Path(tmp), bound=False)
            with self.assertRaisesRegex(ValueError, "only actions"):
                runtime.request_handoff(tool="calendar.availability", arguments={"start": "a", "end": "b"})
            with self.assertRaisesRegex(PermissionError, "not selected"):
                runtime.request_handoff(tool="mail.send", arguments={"draftId": "d"})
            with self.assertRaisesRegex(ValueError, "missing required argument"):
                runtime.request_handoff(tool="calendar.book", arguments={"start": "a"})

    def test_no_named_person_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.runtime(pathlib.Path(tmp), bound=False, assignee=None)
            with self.assertRaisesRegex(PermissionError, "no named person"):
                runtime.request_handoff(tool="calendar.book", arguments=ARGS)

    def test_unknown_handoff_id_and_malformed_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.runtime(pathlib.Path(tmp), bound=False)
            with self.assertRaisesRegex(ValueError, "no such hand-off"):
                runtime.handoff_status("0" * 64)
            with self.assertRaises(ValueError):
                runtime.handoff_status("../x")


class OperatorScriptTests(unittest.TestCase):
    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = runtime_cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_list_done_and_decline_write_the_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            store = root / ".modelgarden" / "handoffs"
            audit = root / ".modelgarden" / "audit.jsonl"
            first = govern.create_handoff(state_with(), "calendar.book", ARGS, store, reason="unbound", assignee="jordan", audit_path=audit)
            other = govern.create_handoff(state_with(), "calendar.book", {**ARGS, "start": "2026-10-21T10:00:00+08:00"}, store, reason="unbound", assignee="jordan", audit_path=audit)
            code, out, _ = self.run_cli(str(store), "list")
            listing = json.loads(out)
            self.assertEqual((code, len(listing)), (0, 2))
            self.assertEqual(listing[0]["arguments"]["summary"], "Boiler visit")
            code, out, _ = self.run_cli(str(store), "done", first["handoff"]["handoffId"], "--by", "jordan@client.example")
            self.assertEqual((code, json.loads(out)["status"], json.loads(out)["performedBy"]), (0, "done", "jordan@client.example"))
            code, out, _ = self.run_cli(str(store), "decline", other["handoff"]["handoffId"], "--by", "jordan@client.example", "--note", "no")
            self.assertEqual((code, json.loads(out)["status"]), (0, "declined"))
            outcomes = [json.loads(line)["outcome"] for line in audit.read_text().splitlines() if '"event":"handoff"' in line]
            self.assertEqual(outcomes, ["created", "created", "done", "declined"])
            code, _, err = self.run_cli(str(store), "done", first["handoff"]["handoffId"], "--by", "jordan")
            self.assertEqual(code, 1)
            self.assertIn("no pending hand-off", err)


class OperatorCommandTests(unittest.TestCase):
    def test_client_operator_exposes_handoff_commands_with_validated_input(self):
        operator = _load("client_operator_for_handoff", "scripts/client-operator.py")
        with self.assertRaises(ValueError):
            operator.runtime_handoff("acme-prod", "close")
        with self.assertRaises(ValueError):
            operator.runtime_handoff("Bad Name", "list")
        for bad_id in ("not-a-hash", "../x"):
            with self.assertRaisesRegex(ValueError, "64-character"):
                operator.runtime_handoff("acme-prod", "done", handoff_id=bad_id, by="jordan")
        with self.assertRaisesRegex(ValueError, "--by"):
            operator.runtime_handoff("acme-prod", "done", handoff_id="a" * 64, by=" ")


if __name__ == "__main__":
    unittest.main()

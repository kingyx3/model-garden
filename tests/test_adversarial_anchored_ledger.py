"""Independent exported-ledger correlation checks; synthetic fixtures, no provider."""
from __future__ import annotations

import copy
import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = ROOT / "scripts" / "verify-adversarial-ledger.py"
spec = importlib.util.spec_from_file_location("adversarial_anchored_ledger", SPEC)
checker = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(checker)


def fixture():
    suite = {"schemaVersion": 1, "suite": "synthetic", "modelCases": [
        {"id": name, "turns": ["hostile"], "expected": {
            "forbiddenTools": ["crm.create"], "forbiddenOutput": [],
            "allowedDispositions": ["refuse"]}}
        for name in ("hostile-1", "hostile-2")
    ]}
    trace = {"schemaVersion": 1, "source": "recorded-model-run", "cases": [
        {"id": name, "disposition": "refuse", "assistantMessages": ["I cannot do that."],
         "toolCalls": [{"requestId": f"req-{i}", "tool": "crm.create", "outcome": "deny"}],
         "ledgerEvents": [{"requestId": f"req-{i}", "tool": "crm.create", "outcome": "deny"}]}
        for i, name in enumerate(("hostile-1", "hostile-2"), 1)
    ]}
    bindings = {"schemaVersion": 1, "tenantId": "tenant-dev", "environment": "dev",
                "agent": "receptionist", "cases": [
                    {"id": "hostile-1", "correlationId": "run-a"},
                    {"id": "hostile-2", "correlationId": "run-b"}]}
    records = [{"event": {
        "event": "authorization", "outcome": "deny", "requestId": f"req-{i}",
        "tool": "crm.create", "agent": "receptionist",
        "governance": {"tenantId": "tenant-dev", "environment": "dev", "correlationId": corr}
    }} for i, corr in enumerate(("run-a", "run-b"), 1)]
    return suite, trace, bindings, records


class AnchoredLedgerTests(unittest.TestCase):
    def check(self, suite, trace, bindings, records, *, report=None):
        return checker.evaluate_anchored(
            suite, trace, bindings, records, {"ok": True} if report is None else report
        )

    def test_matching_denials_with_independent_anchor_pass_structural_join(self):
        self.assertEqual(self.check(*fixture()), [])

    def test_forged_trace_denial_without_exported_receipt_fails(self):
        args = list(fixture())
        args[3].pop()
        self.assertIn("hostile-2:missing-anchored-receipt", self.check(*args))

    def test_exported_execution_cannot_be_hidden_by_trace_denial(self):
        args = list(fixture())
        args[3][0]["event"].update(event="execution", outcome="succeeded")
        self.assertIn("hostile-1:anchored-mismatch", self.check(*args))

    def test_unreported_exported_effect_is_rejected(self):
        args = list(fixture())
        extra = copy.deepcopy(args[3][0])
        extra["event"].update(requestId="unreported", event="execution", outcome="succeeded")
        args[3].append(extra)
        self.assertIn("hostile-1:unreported-anchored-effect", self.check(*args))

    def test_cross_tenant_and_agent_spoofing_fails_closed(self):
        for field, value in (("tenantId", "other"), ("environment", "prod")):
            args = list(fixture())
            args[3][0]["event"]["governance"][field] = value
            with self.subTest(field=field):
                self.assertIn("hostile-1:wrong-anchored-scope", self.check(*args))
        args = list(fixture())
        args[3][0]["event"]["agent"] = "another-agent"
        self.assertIn("hostile-1:wrong-anchored-scope", self.check(*args))

    def test_duplicate_case_binding_and_reused_correlation_rejected(self):
        args = list(fixture())
        args[2]["cases"][1]["correlationId"] = "run-a"
        self.assertIn("bindings:duplicate-correlation", self.check(*args))
        args = list(fixture())
        args[2]["cases"][1]["id"] = "hostile-1"
        self.assertIn("bindings:invalid-cases", self.check(*args))

    def test_untrusted_anchor_never_passes(self):
        self.assertEqual(self.check(*fixture(), report={"ok": False}), ["export:invalid-chain"])

    def test_approval_pending_and_failed_execution_are_not_silent_success(self):
        for kind, outcome, mapped in (
            ("approval", "pending", "pending-approval"),
            ("execution", "failed", "failed"),
        ):
            args = list(fixture())
            args[3][0]["event"].update(event=kind, outcome=outcome)
            self.assertIn("hostile-1:anchored-mismatch", self.check(*args))
            args[1]["cases"][0]["toolCalls"][0]["outcome"] = mapped
            args[1]["cases"][0]["ledgerEvents"][0]["outcome"] = mapped
            self.assertIn("hostile-1:forbidden-tool-effect", self.check(*args))

    def test_unresolved_started_execution_and_conflicting_terminals_fail(self):
        args = list(fixture())
        args[3][0]["event"].update(event="execution", outcome="started")
        self.assertIn("hostile-1:unresolved-anchored-request", self.check(*args))
        args = list(fixture())
        extra = copy.deepcopy(args[3][0])
        extra["event"].update(event="execution", outcome="succeeded")
        args[3].append(extra)
        self.assertIn("hostile-1:conflicting-anchored-outcomes", self.check(*args))

    def test_unrelated_events_are_ignored_but_request_replay_is_not(self):
        args = list(fixture())
        extra = copy.deepcopy(args[3][0])
        extra["event"]["requestId"] = "unrelated"
        extra["event"]["governance"]["correlationId"] = "other-run"
        args[3].append(extra)
        self.assertEqual(self.check(*args), [])
        extra["event"]["requestId"] = "req-1"
        self.assertIn("hostile-1:request-outside-bound-run", self.check(*args))

    def test_missing_context_and_unsupported_outcome_fail(self):
        args = list(fixture())
        args[3][0]["event"].pop("governance")
        self.assertIn("hostile-1:request-outside-bound-run", self.check(*args))
        args = list(fixture())
        args[3][0]["event"]["outcome"] = "unknown"
        self.assertIn("hostile-1:unknown-anchored-outcome", self.check(*args))

    def test_nonterminal_tool_identity_cannot_be_swapped_before_execution(self):
        args = list(fixture())
        extra = copy.deepcopy(args[3][0])
        extra["event"].update(event="execution", outcome="succeeded", tool="crm.read")
        # Same request ID but a different Tool must fail even when the trace
        # claims a safe denial and the authorization receipt remains unchanged.
        args[3].append(extra)
        self.assertIn("hostile-1:conflicting-anchored-tools", self.check(*args))

    def test_actual_export_verifier_and_head_witness_are_required(self):
        spec = importlib.util.spec_from_file_location(
            "adversarial_export_fixture", ROOT / "platform" / "core" / "ledger_export.py"
        )
        export = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(export)
        suite, trace, bindings, records = fixture()
        event = {"schemaVersion": 3, "sequence": 1, "previousEventHash": None,
                 **records[0]["event"]}
        event["eventHash"] = export.event_hash(event)
        with tempfile.TemporaryDirectory() as tmp:
            sink = export.DirectorySink(pathlib.Path(tmp))
            sink.put_records([export.make_record(event)])
            sink.put_head(export.make_head([event], exported_at=1700000000))
            verified = export.verify_remote_export(
                sink, expected_sequence=1, expected_hash=event["eventHash"]
            )
            self.assertTrue(verified["ok"], verified["problems"])
            self.assertIn("hostile-2:missing-anchored-receipt",
                          self.check(suite, trace, bindings, sink.records(), report=verified))
            invalid = export.verify_remote_export(
                sink, expected_sequence=1, expected_hash="0" * 64
            )
            self.assertEqual(self.check(suite, trace, bindings, sink.records(), report=invalid),
                             ["export:invalid-chain"])


if __name__ == "__main__":
    unittest.main()

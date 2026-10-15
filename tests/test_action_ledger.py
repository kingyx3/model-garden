from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    module_spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(module_spec)
    assert module_spec.loader is not None
    module_spec.loader.exec_module(module)
    return module


govern = _load("govern_action_for_ledger", "scripts/govern-action.py")
lago = _load("lago_for_ledger", "platform/metering/lago.py")
compiler = _load("compile_workspace_for_ledger", "scripts/compile-workspace.py")
runtime_module = _load("governed_mcp_for_ledger", "platform/runtime/governed_mcp.py")

ARGS = {
    "start": "2026-09-16T10:00:00+08:00",
    "end": "2026-09-16T10:30:00+08:00",
    "summary": "Boiler repair for Alice Example",
    "attendees": ["alice@example.invalid"],
}


def desired_state(*, risk: str = "write", approval_required: bool = False) -> dict:
    return {
        "schemaVersion": 1,
        "source": {"name": "receptionist"},
        "agent": {"spec": {"approvals": {"externalActions": "none"}}},
        "tools": [
            {"metadata": {"name": "calendar.book"}, "spec": {"type": "internal", "risk": "write", "approvalRequired": approval_required}},
            {"metadata": {"name": "calendar.availability"}, "spec": {"type": "internal", "risk": "read"}},
            {"metadata": {"name": "kb.search"}, "spec": {"type": "internal", "risk": "read"}},
        ],
        "knowledge": [
            {"metadata": {"name": "business-information"}, "spec": {"type": "repository", "classification": "internal"}},
            {"metadata": {"name": "hr-records"}, "spec": {"type": "database", "classification": "restricted"}},
        ],
    }


def events(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class LedgerSchemaV3Tests(unittest.TestCase):
    CONTEXT = {
        "tenantId": "acme",
        "originatingPrincipal": "alice@acme.example",
        "channel": "slack",
        "triggerRef": "slack:message/T1-C1-1700000000.0001",
        "taskId": "task-42",
        "parentRequestId": "a" * 64,
        "delegationChain": ["alice@acme.example", "agent:coordinator", "agent:receptionist"],
        "correlationId": "trace-42",
    }
    LEDGER = {
        "workspaceRevision": "git:abc123",
        "justification": "  Caller asked   for the first free slot on Tuesday.  ",
        "provider": "google",
        "tier": 1,
    }

    def _execute(self, audit: pathlib.Path, **kwargs):
        return govern.execute_action(
            desired_state(),
            "calendar.book",
            ARGS,
            lambda _: {"eventId": "evt-9"},
            initiating_user="alice@acme.example",
            governance_context=self.CONTEXT,
            audit_path=audit,
            ledger=self.LEDGER,
            external_object_ref=lambda result: f"google-calendar:event/{result['eventId']}",
            **kwargs,
        )

    def test_v3_records_carry_the_full_bounded_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "audit.jsonl"
            self._execute(audit)
            recorded = events(audit)
        self.assertEqual([event["schemaVersion"] for event in recorded], [3, 3])
        execution = recorded[-1]
        self.assertEqual(execution["governance"]["delegationChain"], self.CONTEXT["delegationChain"])
        self.assertEqual(execution["governance"]["originatingPrincipal"], "alice@acme.example")
        self.assertEqual(execution["governance"]["taskId"], "task-42")
        ledger = execution["ledger"]
        self.assertEqual(ledger["policyRuleId"], "selected-tool")
        self.assertEqual(ledger["policyVersion"], govern.POLICY_VERSION)
        self.assertEqual(ledger["capability"], "calendar")
        self.assertEqual(ledger["provider"], "google")
        self.assertEqual(ledger["tier"], 1)
        self.assertEqual(ledger["workspaceRevision"], "git:abc123")
        self.assertEqual(ledger["justification"], "Caller asked for the first free slot on Tuesday.")
        self.assertEqual(ledger["externalObjectRef"], "google-calendar:event/evt-9")
        self.assertNotIn("externalObjectRef", recorded[0]["ledger"])

    def test_arguments_are_redacted_with_a_stable_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "audit.jsonl"
            self._execute(audit)
            text = audit.read_text(encoding="utf-8")
            recorded = events(audit)
        self.assertEqual(
            recorded[0]["arguments"],
            {"attendees": "[redacted]", "end": ARGS["end"], "start": ARGS["start"], "summary": "[redacted]"},
        )
        self.assertEqual(recorded[0]["argumentsDigest"], govern._sha256_json(ARGS))
        self.assertNotIn("Alice Example", text)
        self.assertNotIn("alice@example.invalid", text)

    def test_unknown_tools_redact_every_argument(self):
        self.assertEqual(govern.redact_arguments("custom.lookup", {"query": "x", "limit": 3}), {"limit": "[redacted]", "query": "[redacted]"})

    def test_justification_is_capped_and_not_bound_into_request_id(self):
        long = govern.ledger_context({"justification": "x" * 5000})
        self.assertEqual(len(long["justification"]), govern.MAX_JUSTIFICATION)
        with tempfile.TemporaryDirectory() as tmp:
            first = self._execute(pathlib.Path(tmp) / "a.jsonl")
            self.LEDGER = {**self.LEDGER, "justification": "Different wording"}
            second = self._execute(pathlib.Path(tmp) / "b.jsonl")
        self.assertEqual(first["request"]["requestId"], second["request"]["requestId"])

    def test_delegation_chain_binds_request_id(self):
        first = govern.build_action_request(desired_state(), "calendar.book", ARGS, governance_context=self.CONTEXT)
        changed = govern.build_action_request(
            desired_state(), "calendar.book", ARGS, governance_context={**self.CONTEXT, "delegationChain": ["mallory@evil.example"]}
        )
        self.assertNotEqual(first["requestId"], changed["requestId"])

    def test_every_new_field_is_bounded_and_arbitrary_fields_are_rejected(self):
        for key in ("originatingPrincipal", "channel", "triggerRef", "taskId", "parentRequestId"):
            with self.assertRaises(ValueError, msg=key):
                govern.build_action_request(desired_state(), "calendar.book", {}, governance_context={key: "x" * 1000})
            with self.assertRaises(ValueError, msg=key):
                govern.build_action_request(desired_state(), "calendar.book", {}, governance_context={key: ""})
        for chain in ([], ["x"] * 17, [""], ["x" * 1000], "alice"):
            with self.assertRaises(ValueError, msg=repr(chain)):
                govern.build_action_request(desired_state(), "calendar.book", {}, governance_context={"delegationChain": chain})
        for key in ("agentVersion", "workspaceRevision", "platformRelease", "policyRuleId", "policyVersion", "capability", "provider", "externalObjectRef"):
            with self.assertRaises(ValueError, msg=key):
                govern.ledger_context({key: "x" * 1000})
            with self.assertRaises(ValueError, msg=key):
                govern.ledger_context({key: 7})
        for tier in (5, -1, "1", True):
            with self.assertRaises(ValueError, msg=repr(tier)):
                govern.ledger_context({"tier": tier})
        for smuggled in ("prompt", "transcript", "chainOfThought", "rawCredential"):
            with self.assertRaises(ValueError, msg=smuggled):
                govern.ledger_context({smuggled: "anything"})
            with self.assertRaises(ValueError, msg=smuggled):
                govern.build_action_request(desired_state(), "calendar.book", {}, governance_context={smuggled: "anything"})

    def test_mixed_v1_v2_v3_chain_verifies_without_rewriting_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "audit.jsonl"
            legacy = {"schemaVersion": 1, "event": "execution", "outcome": "succeeded"}
            audit.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
            v2 = {"schemaVersion": 2, "event": "authorization", "outcome": "allow", "requestId": "b" * 64}
            govern.append_audit_event(audit, v2)
            self._execute(audit)
            before = audit.read_text(encoding="utf-8").splitlines()[:2]
            result = govern.verify_audit_chain(audit)
            after = audit.read_text(encoding="utf-8").splitlines()[:2]
        self.assertTrue(result["valid"])
        self.assertEqual(result["events"], 4)
        self.assertEqual(before, after)

    def test_chain_rejects_schema_version_moving_backwards(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "audit.jsonl"
            self._execute(audit)
            govern.append_audit_event(audit, {"schemaVersion": 2, "event": "execution", "outcome": "succeeded"})
            with self.assertRaisesRegex(ValueError, "moved backwards"):
                govern.verify_audit_chain(audit)

    def test_usage_attribution_still_joins_on_request_and_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "audit.jsonl"
            result = self._execute(audit)
            recorded = events(audit)
        properties = lago.build_attribution_properties(
            client="acme",
            agent="receptionist",
            environment="prod",
            provider="openai",
            trace_id=recorded[-1]["governance"]["correlationId"],
            action_request_id=recorded[-1]["requestId"],
        )
        self.assertEqual(properties["action_request_id"], result["request"]["requestId"])
        self.assertEqual(properties["trace_id"], "trace-42")

    def test_public_principal_is_denied_writes_and_ledgered(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "audit.jsonl"
            with self.assertRaisesRegex(PermissionError, "mapped, verified principal"):
                self._execute(audit, principal_access="public")
            recorded = events(audit)
        self.assertEqual(recorded[0]["outcome"], "deny")
        self.assertEqual(recorded[0]["ledger"]["policyRuleId"], "principal-not-authorized")


class LedgerReadEventTests(unittest.TestCase):
    CONTEXT = {"tenantId": "acme", "taskId": "task-7", "correlationId": "trace-7"}

    def test_read_tool_emits_digest_only_read_event_sharing_task_and_trace(self):
        result_payload = {"available": False, "busy": [{"start": "2026-09-16T02:00:00Z", "end": "2026-09-16T02:30:00Z", "summary": "Private dentist visit"}]}
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "audit.jsonl"
            govern.execute_action(
                desired_state(),
                "calendar.availability",
                {"start": "2026-09-16T10:00:00+08:00", "end": "2026-09-16T10:30:00+08:00"},
                lambda _: result_payload,
                governance_context=self.CONTEXT,
                audit_path=audit,
                read_receipt=lambda result: [{"sourceId": "calendar:google", "content": result}],
            )
            govern.execute_action(
                desired_state(), "calendar.book", ARGS, lambda _: {"eventId": "e"}, governance_context=self.CONTEXT, audit_path=audit
            )
            text = audit.read_text(encoding="utf-8")
            recorded = events(audit)
            self.assertEqual(govern.verify_audit_chain(audit)["events"], 5)
        self.assertEqual([event["event"] for event in recorded], ["authorization", "execution", "read", "authorization", "execution"])
        read = recorded[2]
        self.assertEqual(read["details"]["reads"][0]["sourceId"], "calendar:google")
        self.assertEqual(read["details"]["reads"][0]["contentDigest"], govern._sha256_json(result_payload))
        self.assertNotIn("Private dentist visit", text)
        self.assertEqual(read["governance"]["taskId"], recorded[-1]["governance"]["taskId"])
        self.assertEqual(read["governance"]["correlationId"], recorded[-1]["governance"]["correlationId"])

    def test_write_tools_emit_no_read_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "audit.jsonl"
            govern.execute_action(desired_state(), "calendar.book", ARGS, lambda _: {"eventId": "e"}, audit_path=audit)
            self.assertNotIn('"event":"read"', audit.read_text(encoding="utf-8"))

    def _kb_read(self, audit: pathlib.Path, source: str, documents: list[dict], policy=None):
        request = govern.build_action_request(desired_state(), "kb.search", {"query": "opening hours"}, governance_context=self.CONTEXT)
        return govern.record_knowledge_read(desired_state(), request, source, documents, audit_path=audit, policy=policy)

    def test_knowledge_reads_record_ids_and_stable_digests_not_content(self):
        documents = [{"id": "doc-hours", "content": "We open at 9am."}, {"id": "doc-area", "content": {"area": "North"}}]
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "audit.jsonl"
            first = self._kb_read(audit, "business-information", documents)
            second = self._kb_read(audit, "business-information", [dict(d) for d in documents])
            text = audit.read_text(encoding="utf-8")
        read = first["details"]["reads"][0]
        self.assertEqual(read["sourceId"], "knowledge:business-information")
        self.assertEqual(read["classification"], "internal")
        self.assertEqual([r["id"] for r in read["records"]], ["doc-hours", "doc-area"])
        self.assertEqual(read["records"], second["details"]["reads"][0]["records"])
        self.assertEqual(read["records"][0]["contentDigest"], govern._sha256_json("We open at 9am."))
        self.assertNotIn("We open at 9am.", text)
        self.assertNotIn("North", text)

    def test_unselected_knowledge_source_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PermissionError):
                self._kb_read(pathlib.Path(tmp) / "audit.jsonl", "payroll", [])

    def test_aggregate_mode_drops_record_ids_but_keeps_count_and_digest(self):
        documents = [{"id": f"doc-{n}", "content": n} for n in range(5)]
        with tempfile.TemporaryDirectory() as tmp:
            event = self._kb_read(pathlib.Path(tmp) / "audit.jsonl", "business-information", documents, govern.parse_read_policy("aggregate"))
        read = event["details"]["reads"][0]
        self.assertNotIn("records", read)
        self.assertEqual(read["recordCount"], 5)
        self.assertEqual(len(read["recordsDigest"]), 64)

    def test_restricted_sources_are_recorded_even_when_aggregated_or_sampled_out(self):
        documents = [{"id": f"emp-{n}", "content": n} for n in range(3)]
        never = {"mode": "sampled", "sampleRate": 10**6, "maxRecordIds": 1}
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "audit.jsonl"
            internal = self._kb_read(audit, "business-information", documents, never)
            restricted = self._kb_read(audit, "hr-records", documents, never)
            aggregated = self._kb_read(audit, "hr-records", documents, govern.parse_read_policy("aggregate"))
        if internal is not None:
            # requestId happened to sample in; the restricted guarantee below is what matters.
            self.assertEqual(internal["details"]["readPolicy"], "sampled")
        self.assertIsNotNone(restricted)
        self.assertEqual([r["id"] for r in restricted["details"]["reads"][0]["records"]], ["emp-0", "emp-1", "emp-2"])
        self.assertEqual(len(aggregated["details"]["reads"][0]["records"]), 3)

    def test_record_ids_are_capped_in_full_mode(self):
        documents = [{"id": f"doc-{n}", "content": n} for n in range(5)]
        with tempfile.TemporaryDirectory() as tmp:
            event = self._kb_read(pathlib.Path(tmp) / "audit.jsonl", "business-information", documents, {"mode": "full", "maxRecordIds": 2})
        read = event["details"]["reads"][0]
        self.assertEqual(len(read["records"]), 2)
        self.assertTrue(read["recordsTruncated"])
        self.assertEqual(read["recordCount"], 5)

    def test_read_policy_parsing(self):
        self.assertEqual(govern.parse_read_policy(None)["mode"], "full")
        self.assertEqual(govern.parse_read_policy("sampled:10")["sampleRate"], 10)
        for bad in ("sampled", "sampled:0", "everything"):
            with self.assertRaises(ValueError, msg=bad):
                govern.parse_read_policy(bad)

    def test_runtime_calendar_read_is_ledgered_with_provenance(self):
        class Transport:
            def __call__(self, method, url, headers, payload):
                return 200, {"calendars": {"primary": {"busy": []}}}

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            desired = compiler.compile_workspace(ROOT / "examples" / "workspace", "receptionist")[0]
            (root / "desired.json").write_text(json.dumps(desired), encoding="utf-8")
            runtime = runtime_module.GovernedToolRuntime(
                root / "desired.json",
                root / "audit.jsonl",
                root / "approvals",
                environ={"MODEL_GARDEN_GOOGLE_CALENDAR_TOKEN": "t", "MODEL_GARDEN_WORKSPACE_REVISION": "git:feed"},
                calendar_transport=Transport(),
            )
            runtime.calendar_availability(
                start="2026-09-16T10:00:00+08:00", end="2026-09-16T10:30:00+08:00", justification="Caller asked about 10am."
            )
            recorded = events(root / "audit.jsonl")
        # The first successful use also records the connection as healthy, as its own ledger event.
        self.assertEqual([event["event"] for event in recorded], ["authorization", "execution", "read", "operator-action"])
        self.assertEqual((recorded[-1]["action"], recorded[-1]["details"]), ("connection.health", {"family": "calendar", "health": "healthy"}))
        ledger = recorded[2]["ledger"]
        self.assertEqual(ledger["provider"], "google")
        self.assertEqual(ledger["workspaceRevision"], "git:feed")
        self.assertEqual(ledger["platformRelease"], (ROOT / "VERSION").read_text().strip())
        self.assertTrue(ledger["agentVersion"].startswith("sha256:"))
        self.assertEqual(ledger["justification"], "Caller asked about 10am.")
        self.assertEqual(recorded[2]["details"]["reads"][0]["sourceId"], "calendar:google")


if __name__ == "__main__":
    unittest.main()

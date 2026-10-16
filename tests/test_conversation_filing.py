"""Credential-free checks for filing finished calls and verified-sender emails into the bound CRM."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


compiler = _load("compile_workspace_for_filing", "scripts/compile-workspace.py")
runtime_module = _load("governed_mcp_for_filing", "platform/runtime/governed_mcp.py")
filing = _load("conversation_filing_under_test", "platform/channels/conversation_filing.py")
cli = _load("runtime_file_calls_under_test", "scripts/runtime-file-calls.py")
operator = _load("client_operator_for_filing", "scripts/client-operator.py")
policy = runtime_module.POLICY if hasattr(runtime_module, "POLICY") else _load("policy_for_filing", "platform/core/policy.py")

CRM_BINDING = {"environment": "dev", "connectors": {"crm": {"provider": "built-in-crm", "tier": 0, "owner": "owner@client.example"}}}
SECRET_WORDS = "my card is blue and my mother's maiden name is Hargreaves"


def transcript(call_hash="call-1", *, started=1_790_000_000, summary=None, outcome="message-taken") -> dict:
    record = {
        "type": "CallTranscript", "schemaVersion": 1, "timestamp": started, "endedAt": started + 95,
        "callHash": call_hash, "agent": "receptionist", "outcome": outcome, "requestIds": [], "redactions": {},
        "turns": [
            {"index": 0, "speaker": "caller", "offsetSeconds": 0.0, "text": SECRET_WORDS},
            {"index": 1, "speaker": "agent", "offsetSeconds": 2.0, "text": "I have noted that."},
        ],
    }
    if summary:
        record["summary"] = summary
    return record


class FilingCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def workspace(self, *rules: dict) -> pathlib.Path:
        target = self.root / "workspace"
        if not target.exists():
            shutil.copytree(ROOT / "examples" / "workspace", target)
        if rules:
            path = target / "policies" / "default.yaml"
            document = yaml.safe_load(path.read_text())
            # The reference policy gates every `*.create`; stricter wins over any allow rule, so a client who
            # wants automatic contact creation must narrow that gate as well as sign an allow rule.
            for existing in document["spec"]["rules"]:
                if existing["id"] == "approve-external-write":
                    existing["actions"] = [a for a in existing["actions"] if a != "*.create"]
            document["spec"]["rules"].extend(rules)
            path.write_text(yaml.safe_dump(document))
        return target

    def runtime(self, *rules: dict, binding=CRM_BINDING):
        desired = compiler.compile_workspace(self.workspace(*rules), "receptionist")[0]
        desired["source"]["client"] = "acme"
        path = self.root / "desired.json"
        path.write_text(json.dumps(desired), encoding="utf-8")
        environ = {"MODEL_GARDEN_INITIATING_IDENTITY": cli.FILING_IDENTITY}
        if binding is not None:
            manifest = runtime_module.CAPABILITIES.build_manifest(desired, binding)
            (self.root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            environ["MODEL_GARDEN_CAPABILITY_MANIFEST"] = str(self.root / "manifest.json")
        return runtime_module.GovernedToolRuntime(path, self.root / "audit.jsonl", self.root / "approvals", environ=environ)

    def audit(self) -> list[dict]:
        path = self.root / "audit.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def everything_on_disk(self) -> str:
        return "".join(p.read_text() for p in self.root.rglob("*") if p.is_file() and p.suffix in {".json", ".jsonl"}
                       and "workspace" not in p.parts)


class CallInteractionTests(unittest.TestCase):
    def test_a_call_becomes_a_reference_outcome_and_size_never_its_words(self):
        arguments = filing.interaction_from_call(transcript())
        self.assertEqual((arguments["interactionType"], arguments["channel"], arguments["direction"]), ("call", "voice", "inbound"))
        self.assertEqual(arguments["transcriptRef"], "transcript:call-1")
        self.assertEqual(arguments["outcome"], "message-taken")
        self.assertTrue(arguments["timestamp"].endswith("Z"))
        self.assertIn("95 seconds, 2 turns, outcome message-taken", arguments["summary"])
        self.assertNotIn("Hargreaves", json.dumps(arguments))

    def test_an_extractive_summary_that_quotes_the_call_is_never_filed(self):
        record = transcript(summary='The caller first said: "my boiler is broken at 12 Acacia Road". 1 caller and 1 agent turns. Outcome: resolved.')
        record["summarySource"] = "extractive"
        arguments = filing.interaction_from_call(record)
        self.assertNotIn("Acacia", arguments["summary"])
        self.assertIn("outcome", arguments["summary"])
        record["summarySource"] = "provided"
        self.assertIn("Acacia", filing.interaction_from_call(record)["summary"])  # a model's summary is still filed, as before

    def test_a_model_summary_is_preferred_and_malformed_records_are_skipped(self):
        self.assertEqual(filing.interaction_from_call(transcript(summary="Wants a callback on Tuesday."))["summary"],
                         "Wants a callback on Tuesday.")
        for bad in ({}, {"type": "FallbackMessage"}, {"type": "CallTranscript"},
                    {"type": "CallTranscript", "callHash": "", "timestamp": 1},
                    {"type": "CallTranscript", "callHash": "c", "timestamp": True},
                    {"type": "CallTranscript", "callHash": "c", "timestamp": "soon"}):
            self.assertIsNone(filing.interaction_from_call(bad), bad)

    def test_email_reference_is_a_digest_not_the_message_id(self):
        arguments = filing.interaction_from_email("<abc@client.example>", 1_790_000_000, outcome="resolved")
        self.assertRegex(arguments["transcriptRef"], r"^email:[0-9a-f]{32}$")
        self.assertNotIn("client.example", json.dumps(arguments))
        self.assertEqual(arguments["interactionType"], "email")

    def test_reading_stored_transcripts_skips_other_records_and_garbage_in_time_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "t.jsonl"
            path.write_text("\n".join([
                json.dumps(transcript("b", started=200)), "not json", json.dumps({"type": "FallbackMessage", "message": "x"}),
                json.dumps(transcript("a", started=100)), "",
            ]), encoding="utf-8")
            self.assertEqual([r["callHash"] for r in filing.read_call_transcripts(path)], ["a", "b"])
            self.assertEqual(filing.read_call_transcripts(pathlib.Path(tmp) / "missing.jsonl"), [])


class CallFilingTests(FilingCase):
    def test_calls_go_to_the_unmatched_queue_through_the_ledger_without_any_words_spoken(self):
        runtime = self.runtime()
        totals = filing.file_call_transcripts(runtime, runtime.crm_store(), [transcript("c1"), transcript("c2", started=1_790_001_000)])
        self.assertEqual((totals["filed"], totals["failed"]), (2, 0))
        queue = runtime.crm_store().review_queue()
        self.assertEqual(len(queue["unmatchedInteractions"]), 2)
        self.assertEqual({i["transcriptRef"] for i in queue["unmatchedInteractions"]}, {"transcript:c1", "transcript:c2"})
        executions = [e for e in self.audit() if e["event"] == "execution"]
        self.assertEqual(len(executions), 2)
        self.assertTrue(all(e["initiatingUser"] == cli.FILING_IDENTITY for e in executions))
        self.assertTrue(all(e["ledger"]["externalObjectRef"].startswith("built-in-crm:interaction/") for e in executions))
        self.assertNotIn("Hargreaves", self.everything_on_disk())
        self.assertTrue(runtime_module.GOVERN.verify_audit_chain(self.root / "audit.jsonl")["valid"])

    def test_re_running_files_nothing_new_even_after_a_person_assigned_the_record(self):
        runtime = self.runtime()
        store = runtime.crm_store()
        records = [transcript("c1")]
        self.assertEqual(filing.file_call_transcripts(runtime, store, records)["filed"], 1)
        self.assertEqual(filing.file_call_transcripts(runtime, store, records)["duplicate"], 1)
        contact = store.create_contact({"name": "Alex Tan"})["objectId"]
        interaction = store.review_queue()["unmatchedInteractions"][0]["id"]
        store.assign_interaction(interaction, contact)
        again = filing.file_call_transcripts(runtime, store, records)
        self.assertEqual((again["filed"], again["duplicate"]), (0, 1))
        self.assertEqual(store.review_queue()["interactions"], 1)

    def test_one_failing_record_does_not_stop_the_rest_and_only_the_error_type_is_reported(self):
        runtime = self.runtime()
        store = runtime.crm_store()
        real = runtime.crm_log_interaction
        seen: list[str] = []

        def flaky(**kwargs):
            if kwargs["transcript_ref"] == "transcript:bad":
                raise RuntimeError("secret provider detail")
            return real(**kwargs)

        runtime.crm_log_interaction = flaky
        totals = filing.file_call_transcripts(runtime, store, [transcript("bad"), transcript("good")], log=seen.append)
        self.assertEqual((totals["filed"], totals["failed"]), (1, 1))
        self.assertEqual(seen, ["transcript:bad: RuntimeError"])

    def test_an_unbound_crm_refuses_instead_of_filing_somewhere_else(self):
        runtime = self.runtime(binding={"environment": "dev", "connectors": {}})
        with self.assertRaises(PermissionError):
            runtime.crm_store()


class EmailFilingTests(FilingCase):
    ARGS = filing.interaction_from_email("<m1@client.example>", 1_790_000_000, outcome="resolved")

    def file(self, runtime, *, verified=None, allow_create=False, arguments=None):
        return filing.file_interaction(runtime, runtime.crm_store(), arguments or self.ARGS,
                                       verified_email=verified, allow_create=allow_create)

    def test_a_verified_sender_with_exactly_one_matching_contact_gets_the_interaction(self):
        runtime = self.runtime()
        contact = runtime.crm_store().create_contact({"name": "Alex", "email": "Alex@Client.example"})["objectId"]
        result = self.file(runtime, verified="alex@client.example")
        self.assertEqual((result["status"], result["basis"]), ("filed", "verified-email"))
        self.assertEqual(runtime.crm_store().review_queue()["unmatchedInteractions"], [])
        read = runtime.crm_store().read(object_id=contact, principal_access="member")
        self.assertEqual(len(read["records"][0]["interactions"]), 1)

    def test_no_match_an_ambiguous_match_or_an_unverified_sender_use_the_unmatched_queue(self):
        runtime = self.runtime()
        store = runtime.crm_store()
        store.create_contact({"email": "twin@client.example"})
        store.create_contact({"email": "twin@client.example"})
        store.create_contact({"email": "known@client.example"})
        cases = {
            "nobody": ("nobody@client.example", "email:" + "1" * 32),
            "ambiguous": ("twin@client.example", "email:" + "2" * 32),
            "unverified sender whose address matches a contact": (None, "email:" + "3" * 32),
        }
        for label, (verified, reference) in cases.items():
            result = self.file(runtime, verified=verified, arguments={**self.ARGS, "transcriptRef": reference})
            self.assertEqual((result["status"], result["basis"]), ("filed", "unmatched"), label)
        self.assertEqual(len(store.review_queue()["unmatchedInteractions"]), 3)

    def test_a_contact_is_created_only_when_enabled_and_crm_create_actually_runs(self):
        default = self.runtime()
        result = self.file(default, verified="new@client.example", allow_create=True)
        # crm.create needs approval by default: nothing is created, the email still lands in the queue.
        self.assertEqual(result["basis"], "unmatched")
        self.assertEqual(default.crm_store().review_queue()["contacts"], 0)
        self.assertTrue(any(e["event"] == "approval" and e["outcome"] == "pending" for e in self.audit()))

    def test_with_a_signed_allow_rule_the_verified_sender_gets_a_contact_and_the_interaction(self):
        rule = {"id": "auto-create", "effect": "allow", "actions": ["crm.create"]}
        signed = {**rule, "signoff": {"owner": "owner@client.example", "ruleDigest": policy.rule_digest(rule)}}
        runtime = self.runtime(signed)
        result = self.file(runtime, verified="new@client.example", allow_create=True)
        self.assertEqual((result["status"], result["basis"]), ("filed", "created-from-verified-email"))
        self.assertEqual(runtime.crm_store().find_by_email("new@client.example") and 1, 1)
        again = self.file(runtime, verified="new@client.example", allow_create=True)
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(runtime.crm_store().review_queue()["contacts"], 1)

    def test_creation_stays_off_unless_enabled(self):
        rule = {"id": "auto-create", "effect": "allow", "actions": ["crm.create"]}
        signed = {**rule, "signoff": {"owner": "owner@client.example", "ruleDigest": policy.rule_digest(rule)}}
        runtime = self.runtime(signed)
        self.assertEqual(self.file(runtime, verified="new@client.example")["basis"], "unmatched")
        self.assertEqual(runtime.crm_store().review_queue()["contacts"], 0)

    def test_the_matching_helpers_are_not_a_lookup_oracle_for_junk_input(self):
        store = self.runtime().crm_store()
        for junk in ("", "not-an-address", None, 5, "a@b"):
            self.assertEqual(store.find_by_email(junk), [])
        self.assertFalse(store.has_reference("email:none"))


class CommandTests(FilingCase):
    def run_cli(self, argv, *, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        env = {"MODEL_GARDEN_CAPABILITY_MANIFEST": str(self.root / "manifest.json")}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if stdin is not None:
                with mock.patch.object(sys, "stdin", io.StringIO(stdin)):
                    code = cli.main(argv)
            else:
                code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def common(self):
        self.runtime()
        return ["--desired-state", str(self.root / "desired.json"), "--audit-path", str(self.root / "audit.jsonl"),
                "--approval-root", str(self.root / "approvals")]

    def test_the_command_files_from_a_path_or_standard_input_and_reruns_safely(self):
        common = self.common()
        path = self.root / "transcripts.jsonl"
        path.write_text(json.dumps(transcript("c1")) + "\n" + json.dumps({"type": "FallbackMessage"}) + "\n", encoding="utf-8")
        code, out, _ = self.run_cli(["--transcripts", str(path), *common])
        self.assertEqual((code, json.loads(out)["filed"], json.loads(out)["records"]), (0, 1, 1))
        code, out, _ = self.run_cli(["--transcripts", "-", *common], stdin=path.read_text())
        self.assertEqual((code, json.loads(out)["duplicate"], json.loads(out)["filed"]), (0, 1, 0))

    def test_no_transcripts_is_a_clean_no_op(self):
        code, out, _ = self.run_cli(["--transcripts", str(self.root / "none.jsonl"), *self.common()])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["records"], 0)

    def test_an_unbound_crm_fails_closed_with_a_clear_message(self):
        common = self.common()
        (self.root / "manifest.json").write_text(json.dumps(
            runtime_module.CAPABILITIES.build_manifest(json.loads((self.root / "desired.json").read_text()),
                                                       {"environment": "dev", "connectors": {}})))
        code, _, err = self.run_cli(["--transcripts", "-", *common], stdin="")
        self.assertEqual(code, 1)
        self.assertIn("not connected", err)

    def test_the_operator_command_pipes_the_voice_transcripts_into_the_governed_container(self):
        calls: list[tuple] = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs.get("input_text")))
            if command[:2] == ["docker", "ps"]:
                service = next(part for part in command if part.startswith("label=com.docker.compose.service="))
                return mock.Mock(returncode=0, stdout=("voice1\n" if service.endswith("voice") else "gov1\n"), stderr="")
            if "cat" in " ".join(command):
                return mock.Mock(returncode=0, stdout='{"type":"CallTranscript"}\n', stderr="")
            return mock.Mock(returncode=0, stdout='{"filed": 1}\n', stderr="")

        with mock.patch.object(operator, "_run", fake_run), mock.patch.object(operator, "_require_command"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(operator.runtime_file_calls("acme-dev"), 0)
        read = next(c for c in calls if c[0][:3] == ["docker", "exec", "voice1"])
        write = next(c for c in calls if c[0][:4] == ["docker", "exec", "-i", "gov1"])
        self.assertIn("/opt/data/transcripts.jsonl", " ".join(read[0]))
        self.assertEqual(write[1], '{"type":"CallTranscript"}\n')
        self.assertTrue(write[0][-2:] == ["--transcripts", "-"])
        with self.assertRaises(ValueError):
            operator.runtime_file_calls("Bad Name")


if __name__ == "__main__":
    unittest.main()

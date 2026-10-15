from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
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


compiler = _load("compile_workspace_for_crm_runtime", "scripts/compile-workspace.py")
runtime_module = _load("governed_mcp_for_crm_runtime", "platform/runtime/governed_mcp.py")
deployer = _load("deploy_client_docker_for_crm", "scripts/deploy-client-docker.py")
provisioner = _load("provision_hermes_for_crm", "scripts/provision-hermes-profile.py")
approval_module = _load("runtime_approval_for_crm", "scripts/runtime-approval.py")
channel = _load("text_channel_for_crm", "platform/channels/text_channel.py")
operator = _load("client_operator_for_crm", "scripts/client-operator.py")

CRM_BINDING = {"environment": "dev", "connectors": {"crm": {"provider": "built-in-crm", "tier": 0, "owner": "owner@client.example"}}}
CHANNEL_BINDING = {
    "apiVersion": "modelgarden.ai/v1",
    "kind": "EnvironmentBinding",
    "spec": {"environment": "prod", "channels": {"directory": {"slack": {"T01/U-ALICE": {"identity": "alice@acme.example"}}}}},
}
LOG_ARGS = {
    "object_id": "",
    "interaction_type": "call",
    "channel": "voice",
    "direction": "inbound",
    "timestamp": "2026-10-20T10:00:00+08:00",
    "summary": "Asked about boiler servicing; promised a callback on Tuesday.",
    "transcript_ref": "transcript:abc123",
}


def workspace(tmp: str) -> pathlib.Path:
    target = pathlib.Path(tmp) / "workspace"
    shutil.copytree(ROOT / "examples" / "workspace", target)
    # The example receptionist already selects the log-conversation Skill and the create and
    # log-interaction Tools; a read Tool is added for the staff-lookup tests.
    agent = target / "agents" / "front-office" / "receptionist" / "agent.yaml"
    agent.write_text(agent.read_text().replace("    - crm.create\n", "    - crm.read\n    - crm.create\n", 1))
    return target


def principal_for(user: str):
    message = channel.InboundMessage(
        channel="slack", conversation_id="C1", thread_id="1", message_id="2",
        sender=channel.ChannelIdentity("slack", user, workspace_id="T01"), text="look up Alex",
    )
    return channel.PrincipalDirectory.from_binding(CHANNEL_BINDING).principal_for(message)


class RuntimeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def runtime(self, binding=CRM_BINDING, principal=None):
        desired = compiler.compile_workspace(workspace(str(self.root)), "receptionist")[0]
        desired["source"]["client"] = "acme"
        path = self.root / "desired.json"
        path.write_text(json.dumps(desired), encoding="utf-8")
        environ = {"MODEL_GARDEN_INITIATING_IDENTITY": "call:trace-7"}
        if binding is not None:
            manifest = runtime_module.CAPABILITIES.build_manifest(desired, binding)
            (self.root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            environ["MODEL_GARDEN_CAPABILITY_MANIFEST"] = str(self.root / "manifest.json")
        return runtime_module.GovernedToolRuntime(
            path, self.root / "audit.jsonl", self.root / "approvals", environ=environ,
            principal_resolver=(lambda: principal) if principal is not None else None,
        )

    def store(self):
        return runtime_module.CONTACTS.ContactStore(self.root / "contacts")

    def audit(self) -> list[dict]:
        return [json.loads(line) for line in (self.root / "audit.jsonl").read_text().splitlines()]

    def approve(self, pending: dict) -> None:
        approval_module.decide(self.root / "approvals", pending["request"]["requestId"], approved=True, approver="owner@client.example")


class FilingTests(RuntimeCase):
    def test_log_interaction_runs_without_approval_and_is_ledgered_at_tier_zero_without_personal_data(self):
        runtime = self.runtime()
        contact = self.store().create_contact({"name": "Alex Tan", "phone": "+65 6123 4567"})["objectId"]
        result = runtime.crm_log_interaction(**{**LOG_ARGS, "object_id": contact}, justification="call finished")
        self.assertEqual(result["status"], "executed")
        self.assertFalse(result["result"]["duplicate"])
        execution = next(event for event in self.audit() if event["event"] == "execution")
        ledger = execution["ledger"]
        self.assertEqual((ledger["provider"], ledger["tier"], ledger["capability"]), ("built-in-crm", 0, "crm"))
        self.assertEqual(ledger["externalObjectRef"], result["result"]["externalObjectRef"])
        self.assertTrue(ledger["externalObjectRef"].startswith("built-in-crm:interaction/i-"))
        self.assertEqual(execution["arguments"]["interactionType"], "call")
        self.assertEqual(execution["arguments"]["summary"], runtime_module.GOVERN.REDACTED)
        text = (self.root / "audit.jsonl").read_text()
        for private in ("Alex", "6123", "callback", "boiler"):
            self.assertNotIn(private, text)
        self.assertTrue(runtime_module.GOVERN.verify_audit_chain(self.root / "audit.jsonl")["valid"])

    def test_retrying_the_same_filing_returns_the_same_record_and_writes_nothing_new(self):
        runtime = self.runtime()
        contact = self.store().create_contact({"name": "Alex Tan"})["objectId"]
        first = runtime.crm_log_interaction(**{**LOG_ARGS, "object_id": contact})["result"]
        again = runtime.crm_log_interaction(**{**LOG_ARGS, "object_id": contact})["result"]
        self.assertEqual((again["interactionId"], again["duplicate"]), (first["interactionId"], True))
        self.assertEqual(len(json.loads((self.root / "contacts" / "contacts.json").read_text())["interactions"]), 1)

    def test_unmatched_filing_and_unknown_contacts(self):
        runtime = self.runtime()
        unmatched = runtime.crm_log_interaction(**{**LOG_ARGS, "object_id": "unmatched"})["result"]
        self.assertFalse(unmatched["matched"])
        with self.assertRaisesRegex(ValueError, "unknown contact"):
            runtime.crm_log_interaction(**{**LOG_ARGS, "object_id": "c-0000000000000000"})
        failed = [event for event in self.audit() if event["event"] == "execution" and event["outcome"] == "failed"]
        self.assertEqual(len(failed), 1)

    def test_create_waits_for_exact_approval_then_never_reveals_duplicates(self):
        runtime = self.runtime()
        fields = {"name": "Alex Tan", "phone": "+65 6123 4567"}
        pending = runtime.crm_create(fields=fields)
        self.assertEqual(pending["status"], "pending-approval")
        self.assertEqual(self.store().review_queue()["contacts"], 0)
        self.approve(pending)
        created = runtime.crm_create(fields=fields)
        self.assertEqual(created["status"], "executed")
        self.assertTrue(created["result"]["externalObjectRef"].startswith("built-in-crm:contact/c-"))
        # The same asserted phone again: a new record, no sign that one already exists.
        other = {"name": "Someone Else", "phone": "6561234567"}
        self.approve(runtime.crm_create(fields=other))
        second = runtime.crm_create(fields=other)["result"]
        self.assertEqual(set(second), {"objectId", "created", "externalObjectRef"})
        self.assertEqual(len(self.store().review_queue()["possibleDuplicates"]), 1)

    def test_only_the_contact_object_type_is_supported(self):
        runtime = self.runtime()
        with self.assertRaisesRegex(ValueError, "contact"):
            runtime.crm_create(fields={"name": "A"}, object_type="account")
        with self.assertRaisesRegex(ValueError, "contact"):
            runtime.crm_read(query="A", object_type="deal")
        with self.assertRaisesRegex(ValueError, "contact"):
            runtime.crm_log_interaction(**{**LOG_ARGS, "object_id": "unmatched"}, object_type="deal")


class ReadTests(RuntimeCase):
    def seed(self) -> str:
        store = self.store()
        contact = store.create_contact({"name": "Alex Tan", "phone": "+65 6123 4567"})["objectId"]
        store.log_interaction({**{k: v for k, v in LOG_ARGS.items() if k != "transcript_ref"}, "objectId": contact,
                               "interactionType": LOG_ARGS["interaction_type"], "transcriptRef": "transcript:abc123"})
        return contact

    def test_a_phone_caller_or_unmapped_sender_never_gets_records_back(self):
        for principal in (None, principal_for("U-STRANGER")):
            self.setUp()  # a fresh workspace and store per principal
            runtime = self.runtime(principal=principal)
            contact = self.seed()
            result = runtime.crm_read(object_id=contact)
            self.assertEqual(result["status"], "executed")
            self.assertEqual(result["result"], {"records": [], "reason": "requires-verified-staff-principal"})
            self.assertEqual(runtime.crm_read(query="Alex")["result"]["records"], [])

    def test_a_verified_staff_member_can_read_and_the_read_is_ledgered_by_digest(self):
        runtime = self.runtime(principal=principal_for("U-ALICE"))
        contact = self.seed()
        result = runtime.crm_read(query="+65 6123 4567", justification="staff lookup")
        record = result["result"]["records"][0]
        self.assertEqual((record["objectId"], record["fields"]["name"]), (contact, "Alex Tan"))
        self.assertEqual(len(record["interactions"]), 1)
        read = next(event for event in self.audit() if event["event"] == "read")
        entry = read["details"]["reads"][0]
        self.assertEqual((entry["sourceId"], entry["classification"]), ("crm:built-in-crm", "confidential"))
        self.assertEqual(entry["records"][0]["id"], contact)
        self.assertEqual(len(entry["records"][0]["contentDigest"]), 64)
        text = (self.root / "audit.jsonl").read_text()
        for private in ("Alex", "6123", "callback"):
            self.assertNotIn(private, text)

    def test_limit_is_validated(self):
        runtime = self.runtime(principal=principal_for("U-ALICE"))
        for bad in (0, 11, True, "5"):
            with self.assertRaises(ValueError, msg=str(bad)):
                runtime.crm_read(query="Alex", limit=bad)


class GateTests(RuntimeCase):
    def test_nothing_runs_unless_crm_is_bound_to_the_built_in_provider(self):
        for binding in (
            None,
            {"environment": "dev", "connectors": {}},
            {"environment": "dev", "connectors": {"crm": {"provider": "hubspot", "tier": 1}}},
            {"environment": "dev", "connectors": {"crm": {"provider": "built-in-crm", "tier": 1}}},
            {"environment": "dev", "connectors": {"kb": {"provider": "built-in-crm", "tier": 0}}},
        ):
            self.setUp()
            runtime = self.runtime(binding)
            for call in (
                lambda: runtime.crm_create(fields={"name": "A"}),
                lambda: runtime.crm_log_interaction(**{**LOG_ARGS, "object_id": "unmatched"}),
                lambda: runtime.crm_read(query="A"),
            ):
                with self.assertRaisesRegex(PermissionError, "not connected"):
                    call()
            self.assertFalse((self.root / "audit.jsonl").exists())
            self.assertFalse((self.root / "contacts").exists())

    def test_tools_the_agent_did_not_select_are_denied(self):
        runtime = self.runtime()
        runtime.desired_state["tools"] = [t for t in runtime.desired_state["tools"] if t["metadata"]["name"] != "crm.log-interaction"]
        with self.assertRaises(PermissionError):
            runtime.crm_log_interaction(**{**LOG_ARGS, "object_id": "unmatched"})


class ProvisionDeployAndReadinessTests(unittest.TestCase):
    def test_hermes_config_exposes_the_selected_crm_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            desired = compiler.compile_workspace(workspace(tmp), "receptionist")[0]
            files = provisioner.build_profile_files(desired)
            include = yaml.safe_load(files[pathlib.Path("config.yaml")])["mcp_servers"]["model_garden"]["tools"]["include"]
        for name in ("crm_create", "crm_log_interaction", "crm_read"):
            self.assertIn(name, include)

    def test_crm_update_has_no_runtime_so_selecting_it_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            desired = compiler.compile_workspace(workspace(tmp), "receptionist")[0]
            desired["tools"].append({"metadata": {"name": "crm.update"}, "spec": {"type": "internal", "risk": "write"}})
            with self.assertRaisesRegex(ValueError, "crm.update"):
                provisioner.build_profile_files(desired)

    def profile(self, root: pathlib.Path, tools: tuple[str, ...]) -> pathlib.Path:
        profile = root / "profile"
        (profile / ".modelgarden").mkdir(parents=True)
        (profile / "skills" / "model-garden" / "answer-faq").mkdir(parents=True)
        (profile / "SOUL.md").write_text("# Receptionist\n", encoding="utf-8")
        (profile / "config.yaml").write_text(yaml.safe_dump({"model": {"provider": "openai", "default": "m"}}), encoding="utf-8")
        (profile / ".modelgarden" / "desired-state.json").write_text(json.dumps({
            "schemaVersion": 1,
            "tools": [{"metadata": {"name": name}, "spec": {"type": "internal", "risk": "read"}} for name in tools],
        }), encoding="utf-8")
        (profile / "skills" / "model-garden" / "answer-faq" / "SKILL.md").write_text("---\nname: answer-faq\n---\nx\n", encoding="utf-8")
        return profile

    def test_a_crm_only_deployment_gets_a_credential_free_sidecar_with_a_private_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            binding = root / "binding.json"
            binding.write_text(json.dumps({
                "apiVersion": "modelgarden.ai/v1", "kind": "EnvironmentBinding",
                "spec": {"environment": "dev", "runtime": {"profile": "receptionist", "model_credential_ref": "secret://model/dev"},
                         "connectors": CRM_BINDING["connectors"]},
            }), encoding="utf-8")
            bundle = root / "bundle"
            metadata = deployer.render_bundle(self.profile(root, ("crm.log-interaction", "crm.create")), binding, bundle, "acme-dev")
            compose = yaml.safe_load((bundle / "compose.yaml").read_text(encoding="utf-8"))
            tools = compose["services"]["governed-tools"]
            self.assertTrue(metadata["governedToolService"])
            self.assertEqual(tools["volumes"], ["governance-data:/opt/data"])
            self.assertNotIn("governance-data", json.dumps(compose["services"]["hermes"]["volumes"]))
            for forbidden in ("CALENDAR", "OPENAI_API_KEY", "secret://"):
                self.assertNotIn(forbidden, json.dumps(tools["environment"]))
            for relative in ("platform/core/contacts.py", "scripts/runtime-contacts.py"):
                self.assertTrue((bundle / "model-garden-platform" / relative).is_file(), relative)
            manifest = json.loads((bundle / "profile" / ".modelgarden" / "capability-manifest.json").read_text())
            crm = next(item for item in manifest["capabilities"] if item["family"] == "crm")
            self.assertEqual((crm["status"], crm["provider"], crm["tier"]), ("available", "built-in-crm", 0))

    def test_the_log_conversation_skill_is_ready_with_the_tier_zero_log_and_blocked_without_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = workspace(tmp)
            calendar = {"provider": "google", "tier": 1, "token_ref": "secret://calendar/dev"}
            ready = {"environment": "dev", "connectors": {
                "calendar": calendar, "kb": {"provider": "text-knowledge", "tier": 0}, **CRM_BINDING["connectors"],
            }}
            report = compiler.workspace_readiness(ws, "receptionist", (), ready)
            skill = next(item for item in report[0]["skills"] if item["name"] == "log-conversation")
            self.assertTrue(skill["ready"])
            self.assertEqual((skill["dependencies"][0]["provider"], skill["dependencies"][0]["tier"]), ("built-in-crm", 0))
            self.assertIn("crm: built-in-crm tier 0", compiler.format_readiness(report))
            compiler.compile_workspace(ws, "receptionist", environment_binding=ready)
            blocked = {"environment": "dev", "connectors": {"calendar": calendar}}
            self.assertFalse(next(item for item in compiler.workspace_readiness(ws, "receptionist", (), blocked)[0]["skills"]
                                  if item["name"] == "log-conversation")["ready"])
            with self.assertRaisesRegex(ValueError, "log-conversation"):
                compiler.compile_workspace(ws, "receptionist", environment_binding=blocked)

    def test_default_policy_gates_create_but_not_the_internal_interaction_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            desired = compiler.compile_workspace(workspace(tmp), "receptionist")[0]
        govern = runtime_module.GOVERN
        create = govern.authorize_action(desired, "crm.create", {"objectType": "contact", "fields": {"name": "A"}})
        log = govern.authorize_action(desired, "crm.log-interaction", {"objectId": "unmatched"})
        self.assertEqual(create["decision"], "require-approval")
        self.assertEqual(log["decision"], "allow")
        # A public principal can never create records or file interactions by itself.
        for tool in ("crm.create", "crm.log-interaction"):
            denied = govern.authorize_action(desired, tool, {"objectId": "unmatched"}, principal_access="public")
            self.assertEqual((denied["decision"], denied["reason"]), ("deny", "principal-not-authorized"))


class OperatorCommandTests(unittest.TestCase):
    def test_contacts_command_validates_input_before_touching_docker(self):
        for call, message in (
            (lambda: operator.runtime_contacts("acme-prod", "purge"), "must be review"),
            (lambda: operator.runtime_contacts("acme-prod", "erase-contact", ("c-0123456789abcdef",), by=" "), "--by"),
            (lambda: operator.runtime_contacts("acme-prod", "erase-contact", ("../etc",), by="jordan"), "identifiers"),
            (lambda: operator.runtime_contacts("acme-prod", "erase-contact", (), by="jordan"), "needs 1"),
            (lambda: operator.runtime_contacts("acme-prod", "assign", ("i-" + "0" * 24,), by="jordan"), "needs 2"),
            (lambda: operator.runtime_contacts("acme-prod", "review", ("c-0123456789abcdef",)), "needs 0"),
        ):
            with self.assertRaisesRegex(ValueError, message):
                call()


if __name__ == "__main__":
    unittest.main()

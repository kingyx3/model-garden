"""Credential-free checks that offboarding revokes provider grants, one coworker or the whole environment."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
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


cli = _load("runtime_connection_for_grant_offboarding", "scripts/runtime-connection.py")
govern = _load("govern_for_grant_offboarding", "scripts/govern-action.py")
capabilities = _load("capabilities_for_grant_offboarding", "platform/core/capabilities.py")
compiler = _load("compiler_for_grant_offboarding", "scripts/compile-workspace.py")
runtime_module = _load("runtime_for_grant_offboarding", "platform/runtime/governed_mcp.py")
grants_module = _load("provider_grants_for_grant_offboarding", "platform/core/provider_grants.py")
operator = _load("operator_for_grant_offboarding", "scripts/client-operator.py")
NOW = 1_800_000_000
OWNER = "owner@client.example"
BINDING = {"environment": "dev", "connectors": {
    "calendar": {"provider": "google", "tier": 1, "token_ref": "secret://google/dev", "owner": OWNER},
    "kb": {"provider": "text-knowledge", "tier": 0},
    "crm": {"provider": "built-in-crm", "tier": 0},
}}
BOTH = ["calendar.availability", "calendar.book"]


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.store = self.root / "governance"
        self.store.mkdir()
        # Future provider is simulated in the manifest; never treated as built in production.
        ws = self.root / "workspace"
        shutil.copytree(ROOT / "examples/workspace", ws)
        path = ws / "agents/front-office/receptionist/agent.yaml"
        agent = yaml.safe_load(path.read_text())
        agent["spec"]["disabledSkills"] = []
        path.write_text(yaml.safe_dump(agent))
        desired = compiler.compile_workspace(ws, "receptionist")[0]
        desired["source"]["client"] = "acme"
        (self.root / "desired.json").write_text(json.dumps(desired), encoding="utf-8")
        self.manifest_path = self.root / "manifest.json"
        real = capabilities._implemented_connector
        with mock.patch.object(capabilities, "_implemented_connector",
                               side_effect=lambda family, provider, tier:
                               True if (family, provider, tier) == ("calendar", "google", 1)
                               else real(family, provider, tier)):
            manifest = capabilities.build_manifest(desired, BINDING)
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.audit = self.store / "audit.jsonl"
        self.registry = grants_module.GrantRegistry(
            self.store / "provider-grants", tenant="acme-dev",
            audit=lambda action, by, details: govern.record_operator_action(self.audit, action, by, details))
        self.counter = 0

    def grant(self, agent: str, family: str = "calendar", provider: str = "google"):
        self.counter += 1
        return self.registry.create(
            agent=agent, family=family, provider=provider, connection_id=f"conn-{self.counter}", isolation="own-identity",
            acting_identity=f"{agent}-{self.counter}@svc.example", secret_ref=f"secret://{agent}/{self.counter}",
            scopes=BOTH if family == "calendar" else ["crm.read"], owner=OWNER, by="jordan@mg.example", now=NOW)

    def run_cli(self, *argv, manifest=True):
        extra = ["--manifest", str(self.manifest_path)] if manifest else []
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([str(self.store), *argv, *extra])
        return code, out.getvalue(), err.getvalue()

    def events(self, action: str | None = None) -> list[dict]:
        rows = [json.loads(line) for line in self.audit.read_text().splitlines()] if self.audit.exists() else []
        return [row for row in rows if action is None or row.get("action") == action]

    def runtime(self, manifest_path=None):
        return runtime_module.GovernedToolRuntime(
            self.root / "desired.json", self.audit, self.store / "approvals",
            environ={"MODEL_GARDEN_CAPABILITY_MANIFEST": str(manifest_path or self.manifest_path)})


class RegistryTests(Case):
    def test_revoking_one_coworkers_grants_leaves_everyone_else_working_and_is_ledgered_without_references(self):
        first, other = self.grant("receptionist"), self.grant("second-coworker")
        crm = self.grant("receptionist", "crm", "hubspot")
        revoked = self.registry.revoke_all(by="jordan@mg.example", now=NOW + 1, agent="receptionist")
        self.assertEqual({item["id"] for item in revoked}, {first["id"], crm["id"]})
        self.assertEqual({item["secretRef"] for item in revoked}, {"secret://receptionist/1", "secret://receptionist/3"})
        with self.assertRaises(grants_module.GrantError):
            self.registry.resolve(attested_agent="receptionist", family="calendar", tool="calendar.book",
                                  originator_permissions=BOTH, enabled_tools=BOTH, now=NOW + 2)
        access = self.registry.resolve(attested_agent="second-coworker", family="calendar", tool="calendar.book",
                                       originator_permissions=BOTH, enabled_tools=BOTH, now=NOW + 2)
        self.assertEqual(access["grantId"], other["id"])
        events = self.events("grant.revoke")
        self.assertEqual(len(events), 2)
        self.assertTrue(all(event["details"]["offboarded"] and "secretRefDigest" in event["details"] for event in events))
        self.assertNotIn("secret://", self.audit.read_text())
        self.assertTrue(govern.verify_audit_chain(self.audit)["valid"])

    def test_repeating_an_offboarding_changes_and_ledgers_nothing_and_tenant_wide_revokes_everyone(self):
        self.grant("receptionist")
        self.grant("second-coworker")
        self.assertEqual(len(self.registry.revoke_all(by="jordan@mg.example", now=NOW + 1, agent="receptionist")), 1)
        self.assertEqual(self.registry.revoke_all(by="jordan@mg.example", now=NOW + 2, agent="receptionist"), [])
        self.assertEqual(len(self.events("grant.revoke")), 1)
        everyone = self.registry.revoke_all(by="jordan@mg.example", now=NOW + 3)
        self.assertEqual([item["agent"] for item in everyone], ["second-coworker"])
        self.assertTrue(all(item.get("revokedAt") for item in self.registry.list()))

    def test_an_offboarding_must_name_who_did_it_and_a_valid_agent(self):
        self.grant("receptionist")
        for kwargs in ({"by": " "}, {"by": "jordan@mg.example", "agent": "bad agent!"}):
            with self.assertRaises(grants_module.GrantError):
                self.registry.revoke_all(now=NOW + 1, **{"by": "x", **kwargs})
        self.assertFalse(any(item.get("revokedAt") for item in self.registry.list()))


class CliTests(Case):
    def test_offboarding_one_coworker_revokes_its_grants_only_and_lists_the_secrets_to_delete(self):
        self.grant("receptionist")
        other = self.grant("second-coworker")
        code, out, _ = self.run_cli("offboard", "--agent", "receptionist", "--actor", "jordan@client.example", "--reason", "role ended")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual((report["revoked"], [item["agent"] for item in report["grantsRevoked"]]), ([], ["receptionist"]))
        self.assertEqual(report["secretRefsToDelete"], ["secret://receptionist/1"])
        self.assertIn("secrets", " ".join(report["manualSteps"]))
        self.assertFalse(next(item for item in self.registry.list() if item["id"] == other["id"]).get("revokedAt"))
        self.assertEqual(capabilities.read_connection_health(self.store), {})  # environment connection health is untouched
        (event,) = self.events("coworker.offboard")
        self.assertEqual((event["performedBy"], event["details"]),
                         ("jordan@client.example", {"families": [], "grantsRevoked": 1, "agent": "receptionist", "reason": "role ended"}))
        self.assertNotIn("secret://", self.audit.read_text())

    def test_the_manifest_then_shows_the_offboarded_coworkers_skill_blocked_and_the_other_still_ready(self):
        self.grant("receptionist")
        self.grant("second-coworker")
        before = self.runtime().capabilities()
        self.assertTrue(next(s for s in before["skills"] if s["name"] == "book-appointment")["ready"])
        self.run_cli("offboard", "--agent", "receptionist", "--actor", "jordan@client.example")
        after = self.runtime().capabilities()
        booking = next(s for s in after["skills"] if s["name"] == "book-appointment")
        self.assertEqual((booking["ready"], booking["dependencies"][0]["status"]), (False, "grant-unavailable"))
        second = json.loads(self.manifest_path.read_text())
        second["agent"] = "second-coworker"
        path = self.root / "second.json"
        path.write_text(json.dumps(second), encoding="utf-8")
        other = self.runtime(path).capabilities()
        self.assertTrue(next(s for s in other["skills"] if s["name"] == "book-appointment")["ready"])

    def test_offboarding_the_environment_revokes_connections_and_every_grant(self):
        self.grant("receptionist")
        self.grant("second-coworker")
        code, out, _ = self.run_cli("offboard", "--actor", "jordan@client.example")
        report = json.loads(out)
        self.assertEqual((code, report["revoked"], len(report["grantsRevoked"])), (0, ["calendar"], 2))
        self.assertEqual(report["secretRefsToDelete"], ["secret://receptionist/1", "secret://second-coworker/2"])
        self.assertEqual(capabilities.read_connection_health(self.store)["calendar"]["health"], "revoked")
        (event,) = self.events("environment.offboard")
        self.assertEqual(event["details"]["grantsRevoked"], 2)
        self.assertTrue(all(item.get("revokedAt") for item in self.registry.list()))

    def test_without_a_registry_a_coworker_offboarding_is_still_recorded_and_revokes_nothing(self):
        code, out, _ = self.run_cli("offboard", "--agent", "receptionist", "--actor", "jordan@client.example")
        self.assertEqual((code, json.loads(out)["grantsRevoked"]), (0, []))
        (event,) = self.events("coworker.offboard")
        self.assertNotIn("grantsRevoked", event["details"])

    def test_a_coworker_offboarding_needs_to_know_the_environment_and_tenant_can_be_given(self):
        self.grant("receptionist")
        bare = json.loads(self.manifest_path.read_text())
        bare.pop("client")
        self.manifest_path.write_text(json.dumps(bare), encoding="utf-8")
        code, _, err = self.run_cli("offboard", "--agent", "receptionist", "--actor", "jordan@client.example")
        self.assertEqual(code, 1)
        self.assertIn("--tenant", err)
        self.assertEqual(self.events("coworker.offboard"), [])
        code, out, _ = self.run_cli("offboard", "--agent", "receptionist", "--actor", "jordan@client.example", "--tenant", "acme-dev")
        self.assertEqual((code, len(json.loads(out)["grantsRevoked"])), (0, 1))
        # a whole-environment offboarding of a manifest that does not name its client still works (as it did before grants)
        self.assertEqual(self.run_cli("offboard", "--actor", "jordan@client.example")[0], 0)

    def test_listing_never_shows_a_secret_reference_and_one_grant_can_be_revoked(self):
        mine, theirs = self.grant("receptionist"), self.grant("second-coworker")
        code, out, _ = self.run_cli("grants", "list")
        self.assertEqual(code, 0)
        self.assertNotIn("secret://", out)
        self.assertEqual(len(json.loads(out)["grants"]), 2)
        self.assertEqual([item["agent"] for item in json.loads(self.run_cli("grants", "list", "--agent", "second-coworker")[1])["grants"]], ["second-coworker"])
        code, out, _ = self.run_cli("grants", "revoke", mine["id"], "--actor", "jordan@client.example")
        self.assertEqual((code, json.loads(out)["revoked"]["agent"]), (0, "receptionist"))
        self.assertFalse(next(item for item in self.registry.list() if item["id"] == theirs["id"]).get("revokedAt"))
        code, _, err = self.run_cli("grants", "revoke", "g-000000000000", "--actor", "jordan@client.example")
        self.assertEqual(code, 1)
        self.assertIn("Runtime connection failed", err)

    def test_listing_with_no_registry_says_so(self):
        code, out, _ = self.run_cli("grants", "list")
        self.assertEqual((code, json.loads(out)), (0, {"grants": [], "registry": "none"}))


class OperatorTests(Case):
    def fake(self, calls):
        def fake_run(command, **kwargs):
            calls.append(command)
            if command[:2] == ["docker", "ps"]:
                return mock.Mock(returncode=0, stdout="gov1\n", stderr="")
            return mock.Mock(returncode=0, stdout="{}\n", stderr="")
        return fake_run

    def test_the_operator_passes_the_agent_and_validates_before_running_anything(self):
        calls: list = []
        with mock.patch.object(operator, "_run", self.fake(calls)), mock.patch.object(operator, "_require_command"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(operator.runtime_connection("acme-dev", "offboard", actor="jordan@client.example", agent="receptionist"), 0)
            self.assertEqual(calls[-1][-5:], ["offboard", "--actor", "jordan@client.example", "--agent", "receptionist"])
            before = len(calls)
            with self.assertRaises(ValueError):
                operator.runtime_connection("acme-dev", "offboard", actor="jordan@client.example", agent="bad agent; rm -rf")
            self.assertEqual(len(calls), before)

    def test_the_grants_command_is_validated_and_never_asks_for_a_secret(self):
        calls: list = []
        with mock.patch.object(operator, "_run", self.fake(calls)), mock.patch.object(operator, "_require_command"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(operator.runtime_grants("acme-dev", "list", agent="receptionist"), 0)
            self.assertEqual(calls[-1][-4:], ["grants", "list", "--agent", "receptionist"])
            self.assertEqual(operator.runtime_grants("acme-dev", "revoke", grant_id="g-0123456789ab", actor="jordan@client.example"), 0)
            self.assertEqual(calls[-1][-4:], ["revoke", "g-0123456789ab", "--actor", "jordan@client.example"])
            before = len(calls)
            for kwargs in ({"grant_id": "g-1", "actor": "a@b.example"}, {"grant_id": "g-0123456789ab", "actor": " "}, {"grant_id": None, "actor": "a@b.example"}):
                with self.assertRaises(ValueError):
                    operator.runtime_grants("acme-dev", "revoke", **kwargs)
            with self.assertRaises(ValueError):
                operator.runtime_grants("acme-dev", "delete")
            self.assertEqual(len(calls), before)


if __name__ == "__main__":
    unittest.main()

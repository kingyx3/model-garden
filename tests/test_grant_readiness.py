"""Credential-free checks that Skill readiness depends on the coworker's own provider grant."""
from __future__ import annotations

import copy
import importlib.util
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


compiler = _load("compile_for_grant_readiness", "scripts/compile-workspace.py")
capabilities = _load("capabilities_for_grant_readiness", "platform/core/capabilities.py")
readiness = _load("grant_readiness_under_test", "platform/core/grant_readiness.py")
grants_module = _load("provider_grants_for_grant_readiness", "platform/core/provider_grants.py")
runtime_module = _load("governed_mcp_for_grant_readiness", "platform/runtime/governed_mcp.py")
NOW = 1_800_000_000
TENANT = "acme-prod"
BOTH = ["calendar.availability", "calendar.book"]
BINDING = {"environment": "prod", "connectors": {
    "kb": {"provider": "text-knowledge", "tier": 0, "owner": "owner@client.example"},
    "crm": {"provider": "built-in-crm", "tier": 0},
    "calendar": {"provider": "google", "tier": 1, "token_ref": "secret://calendar/prod"},
}}


def test_desired() -> dict:
    """Enable future booking only in this unit-test fixture, never in production."""
    with tempfile.TemporaryDirectory() as tmp:
        ws = pathlib.Path(tmp) / "workspace"
        shutil.copytree(ROOT / "examples/workspace", ws)
        config = ws / "agents/front-office/receptionist/agent.yaml"
        document = yaml.safe_load(config.read_text())
        document["spec"]["disabledSkills"] = []
        config.write_text(yaml.safe_dump(document))
        return compiler.compile_workspace(ws, "receptionist")[0]


def manifest_for(agent: str = "receptionist") -> dict:
    desired = test_desired()
    desired["source"]["client"] = "acme"
    real = capabilities._implemented_connector
    with mock.patch.object(capabilities, "_implemented_connector",
                           side_effect=lambda family, provider, tier:
                           True if (family, provider, tier) == ("calendar", "google", 1)
                           else real(family, provider, tier)):
        result = capabilities.build_manifest(desired, BINDING)
    result["agent"] = agent
    return result


def skill(manifest: dict, name: str) -> dict:
    return next(item for item in manifest["skills"] if item["name"] == name)


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name) / "provider-grants"
        self.registry = grants_module.GrantRegistry(self.root, tenant=TENANT)
        self.manifest = manifest_for()
        self.counter = 0

    def grant(self, agent="receptionist", *, provider="google", scopes=BOTH, family="calendar", **extra):
        self.counter += 1
        return self.registry.create(
            agent=agent, family=family, provider=provider, connection_id=f"conn-{self.counter}", isolation="own-identity",
            acting_identity=f"{agent}-{self.counter}@svc.example", secret_ref=f"secret://{agent}/{self.counter}-token",
            scopes=scopes, owner="owner@client.example", by="jordan@mg.example", now=NOW, **extra)

    def applied(self, manifest=None, *, now=NOW):
        return readiness.overlay_from_root(manifest or self.manifest, self.root, now=now)

    # -- when it applies ---------------------------------------------------------------------

    def test_without_a_registry_nothing_changes_and_nothing_is_created(self):
        result = readiness.overlay_from_root(self.manifest, self.root, now=NOW)
        self.assertEqual(result, self.manifest)
        self.assertFalse(self.root.exists())
        self.assertTrue(skill(result, "book-appointment")["ready"])

    def test_tier_zero_skills_and_optional_unbound_ones_are_untouched_by_a_registry(self):
        self.grant(family="crm", provider="hubspot", scopes=["crm.create"])  # registry exists; no calendar grant
        result = self.applied()
        for name in ("answer-faq", "log-conversation", "take-message", "human-handoff"):
            self.assertEqual(skill(result, name), skill(self.manifest, name), name)

    # -- the verdicts ------------------------------------------------------------------------------

    def test_a_connected_skill_is_blocked_until_this_coworker_holds_its_own_grant(self):
        self.grant("other-coworker")  # someone else's grant never counts
        result = self.applied()
        booking = skill(result, "book-appointment")
        self.assertEqual((booking["ready"], booking["canEnable"], booking["dependencies"][0]["status"]), (False, False, "no-grant"))
        self.assertIn("its own calendar", booking["remediation"])
        self.assertIn("connect calendar", booking["remediation"])
        entry = next(item for item in result["capabilities"] if item["family"] == "calendar")
        self.assertEqual((entry["status"], entry["grant"]), ("unavailable", "unavailable"))
        self.grant()
        self.assertTrue(skill(self.applied(), "book-appointment")["ready"])

    def test_a_usable_grant_that_covers_the_selected_tools_makes_the_skill_ready_with_a_non_secret_note(self):
        self.grant()
        result = self.applied()
        booking = skill(result, "book-appointment")
        self.assertEqual((booking["ready"], booking["canEnable"]), (True, True))
        self.assertEqual(booking["dependencies"][0]["grant"], {"state": "active", "isolation": "own-identity"})
        self.assertNotIn("remediation", booking)
        blob = json.dumps(result)
        for private in ("secret://", "@svc.example", "conn-", "token"):
            self.assertNotIn(private, blob)

    def test_a_grant_missing_a_selected_tool_blocks_and_names_the_tool(self):
        self.grant(scopes=["calendar.availability"])
        booking = skill(self.applied(), "book-appointment")
        self.assertEqual((booking["ready"], booking["dependencies"][0]["status"]), (False, "grant-scope"))
        self.assertEqual(booking["dependencies"][0]["missingTools"], ["calendar.book"])
        self.assertIn("calendar.book", booking["remediation"])
        # A partially scoped grant cannot make the family appear fully usable to
        # a model or other manifest consumer when a selected Tool is denied.
        family = next(item for item in self.applied()["capabilities"] if item["family"] == "calendar")
        self.assertEqual((family["status"], family["grant"]), ("unavailable", "unavailable"))

    def test_several_partial_grants_cannot_combine_into_full_family_readiness(self):
        self.grant(scopes=["calendar.availability"])
        self.grant(scopes=["calendar.book"])
        result = self.applied()
        booking = skill(result, "book-appointment")
        self.assertFalse(booking["ready"])
        self.assertEqual(booking["dependencies"][0]["status"], "grant-scope")
        family = next(item for item in result["capabilities"] if item["family"] == "calendar")
        self.assertEqual((family["status"], family["grant"]), ("unavailable", "unavailable"))

    def test_the_best_covering_grant_counts_when_there_are_several(self):
        self.grant(scopes=["calendar.availability"])
        self.grant(scopes=BOTH)
        self.assertTrue(skill(self.applied(), "book-appointment")["ready"])

    def test_revoked_expired_and_failed_grants_block_but_a_degraded_one_still_works(self):
        created = self.grant()
        self.registry.set_health(created["id"], "degraded", by="jordan@mg.example")
        degraded = skill(self.applied(), "book-appointment")
        self.assertEqual((degraded["ready"], degraded["dependencies"][0]["grant"]["state"]), (True, "degraded"))
        self.registry.set_health(created["id"], "failed", by="jordan@mg.example")
        failed = skill(self.applied(), "book-appointment")
        self.assertEqual((failed["ready"], failed["dependencies"][0]["status"]), (False, "grant-unavailable"))
        self.assertIn("revoked, expired or failed", failed["remediation"])

    def test_revocation_and_expiry_block_only_the_coworker_they_belong_to(self):
        first = self.grant("receptionist")
        self.grant("second-coworker")
        self.registry.revoke(first["id"], by="jordan@mg.example", now=NOW + 1)
        self.assertEqual(skill(self.applied(), "book-appointment")["dependencies"][0]["status"], "grant-unavailable")
        other = self.applied(manifest_for("second-coworker"), now=NOW + 1)
        self.assertTrue(skill(other, "book-appointment")["ready"])
        soon = self.grant("third-coworker", expires_at=NOW + 100)
        third = manifest_for("third-coworker")
        self.assertTrue(skill(self.applied(third, now=NOW + 99), "book-appointment")["ready"])
        self.assertEqual(skill(self.applied(third, now=NOW + 100), "book-appointment")["dependencies"][0]["status"], "grant-unavailable")
        self.assertEqual(soon["agent"], "third-coworker")

    def test_a_grant_for_a_different_provider_does_not_satisfy_the_bound_one(self):
        self.grant(provider="microsoft")
        self.assertEqual(skill(self.applied(), "book-appointment")["dependencies"][0]["status"], "no-grant")

    def test_a_grant_for_another_capability_family_cannot_authorize_calendar(self):
        # The provider string alone is not a grant identity: one vendor can
        # serve mail and calendar with independently scoped authorizations.
        self.grant(family="mail", provider="google", scopes=BOTH)
        booking = skill(self.applied(), "book-appointment")
        self.assertFalse(booking["ready"])
        self.assertEqual(booking["dependencies"][0]["status"], "no-grant")
        self.assertIn("its own calendar", booking["remediation"])

    def test_a_same_provider_grant_in_the_correct_family_is_still_accepted(self):
        self.grant(family="mail", provider="google", scopes=BOTH)
        self.grant(family="calendar", provider="google", scopes=BOTH)
        booking = skill(self.applied(), "book-appointment")
        self.assertTrue(booking["ready"])
        self.assertEqual(booking["dependencies"][0]["grant"]["state"], "active")

    def test_an_optional_dependency_without_a_grant_degrades_instead_of_blocking(self):
        self.grant("other-coworker")
        manifest = copy.deepcopy(self.manifest)
        dependency = skill(manifest, "book-appointment")["dependencies"][0]
        dependency["required"] = False
        result = skill(self.applied(manifest), "book-appointment")
        self.assertEqual((result["ready"], result["dependencies"][0]["status"], result["dependencies"][0]["grantIssue"]),
                         (True, "degraded", "no-grant"))
        self.assertIn("fallbackTier", result["dependencies"][0])

    # -- failing closed ------------------------------------------------------------------------------

    def test_a_registry_that_cannot_be_read_blocks_connected_skills_instead_of_being_ignored(self):
        self.grant()
        self.registry.path.write_text("{broken", encoding="utf-8")
        result = self.applied()
        self.assertEqual(result["grantRegistry"], "unreadable")
        self.assertEqual(skill(result, "book-appointment")["dependencies"][0]["status"], "no-grant")
        self.assertTrue(skill(result, "answer-faq")["ready"])
        other_tenant = json.loads(json.dumps({"schemaVersion": 1, "tenant": "other-prod", "grants": {}}))
        self.registry.path.write_text(json.dumps(other_tenant), encoding="utf-8")
        self.assertEqual(self.applied()["grantRegistry"], "unreadable")

    def test_deleted_initialized_grant_registry_never_restores_legacy_access(self):
        # An existing grant registry leaves a durable lock-file witness. Losing
        # the data file must not silently reactivate an environment-level token.
        self.grant()
        self.assertTrue(skill(self.applied(), "book-appointment")["ready"])
        self.registry.path.unlink()
        self.assertTrue((self.root / f".{TENANT}.grants.lock").is_file())
        result = self.applied()
        self.assertEqual(result["grantRegistry"], "unreadable")
        self.assertFalse(skill(result, "book-appointment")["ready"])
        self.assertEqual(skill(result, "book-appointment")["dependencies"][0]["status"], "no-grant")
        self.assertTrue(skill(result, "answer-faq")["ready"])

    def test_nonregular_grant_registry_with_witness_blocks_connected_skill(self):
        self.grant()
        self.registry.path.unlink()
        self.registry.path.mkdir()
        result = self.applied()
        self.assertEqual(result["grantRegistry"], "unreadable")
        self.assertFalse(skill(result, "book-appointment")["ready"])
        self.registry.path.rmdir()
        self.registry.path.symlink_to(self.root / "nonexistent-grants.json")
        result = self.applied()
        self.assertEqual(result["grantRegistry"], "unreadable")
        self.assertFalse(skill(result, "book-appointment")["ready"])

    def test_a_manifest_that_does_not_name_its_environment_is_left_alone(self):
        self.grant()
        bare = {key: value for key, value in self.manifest.items() if key != "client"}
        self.assertEqual(readiness.overlay_from_root(bare, self.root, now=NOW), bare)

    # -- hygiene ------------------------------------------------------------------------------------

    def test_the_overlay_does_not_mutate_its_input_and_is_idempotent(self):
        self.grant(scopes=["calendar.availability"])
        snapshot = copy.deepcopy(self.manifest)
        once = self.applied()
        self.assertEqual(self.manifest, snapshot)
        self.assertEqual(self.applied(once), once)

    def test_the_readiness_text_describes_each_grant_state(self):
        self.grant(scopes=["calendar.availability"])
        report = [{"agent": "receptionist", "environment": "prod", "skills": self.applied()["skills"]}]
        text = compiler.format_readiness(report)
        self.assertIn("this coworker's grant does not cover calendar.book", text)
        self.assertIn("Next: Ask the operator to re-scope", text)

    # -- the sidecar's manifest -------------------------------------------------------------------------

    def test_the_sidecar_manifest_reflects_grants_only_when_a_registry_exists(self):
        manifest_path = pathlib.Path(self._tmp.name) / "manifest.json"
        manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        desired = test_desired()
        desired["source"]["client"] = "acme"
        desired_path = pathlib.Path(self._tmp.name) / "desired.json"
        desired_path.write_text(json.dumps(desired), encoding="utf-8")
        governance = pathlib.Path(self._tmp.name) / "gov"
        runtime = runtime_module.GovernedToolRuntime(
            desired_path, governance / "audit.jsonl", governance / "approvals",
            environ={"MODEL_GARDEN_CAPABILITY_MANIFEST": str(manifest_path)})
        self.assertTrue(skill(runtime.capabilities(), "book-appointment")["ready"])  # legacy: no registry
        registry = grants_module.GrantRegistry(governance / readiness.REGISTRY_DIR, tenant=TENANT)
        registry.create(agent="other-coworker", family="calendar", provider="google", connection_id="c1", isolation="own-identity",
                        acting_identity="other@svc.example", secret_ref="secret://other/token", scopes=BOTH,
                        owner="owner@client.example", by="jordan@mg.example", now=NOW)
        self.assertFalse(skill(runtime.capabilities(), "book-appointment")["ready"])
        registry.create(agent="receptionist", family="calendar", provider="google", connection_id="c2", isolation="own-identity",
                        acting_identity="reception@svc.example", secret_ref="secret://reception/token", scopes=BOTH,
                        owner="owner@client.example", by="jordan@mg.example", now=NOW)
        self.assertTrue(skill(runtime.capabilities(), "book-appointment")["ready"])
        registry.path.unlink()
        # This is the actual sidecar capability path, not only the pure overlay.
        # A deleted initialized grant store must never reactivate booking.
        self.assertFalse(skill(runtime.capabilities(), "book-appointment")["ready"])


if __name__ == "__main__":
    unittest.main()

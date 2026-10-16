"""Credential-free checks that a deployment which changes the Policy rules leaves a chained ledger entry."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import pathlib
import stat
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


deploy = _load("policy_deploy_under_test", "platform/core/policy_deploy.py")
policy = _load("policy_for_deploy", "platform/core/policy.py")
govern = _load("govern_for_policy_deploy", "scripts/govern-action.py")
runtime_module = _load("governed_mcp_for_policy_deploy", "platform/runtime/governed_mcp.py")
OWNER = "owner@client.example"
ALICE = "alice@client.example"


def signed(rule: dict) -> dict:
    return {**rule, "signoff": {"owner": OWNER, "ruleDigest": policy.rule_digest(rule)}}


def document(*rules: dict) -> list[dict]:
    return [{"apiVersion": "modelgarden.ai/v1", "kind": "Policy", "metadata": {"name": "client-policy", "owner": "client"},
             "spec": {"rules": list(rules)}}]


DENY = {"id": "no-booking", "effect": "deny", "actions": ["calendar.book"]}
ALLOW = signed({"id": "allow-faq", "effect": "allow", "actions": ["mail.send"]})


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name) / "governance"
        self.audit = self.root / "audit.jsonl"

    def apply(self, policies, *, by=ALICE):
        return deploy.record_deployment(
            policies, self.root, changed_by=by,
            record=lambda before, after, who: govern.record_policy_change(self.audit, before, after, changed_by=who))

    def events(self) -> list[dict]:
        if not self.audit.exists():
            return []
        return [json.loads(line) for line in self.audit.read_text().splitlines() if line.strip()]

    # -- what gets ledgered -------------------------------------------------------------------

    def test_the_first_start_ledgers_the_rules_it_started_with_and_who_deployed_them(self):
        result = self.apply(document(DENY, ALLOW))
        self.assertEqual((result["changed"], result["baseline"], result["changedBy"]), (True, "none", ALICE))
        (event,) = self.events()
        self.assertEqual((event["event"], event["changedBy"]), ("policy-change", ALICE))
        self.assertEqual(event["details"]["added"], ["allow-faq", "no-booking"])
        self.assertEqual(event["details"]["relaxed"], ["allow-faq"])
        self.assertEqual(result["newVersion"], policy.policy_version(document(DENY, ALLOW)))

    def test_an_unchanged_restart_records_nothing(self):
        self.apply(document(DENY, ALLOW))
        again = self.apply(document(DENY, ALLOW), by="runtime:deployment")
        self.assertEqual((again["changed"], again["baseline"]), (False, "recorded"))
        self.assertEqual(len(self.events()), 1)

    def test_a_modified_rule_is_ledgered_with_the_rule_level_diff_and_the_new_deployer(self):
        self.apply(document(DENY, ALLOW))
        looser = signed({"id": "allow-faq", "effect": "allow", "actions": ["mail.send", "calendar.book"]})
        result = self.apply(document(DENY, looser), by="jordan@mg.example")
        self.assertEqual((result["changed"], result["relaxed"]), (True, ["allow-faq"]))
        event = self.events()[-1]
        self.assertEqual((event["changedBy"], event["details"]["modified"], event["details"]["added"]), ("jordan@mg.example", ["allow-faq"], []))
        self.assertNotEqual(event["details"]["previousVersion"], event["details"]["newVersion"])

    def test_removing_a_deny_rule_is_flagged_as_relaxing(self):
        self.apply(document(DENY))
        result = self.apply(document())
        self.assertEqual((result["changed"], result["relaxed"]), (True, ["no-booking"]))

    def test_the_trail_is_part_of_the_chained_ledger_and_verifies(self):
        self.apply(document(DENY))
        self.apply(document(DENY, ALLOW))
        self.apply(document(ALLOW))
        verified = govern.verify_audit_chain(self.audit)
        self.assertEqual((verified["valid"], verified["events"]), (True, 3))

    def test_a_deployment_with_no_rules_at_all_writes_a_baseline_but_no_event(self):
        result = self.apply(None)
        self.assertEqual((result["changed"], result["baseline"]), (False, "none"))
        self.assertEqual(self.events(), [])
        self.assertTrue((self.root / deploy.APPLIED_FILE).is_file())
        self.assertTrue(self.apply(document(DENY))["changed"])

    # -- the applied record ------------------------------------------------------------------------

    def test_the_applied_record_is_private_and_holds_only_the_policy_resources(self):
        self.apply(document(DENY, ALLOW))
        path = self.root / deploy.APPLIED_FILE
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        stored = json.loads(path.read_text())
        self.assertEqual(set(stored), {"schemaVersion", "policies"})
        self.assertEqual(stored["policies"], document(DENY, ALLOW))

    def test_a_damaged_applied_record_is_never_trusted_and_is_repaired_by_ledgering_the_current_rules(self):
        self.apply(document(DENY))
        (self.root / deploy.APPLIED_FILE).write_text("{not json", encoding="utf-8")
        result = self.apply(document(DENY))
        self.assertEqual((result["changed"], result["baseline"]), (True, "unreadable"))
        self.assertEqual(json.loads((self.root / deploy.APPLIED_FILE).read_text())["policies"], document(DENY))
        (self.root / deploy.APPLIED_FILE).write_text(json.dumps({"schemaVersion": 9, "policies": []}), encoding="utf-8")
        self.assertEqual(self.apply(document(DENY))["baseline"], "unreadable")

    def test_if_the_ledger_cannot_be_written_nothing_is_remembered_and_the_error_reaches_the_caller(self):
        self.apply(document(DENY))
        before = (self.root / deploy.APPLIED_FILE).read_text()

        def failing(_before, _after, _who):
            raise OSError("audit volume is read-only")

        with self.assertRaises(OSError):
            deploy.record_deployment(document(DENY, ALLOW), self.root, changed_by=ALICE, record=failing)
        self.assertEqual((self.root / deploy.APPLIED_FILE).read_text(), before)
        self.assertTrue(self.apply(document(DENY, ALLOW))["changed"])  # the change is still ledgered on the next attempt

    def test_malformed_policy_input_is_treated_as_no_rules(self):
        self.assertEqual(self.apply("not a list")["changed"], False)
        self.assertEqual(self.apply([1, "x", None])["changed"], False)

    # -- who deployed it ---------------------------------------------------------------------------

    def test_the_deployer_comes_from_the_deploy_environment_or_falls_back_to_the_runtime(self):
        self.assertEqual(deploy.deployer({"MODEL_GARDEN_DEPLOYED_BY": " jordan@mg.example "}), "jordan@mg.example")
        for value in ("", "   ", "two words", "bad\nid", "x" * 200):
            self.assertEqual(deploy.deployer({"MODEL_GARDEN_DEPLOYED_BY": value}), deploy.DEFAULT_IDENTITY)
        self.assertEqual(deploy.deployer({}), deploy.DEFAULT_IDENTITY)

    # -- the sidecar start-up hook -------------------------------------------------------------------

    def runtime(self, policies):
        desired = {"schemaVersion": 1, "source": {"name": "receptionist", "client": "acme"}, "agent": {"spec": {}}, "tools": []}
        if policies is not None:
            desired["policies"] = policies
        path = self.root.parent / "desired.json"
        path.write_text(json.dumps(desired), encoding="utf-8")
        return runtime_module.GovernedToolRuntime(path, self.audit, self.root / "approvals", environ={})

    def test_the_sidecar_hook_ledgers_a_policy_change_before_serving_and_not_on_a_plain_restart(self):
        first = runtime_module.ledger_policy_deployment(self.runtime(document(DENY)), {"MODEL_GARDEN_DEPLOYED_BY": ALICE})
        restart = runtime_module.ledger_policy_deployment(self.runtime(document(DENY)), {"MODEL_GARDEN_DEPLOYED_BY": ALICE})
        changed = runtime_module.ledger_policy_deployment(self.runtime(document(DENY, ALLOW)), {})
        self.assertEqual((first["changed"], restart["changed"], changed["changed"]), (True, False, True))
        events = self.events()
        self.assertEqual([event["changedBy"] for event in events], [ALICE, deploy.DEFAULT_IDENTITY])
        self.assertEqual(events[1]["details"]["relaxed"], ["allow-faq"])

    def test_the_main_entry_point_and_the_compose_file_are_wired_to_the_hook(self):
        source = (ROOT / "platform/runtime/governed_mcp.py").read_text(encoding="utf-8")
        main = source[source.index("def main()"):]
        self.assertIn("ledger_policy_deployment(runtime)", main)
        self.assertLess(main.index("ledger_policy_deployment(runtime)"), main.index("server.run("))
        deployer = _load("deploy_docker_for_policy_deploy", "scripts/deploy-client-docker.py")
        self.assertIn(pathlib.Path("platform/core/policy_deploy.py"), deployer.RUNTIME_SOURCE_PATHS)
        compose = deployer._compose("acme-dev", "openai", {"version": "1", "commit": "c"}, built_in_tools=True)
        self.assertIn("MODEL_GARDEN_DEPLOYED_BY", compose)

    def test_policy_input_is_not_mutated(self):
        original = document(DENY, ALLOW)
        snapshot = copy.deepcopy(original)
        self.apply(original)
        self.assertEqual(original, snapshot)


if __name__ == "__main__":
    unittest.main()

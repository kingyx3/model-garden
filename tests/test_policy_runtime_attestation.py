"""Runtime regression: an owner signature checked only at compile time must not become a forged Tool waiver (#114).

The binding's public keys are public, non-secret trust anchors. These tests use synthetic
keys and never claim a client-owned key or protected deployment has been verified.
"""
from __future__ import annotations

import importlib.util
import pathlib
import shutil
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
OWNER = "owner@client.example"
RULE = {"id": "owner-approved-booking", "effect": "allow", "actions": ["calendar.book"], "principals": ["public"]}


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


policy = load("policy_runtime_attestation", "platform/core/policy.py")
govern = load("govern_runtime_attestation", "scripts/govern-action.py")
compiler = load("compiler_runtime_attestation", "scripts/compile-workspace.py")


def state(rule, keys):
    return {
        "schemaVersion": 1,
        "source": {"name": "receptionist"},
        "agent": {"spec": {"approvals": {"externalActions": "none"}, "security": {"dataClassification": "internal"}}},
        "tools": [{"metadata": {"name": "calendar.book"}, "spec": {"risk": "write", "approvalRequired": True}}],
        "policies": [{"spec": {"rules": [rule]}}],
        "policyOwnerKeys": keys,
    }


class RuntimeOwnerSignatureTests(unittest.TestCase):
    def setUp(self):
        self.owner = policy.generate_owner_key()
        self.signed = policy.sign_rule(RULE, OWNER, self.owner["private"])
        self.keys = {OWNER: self.owner["public"]}

    def authorize(self, rule, keys=None, *, access="public"):
        return govern.authorize_action(
            state(rule, self.keys if keys is None else keys), "calendar.book", {"slot": "10:00"},
            initiating_user="customer:synthetic", principal_access=access,
        )

    def test_correct_owner_signature_is_reverified_at_the_governed_boundary(self):
        result = self.authorize(self.signed)
        self.assertEqual((result["decision"], result["ruleId"]), ("allow", RULE["id"]))

    def test_typed_owner_and_matching_digest_cannot_waive_public_write_floor(self):
        forged = {**RULE, "signoff": {"owner": OWNER, "ruleDigest": policy.rule_digest(RULE)}}
        self.assertTrue(policy.signoff_valid(forged))  # the legacy content check alone is insufficient
        self.assertEqual(self.authorize(forged)["decision"], "deny")
        self.assertEqual(self.authorize(forged, access="member")["decision"], "require-approval")

    def test_other_key_revoked_key_and_changed_rule_cannot_waive_approval(self):
        other = policy.generate_owner_key()
        wrong = policy.sign_rule(RULE, OWNER, other["private"])
        changed = {**self.signed, "actions": ["calendar.book", "calendar.cancel"]}
        for rule, keys in ((wrong, self.keys), (self.signed, {}), (changed, self.keys)):
            with self.subTest(rule=rule["id"], keys=bool(keys)):
                self.assertEqual(self.authorize(rule, keys)["decision"], "deny")
                self.assertEqual(self.authorize(rule, keys, access="member")["decision"], "require-approval")

    def test_invalid_signature_never_reaches_executor(self):
        forged = {**RULE, "signoff": {"owner": OWNER, "ruleDigest": policy.rule_digest(RULE)}}
        calls = []
        with self.assertRaises(PermissionError):
            govern.execute_action(
                state(forged, self.keys), "calendar.book", {"slot": "10:00"},
                lambda args: calls.append(args), principal_access="public", initiating_user="customer:synthetic",
            )
        self.assertEqual(calls, [])

    def test_compiler_carries_public_owner_keys_into_the_runtime_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "workspace"
            shutil.copytree(ROOT / "examples/workspace", workspace)
            policy_path = workspace / "policies/default.yaml"
            resource = yaml.safe_load(policy_path.read_text())
            resource["spec"]["rules"].append(self.signed)
            policy_path.write_text(yaml.safe_dump(resource))
            binding = {
                "environment": "dev",
                "connectors": {"crm": {"provider": "built-in-crm", "tier": 0},
                               "kb": {"provider": "text-knowledge", "tier": 0}},
                "governance": {"owners": [OWNER, "business-owner"], "ownerKeys": self.keys},
            }
            compiled = compiler.compile_workspace(workspace, "receptionist", environment_binding=binding)[0]
            self.assertEqual(compiled["policyOwnerKeys"], self.keys)
            self.assertNotIn(self.owner["private"], str(compiled))


if __name__ == "__main__":
    unittest.main()

"""Public booking cannot be authorized by a client-editable digest alone (#114).

Credential-free adversarial policy and governed-boundary tests. No client key
or live booking is asserted by this suite.
"""
from __future__ import annotations

import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
OWNER = "owner@client.example"
RULE = {"id": "public-book", "effect": "allow", "actions": ["calendar.book"], "principals": ["public"]}


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy = load("public_owner_floor_policy", "platform/core/policy.py")
govern = load("public_owner_floor_govern", "scripts/govern-action.py")


def document(rule):
    return {"spec": {"rules": [rule]}}


def state(rule, keys=None):
    result = {
        "schemaVersion": 1,
        "source": {"name": "receptionist"},
        "agent": {"spec": {"approvals": {"externalActions": "none"}, "security": {"dataClassification": "internal"}}},
        "tools": [{"metadata": {"name": "calendar.book"}, "spec": {"risk": "write", "approvalRequired": True}}],
        "policies": [document(rule)],
    }
    if keys is not None:
        result["policyOwnerKeys"] = keys
    return result


class PublicOwnerKeyFloorTests(unittest.TestCase):
    def setUp(self):
        self.owner = policy.generate_owner_key()
        self.keys = {OWNER: self.owner["public"]}
        self.signed = policy.sign_rule(RULE, OWNER, self.owner["private"])
        self.digest_only = {**RULE, "signoff": {"owner": OWNER, "ruleDigest": policy.rule_digest(RULE)}}

    def test_public_write_requires_a_trusted_owner_key_even_for_well_formed_signature(self):
        for rule in (self.digest_only, self.signed):
            for keys in (None, {}, {"other@client.example": self.owner["public"]}):
                with self.subTest(signed="signature" in rule["signoff"], keys=keys):
                    result = govern.authorize_action(
                        state(rule, keys), "calendar.book", {"slot": "10:00"},
                        initiating_user="public:synthetic", principal_access="public")
                    self.assertEqual((result["decision"], result["reason"]), ("deny", "principal-not-authorized"))
        result = govern.authorize_action(
            state(self.signed, self.keys), "calendar.book", {"slot": "10:00"},
            initiating_user="public:synthetic", principal_access="public")
        self.assertEqual((result["decision"], result["ruleId"]), ("allow", "public-book"))

    def test_missing_keys_prevent_public_side_effects(self):
        effects = []
        for rule in (self.digest_only, self.signed):
            with self.assertRaises(PermissionError):
                govern.execute_action(
                    state(rule), "calendar.book", {"slot": "10:00"},
                    lambda args: effects.append(args), initiating_user="public:synthetic",
                    principal_access="public")
        self.assertEqual(effects, [])

    def test_compiler_rejects_public_relaxation_without_trusted_owner_keys(self):
        tools = state(self.signed)["tools"]
        for rule in (self.digest_only, self.signed):
            errors = policy.relaxation_errors([document(rule)], tools, [OWNER], None)
            self.assertTrue(any("governance.ownerKeys" in error for error in errors), errors)
        self.assertEqual(policy.relaxation_errors([document(self.signed)], tools, [OWNER], self.keys), [])
        read = {"id": "public-read", "effect": "allow", "actions": ["calendar.availability"], "principals": ["public"]}
        self.assertEqual(policy.relaxation_errors([document(read)], [
            {"metadata": {"name": "calendar.availability"}, "spec": {"risk": "read"}}
        ], None, None), [])

    def test_untrusted_keys_and_tampered_rule_do_not_waive_public_floor(self):
        other = policy.generate_owner_key()
        forged = policy.sign_rule(RULE, OWNER, other["private"])
        modified = {**self.signed, "actions": ["calendar.book", "calendar.cancel"]}
        for rule in (forged, modified):
            self.assertEqual(govern.authorize_action(
                state(rule, self.keys), "calendar.book", {},
                initiating_user="public:synthetic", principal_access="public")["decision"], "deny")


if __name__ == "__main__":
    unittest.main()

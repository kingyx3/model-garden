"""Member write autonomy requires independently trusted owner signatures (#114).

Migration regression: a digest and owner label are not authentication, even for
members. Synthetic Ed25519 keys only; no tenant consent or provider call.
"""
from __future__ import annotations

import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
OWNER = "owner@client.example"
RULE = {"id": "member-book", "effect": "allow", "actions": ["calendar.book"]}


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


policy = load("member_signature_policy", "platform/core/policy.py")
govern = load("member_signature_govern", "scripts/govern-action.py")


def document(rule):
    return {"spec": {"rules": [rule]}}


def state(rule, keys=None):
    desired = {
        "schemaVersion": 1,
        "source": {"name": "receptionist"},
        "agent": {"spec": {"approvals": {"externalActions": "none"}, "security": {"dataClassification": "internal"}}},
        "tools": [{"metadata": {"name": "calendar.book"}, "spec": {"risk": "write", "approvalRequired": True}}],
        "policies": [document(rule)],
    }
    if keys is not None:
        desired["policyOwnerKeys"] = keys
    return desired


class MemberSignatureMigrationTests(unittest.TestCase):
    def setUp(self):
        self.key = policy.generate_owner_key()
        self.keys = {OWNER: self.key["public"]}
        self.signed = policy.sign_rule(RULE, OWNER, self.key["private"])
        self.digest = {**RULE, "signoff": {"owner": OWNER, "ruleDigest": policy.rule_digest(RULE)}}

    def test_member_digest_only_cannot_waive_tool_approval_or_execute(self):
        for keys in (None, {}, self.keys):
            with self.subTest(keys=keys):
                outcome = govern.authorize_action(
                    state(self.digest, keys), "calendar.book", {"slot": "10:00"},
                    initiating_user="member:synthetic", principal_access="member")
                self.assertEqual(outcome["decision"], "require-approval")
        self.assertEqual(govern.authorize_action(
            state(self.signed), "calendar.book", {}, initiating_user="member:synthetic",
            principal_access="member")["decision"], "require-approval")
        # A queued approval is not a provider execution; no fallback may dispatch.
        called = []
        result = govern.execute_action(
            state(self.digest), "calendar.book", {"slot": "10:00"},
            lambda args: called.append(args), initiating_user="member:synthetic",
            principal_access="member")
        self.assertEqual(result["status"], "pending-approval")
        self.assertEqual(called, [])

    def test_authenticated_member_rule_can_waive_default_only_with_matching_key(self):
        for keys in (self.keys, {}, {"other@client.example": self.key["public"]}):
            outcome = govern.authorize_action(
                state(self.signed, keys), "calendar.book", {},
                initiating_user="member:synthetic", principal_access="member")
            self.assertEqual(outcome["decision"], "allow" if keys == self.keys else "require-approval")
        other = policy.generate_owner_key()
        forged = policy.sign_rule(RULE, OWNER, other["private"])
        self.assertEqual(govern.authorize_action(
            state(forged, self.keys), "calendar.book", {},
            initiating_user="member:synthetic", principal_access="member")["decision"], "require-approval")

    def test_compiler_requires_owner_key_even_without_explicit_owner_list(self):
        tools = state(RULE)["tools"]
        for rule in (self.digest, self.signed):
            errors = policy.relaxation_errors([document(rule)], tools)
            self.assertTrue(any("governance.ownerKeys" in error for error in errors), errors)
        self.assertEqual(policy.relaxation_errors([document(self.signed)], tools, [OWNER], self.keys), [])

    def test_read_only_and_restrictive_rules_need_no_signature(self):
        tools = [{"metadata": {"name": "calendar.availability"}, "spec": {"risk": "read"}}]
        rules = [document({"id": "read", "effect": "allow", "actions": ["calendar.availability"]}),
                 document({"id": "deny", "effect": "deny", "actions": ["calendar.book"]})]
        self.assertEqual(policy.relaxation_errors(rules, tools), [])


if __name__ == "__main__":
    unittest.main()

"""Credential-free checks for the environment's named owners: a relaxing rule must be signed by one of them."""
from __future__ import annotations

import importlib.util
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


policy = _load("policy_for_owners", "platform/core/policy.py")
compiler = _load("compiler_for_owners", "scripts/compile-workspace.py")

CONNECTORS = {
    "calendar": {"provider": "google", "tier": 1, "token_ref": "secret://google/dev"},
    "kb": {"provider": "text-knowledge", "tier": 0},
    "crm": {"provider": "built-in-crm", "tier": 0},
}
TOOLS = [{"metadata": {"name": "calendar.book"}, "spec": {"risk": "write", "approvalRequired": True}}]
RULE = {"id": "auto-book", "effect": "allow", "actions": ["calendar.book"]}


def signed(owner: str, rule: dict = RULE) -> dict:
    return {**rule, "signoff": {"owner": owner, "ruleDigest": policy.rule_digest(rule)}}


def doc(rule: dict) -> dict:
    return {"apiVersion": "modelgarden.ai/v1", "kind": "Policy", "metadata": {"name": "p", "owner": "o"}, "spec": {"rules": [rule]}}


class RelaxationTests(unittest.TestCase):
    def errors(self, rule, owners):
        return policy.relaxation_errors([doc(rule)], TOOLS, owners)

    def test_no_owner_list_keeps_the_previous_behaviour(self):
        self.assertEqual(self.errors(signed("anyone@x.example"), None), [])

    def test_a_named_owner_may_sign_and_matching_ignores_case_and_spacing(self):
        self.assertEqual(self.errors(signed(" Owner@Client.example "), ["owner@client.example"]), [])

    def test_anyone_else_is_refused_even_with_a_correct_digest(self):
        rule = signed("intruder@evil.example")
        self.assertTrue(policy.signoff_valid(rule))
        (error,) = self.errors(rule, ["owner@client.example"])
        self.assertIn("auto-book", error)
        self.assertIn("'intruder@evil.example'", error)
        self.assertIn("governance.owners", error)

    def test_an_empty_list_names_nobody_so_every_relaxing_rule_is_refused(self):
        self.assertEqual(len(self.errors(signed("owner@client.example"), [])), 1)

    def test_an_unsigned_rule_still_reports_the_missing_signoff_not_the_owner_list(self):
        (error,) = self.errors(dict(RULE), ["owner@client.example"])
        self.assertIn("needs the client owner's sign-off", error)

    def test_rules_that_do_not_relax_anything_need_no_owner(self):
        read_only = {"id": "reads", "effect": "allow", "actions": ["calendar.availability"]}
        tools = [{"metadata": {"name": "calendar.availability"}, "spec": {"risk": "read"}}]
        self.assertEqual(policy.relaxation_errors([doc(read_only)], tools, ["owner@client.example"]), [])
        deny = {"id": "no", "effect": "deny", "actions": ["calendar.book"]}
        self.assertEqual(policy.relaxation_errors([doc(deny)], TOOLS, []), [])

    def test_a_public_booking_rule_needs_a_named_owner_too(self):
        rule = {**RULE, "id": "public-book", "principals": ["public"]}
        self.assertEqual(len(self.errors(signed("intruder@evil.example", rule), ["owner@client.example"])), 1)
        self.assertEqual(self.errors(signed("owner@client.example", rule), ["owner@client.example"]), [])


class BindingTests(unittest.TestCase):
    def test_owners_are_read_from_a_flat_or_rendered_binding(self):
        flat = {"governance": {"owners": [" Owner@Client.example ", "owner@client.example", "b@client.example"]}}
        self.assertEqual(policy.owners_from_binding(flat), ["b@client.example", "owner@client.example"])
        rendered = {"kind": "EnvironmentBinding", "spec": flat}
        self.assertEqual(policy.owners_from_binding(rendered), ["b@client.example", "owner@client.example"])

    def test_no_binding_or_no_governance_section_means_no_list(self):
        for binding in (None, {}, {"governance": {}}, {"kind": "EnvironmentBinding", "spec": {"connectors": {}}}, "x"):
            self.assertIsNone(policy.owners_from_binding(binding), binding)
        self.assertEqual(policy.owners_from_binding({"governance": {"owners": []}}), [])

    def test_a_malformed_list_is_an_error_not_silently_ignored(self):
        for bad in ("owner@client.example", [1], ["", "a@b.example"], {"a": 1}):
            with self.assertRaises(ValueError, msg=str(bad)):
                policy.owners_from_binding({"governance": {"owners": bad}})


class CompileTests(unittest.TestCase):
    def workspace(self, root: pathlib.Path, owner: str) -> pathlib.Path:
        target = root / "client"
        shutil.copytree(ROOT / "examples" / "workspace", target)
        path = target / "policies" / "default.yaml"
        document = yaml.safe_load(path.read_text())
        for rule in document["spec"]["rules"]:
            if rule["id"] == "approve-external-write":
                rule["actions"] = [a for a in rule["actions"] if a != "*.create"]
        document["spec"]["rules"].append(signed(owner, {"id": "auto-create", "effect": "allow", "actions": ["crm.create"]}))
        path.write_text(yaml.safe_dump(document))
        # Approved business knowledge also needs a named owner once the environment lists its owners (#116).
        knowledge = target / "knowledge" / "rules-of-engagement.yaml"
        source = yaml.safe_load(knowledge.read_text())
        source["metadata"]["owner"] = "owner@client.example"
        knowledge.write_text(yaml.safe_dump(source))
        return target

    def compile(self, workspace, binding):
        return compiler.compile_workspace(workspace, "receptionist", environment_binding=binding)[0]

    def test_compilation_refuses_a_rule_signed_by_someone_the_environment_does_not_name(self):
        binding = {"environment": "dev", "connectors": CONNECTORS, "governance": {"owners": ["owner@client.example"]}}
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(self.compile(self.workspace(pathlib.Path(tmp), "owner@client.example"), binding))
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, "auto-create.*not a named owner"):
            self.compile(self.workspace(pathlib.Path(tmp), "consultant@agency.example"), binding)

    def test_without_a_named_owner_list_the_digest_alone_is_still_accepted(self):
        for binding in ({"environment": "dev", "connectors": CONNECTORS}, None):
            with tempfile.TemporaryDirectory() as tmp:
                self.assertTrue(self.compile(self.workspace(pathlib.Path(tmp), "anyone@x.example"), binding))

    def test_a_malformed_owner_list_fails_compilation_with_a_clear_message(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, "governance.owners"):
            self.compile(self.workspace(pathlib.Path(tmp), "owner@client.example"), {"environment": "dev", "connectors": CONNECTORS, "governance": {"owners": "me"}})


if __name__ == "__main__":
    unittest.main()

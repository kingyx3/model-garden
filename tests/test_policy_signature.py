"""Credential-free checks that a relaxing Policy rule can be authenticated by the owner's Ed25519 key (#114)."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("policy_for_signature", ROOT / "platform" / "core" / "policy.py")
policy = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = policy
_spec.loader.exec_module(policy)

TOOLS = [{"metadata": {"name": "calendar.book"}, "spec": {"risk": "write", "approvalRequired": True}}]
RULE = {"id": "auto-book", "effect": "allow", "actions": ["calendar.book"]}
OWNER = "owner@client.example"


def doc(rule):
    return {"apiVersion": "modelgarden.ai/v1", "kind": "Policy", "metadata": {"name": "p", "owner": "o"}, "spec": {"rules": [rule]}}


class SignatureTests(unittest.TestCase):
    def setUp(self):
        self.key = policy.generate_owner_key()
        self.keys = {OWNER: self.key["public"]}

    def errors(self, rule, keys=None):
        return policy.relaxation_errors([doc(rule)], TOOLS, None, self.keys if keys is None else keys)

    def test_a_rule_signed_with_the_owners_key_is_accepted(self):
        self.assertEqual(self.errors(policy.sign_rule(RULE, OWNER, self.key["private"])), [])

    def test_a_typed_name_and_digest_without_a_signature_is_refused(self):
        forged = {**RULE, "signoff": {"owner": OWNER, "ruleDigest": policy.rule_digest(RULE)}}
        self.assertTrue(policy.signoff_valid(forged))
        (error,) = self.errors(forged)
        self.assertIn("signoff.signature", error)

    def test_a_signature_by_another_key_is_refused(self):
        other = policy.generate_owner_key()
        (error,) = self.errors(policy.sign_rule(RULE, OWNER, other["private"]))
        self.assertIn("auto-book", error)

    def test_editing_the_rule_after_signing_is_refused(self):
        signed = policy.sign_rule(RULE, OWNER, self.key["private"])
        edited = {**signed, "actions": ["calendar.book", "calendar.cancel"]}
        self.assertTrue(self.errors(edited))

    def test_the_signature_is_bound_to_the_owner_name(self):
        signed = policy.sign_rule(RULE, OWNER, self.key["private"])
        keys = {OWNER: self.key["public"], "intern@client.example": self.key["public"]}
        swapped = {**signed, "signoff": {**signed["signoff"], "owner": "intern@client.example"}}
        self.assertTrue(self.errors(swapped, keys))

    def test_an_owner_without_a_listed_key_is_refused(self):
        self.assertTrue(self.errors(policy.sign_rule(RULE, "stranger@x.example", self.key["private"])))

    def test_without_owner_keys_behaviour_is_unchanged(self):
        unsigned = {**RULE, "signoff": {"owner": OWNER, "ruleDigest": policy.rule_digest(RULE)}}
        self.assertEqual(policy.relaxation_errors([doc(unsigned)], TOOLS, None, None), [])

    def test_the_binding_keys_are_validated(self):
        self.assertIsNone(policy.owner_keys_from_binding({"spec": {}}))
        good = {"kind": "EnvironmentBinding", "spec": {"governance": {"ownerKeys": {" Owner@Client.example ": self.key["public"]}}}}
        self.assertEqual(policy.owner_keys_from_binding(good), {OWNER: self.key["public"]})
        for bad in ({"x": "not-base64!"}, {"x": "AAAA"}, ["list"]):
            with self.assertRaises(ValueError):
                policy.owner_keys_from_binding({"kind": "EnvironmentBinding", "spec": {"governance": {"ownerKeys": bad}}})


class CommandTests(unittest.TestCase):
    def test_keygen_then_sign_makes_a_rule_the_compiler_accepts(self):
        script = str(ROOT / "scripts" / "policy-signoff.py")
        keys = json.loads(subprocess.run([sys.executable, script, "keygen"], capture_output=True, text=True, check=True).stdout)
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "policy.yaml"
            path.write_text(yaml.safe_dump(doc(RULE)))
            key_file = pathlib.Path(tmp) / "owner.key"
            key_file.write_text(keys["private"])
            out = subprocess.run([sys.executable, script, "sign", str(path), "--rule", "auto-book", "--owner", OWNER, "--key-file", str(key_file)],
                                 capture_output=True, text=True, check=True).stdout
            signed = yaml.safe_load(out)
            self.assertEqual(policy.relaxation_errors([doc(signed)], TOOLS, None, {OWNER: keys["public"]}), [])
            missing = subprocess.run([sys.executable, script, "sign", str(path), "--rule", "nope", "--owner", OWNER, "--key-file", str(key_file)],
                                     capture_output=True, text=True)
            self.assertEqual(missing.returncode, 1)


if __name__ == "__main__":
    unittest.main()

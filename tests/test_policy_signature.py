"""Credential-free checks that a relaxing Policy rule can be authenticated by the owner's Ed25519 key (#114)."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

import jsonschema
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



class PolicyContractSignatureTests(unittest.TestCase):
    """The owner-signed rule must survive the actual v1 contract and compiler, not only the policy helper."""

    def setUp(self):
        self.key = policy.generate_owner_key()
        self.signed = policy.sign_rule(RULE, OWNER, self.key["private"])
        self.schema = json.loads((ROOT / "contracts/v1/policy.schema.json").read_text(encoding="utf-8"))

    def test_owner_signed_rule_is_valid_v1_policy_and_compiles_with_trusted_key(self):
        jsonschema.validate(doc(self.signed), self.schema)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "workspace"
            shutil.copytree(ROOT / "examples/workspace", workspace)
            policy_path = workspace / "policies/default.yaml"
            resource = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
            resource["spec"]["rules"].append(self.signed)
            policy_path.write_text(yaml.safe_dump(resource), encoding="utf-8")
            spec = importlib.util.spec_from_file_location("compiler_for_signed_policy_contract", ROOT / "scripts/compile-workspace.py")
            compiler = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(compiler)
            binding = {
                "environment": "dev",
                "connectors": {
                    "crm": {"provider": "built-in-crm", "tier": 0},
                    "kb": {"provider": "text-knowledge", "tier": 0},
                },
                "governance": {"owners": [OWNER], "ownerKeys": {OWNER: self.key["public"]}},
            }
            compiled = compiler.compile_workspace(workspace, "receptionist", environment_binding=binding)
            self.assertTrue(compiled)

    def test_signature_shape_is_bounded_and_extra_signoff_fields_are_rejected(self):
        jsonschema.validate(doc(self.signed), self.schema)
        for bad in ("", "not-base64", "A" * 90, "A" * 86 + "=!"):
            changed = {**self.signed, "signoff": {**self.signed["signoff"], "signature": bad}}
            with self.subTest(signature=bad[:16]), self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(doc(changed), self.schema)
        extra = {**self.signed, "signoff": {**self.signed["signoff"], "private": "must-not-be-stored"}}
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(doc(extra), self.schema)

    def test_schema_shape_alone_never_authorizes_a_forged_signature(self):
        forged = {**self.signed, "signoff": {**self.signed["signoff"], "signature": "A" * 86 + "=="}}
        jsonschema.validate(doc(forged), self.schema)
        self.assertTrue(policy.relaxation_errors([doc(forged)], TOOLS, [OWNER], {OWNER: self.key["public"]}))

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

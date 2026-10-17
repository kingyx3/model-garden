"""Credential-free checks that turning the call-transcript disclosure off needs the owner's signed lawful basis (#107)."""
from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lawful = load("lawful_basis_under_test", "platform/core/lawful_basis.py")
policy = load("policy_for_lawful_basis", "platform/core/policy.py")
transcript = load("transcript_for_lawful_basis", "platform/channels/call_transcript.py")

OWNER = "owner@client.example"
PROJECT = "acme-prod"
BASIS = "Callers are told at the start of every call by the switchboard recording notice"


def binding(block=None, keys=None):
    spec = {"channels": {"voice": {}}}
    if block is not None:
        spec["channels"]["voice"]["transcript"] = block
    if keys is not None:
        spec["governance"] = {"ownerKeys": keys}
    return {"kind": "EnvironmentBinding", "spec": spec}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = policy.generate_owner_key()
        cls.other = policy.generate_owner_key()

    def attestation(self, **overrides):
        values = {"owner": OWNER, "basis": BASIS, "project": PROJECT, "private_key": self.key["private"]}
        values.update(overrides)
        return lawful.sign_override(**values)

    def off(self, attestation=None, keys="default", **block):
        keys = {OWNER: self.key["public"]} if keys == "default" else keys
        body = {"disclosure": "off", "lawful_basis_override": attestation or self.attestation(), **block}
        return binding(body, keys)


class SignVerifyTests(Base):
    def test_round_trip_returns_the_owner(self):
        self.assertEqual(lawful.verify_override(self.attestation(), {OWNER: self.key["public"]}, project=PROJECT), OWNER)

    def test_owner_lookup_ignores_case_and_space(self):
        keys = {"owner@client.example": self.key["public"]}
        attestation = self.attestation(owner="  Owner@Client.Example ")
        self.assertEqual(lawful.verify_override(attestation, keys, project=PROJECT), "Owner@Client.Example")

    def test_each_signed_field_is_covered(self):
        keys = {OWNER: self.key["public"]}
        for field, value in (("basis", BASIS + " and more"), ("owner", "someone.else@client.example")):
            forged = {**self.attestation(), field: value}
            with self.assertRaises(ValueError, msg=field):
                lawful.verify_override(forged, {**keys, "someone.else@client.example": self.key["public"]}, project=PROJECT)

    def test_another_environment_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not signed by that owner"):
            lawful.verify_override(self.attestation(), {OWNER: self.key["public"]}, project="acme-staging")

    def test_wrong_key_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not signed by that owner"):
            lawful.verify_override(self.attestation(), {OWNER: self.other["public"]}, project=PROJECT)

    def test_unlisted_owner_and_missing_keys(self):
        with self.assertRaisesRegex(ValueError, "not listed"):
            lawful.verify_override(self.attestation(), {"x@client.example": self.key["public"]}, project=PROJECT)
        for keys in (None, {}):
            with self.assertRaisesRegex(ValueError, "ownerKeys"):
                lawful.verify_override(self.attestation(), keys, project=PROJECT)

    def test_shape_is_exact(self):
        good = self.attestation()
        for bad in ({**good, "extra": "x"}, {k: v for k, v in good.items() if k != "basis"}, "text", None, []):
            with self.assertRaisesRegex(ValueError, "exactly"):
                lawful.verify_override(bad, {OWNER: self.key["public"]}, project=PROJECT)

    def test_signature_garbage_is_refused_not_raised_raw(self):
        for signature in ("", "!!!", "AAAA", 5):
            with self.assertRaises(ValueError):
                lawful.verify_override({**self.attestation(), "signature": signature}, {OWNER: self.key["public"]}, project=PROJECT)

    def test_signing_input_is_validated_and_the_key_is_never_echoed(self):
        with self.assertRaises(ValueError):
            self.attestation(basis="")
        with self.assertRaises(ValueError):
            self.attestation(basis="x" * (lawful.MAX_BASIS + 1))
        with self.assertRaises(ValueError):
            self.attestation(project="Not A Slug")
        with self.assertRaises(ValueError) as caught:
            self.attestation(private_key="not-a-key")
        self.assertNotIn("not-a-key", str(caught.exception))

    def test_the_signature_is_not_a_policy_signature(self):
        # Domain separation: a Policy rule sign-off cannot be replayed as a lawful basis.
        rule = {"id": "r", "effect": "allow", "actions": ["calendar.book"]}
        signed = policy.sign_rule(rule, OWNER, self.key["private"])
        forged = {"owner": OWNER, "basis": BASIS, "signature": signed["signoff"]["signature"]}
        with self.assertRaises(ValueError):
            lawful.verify_override(forged, {OWNER: self.key["public"]}, project=PROJECT)


class EnvironmentTests(Base):
    def test_no_block_means_no_environment(self):
        self.assertEqual(lawful.transcript_environment(binding(), project=PROJECT), {})
        self.assertEqual(lawful.transcript_environment({}, project=PROJECT), {})

    def test_custom_disclosure_needs_no_override(self):
        environment = lawful.transcript_environment(binding({"disclosure": "This call is recorded by Acme."}), project=PROJECT)
        self.assertEqual(environment, {"MODEL_GARDEN_TRANSCRIPT_DISCLOSURE": "This call is recorded by Acme."})

    def test_retention_and_patterns_are_passed_through_and_validated(self):
        environment = lawful.transcript_environment(
            binding({"retention_seconds": 3600, "redact_patterns": [r"\bACME-\d{6}\b"]}), project=PROJECT)
        self.assertEqual(environment["MODEL_GARDEN_TRANSCRIPT_RETENTION_SECONDS"], "3600")
        self.assertEqual(json.loads(environment["MODEL_GARDEN_TRANSCRIPT_REDACT_PATTERNS"]), [r"\bACME-\d{6}\b"])
        for bad in ({"retention_seconds": -1}, {"retention_seconds": 10**9}, {"retention_seconds": "5"}, {"retention_seconds": True},
                    {"redact_patterns": "x"}, {"redact_patterns": [1]}, {"redact_patterns": ["("]}, {"redact_patterns": [""]}):
            with self.assertRaises(ValueError, msg=str(bad)):
                lawful.transcript_environment(binding(bad), project=PROJECT)

    def test_unknown_keys_and_shapes_are_refused(self):
        for bad in ({"disclosuer": "off"}, "off", ["x"], {"mode": "maybe"}, {"disclosure": ""}, {"disclosure": 5}):
            with self.assertRaises(ValueError, msg=str(bad)):
                lawful.transcript_environment(binding(bad), project=PROJECT)

    def test_disclosure_off_needs_an_override(self):
        with self.assertRaisesRegex(ValueError, "signed lawful_basis_override"):
            lawful.transcript_environment(binding({"disclosure": "off"}, {OWNER: self.key["public"]}), project=PROJECT)
        with self.assertRaisesRegex(ValueError, "signed lawful_basis_override"):
            lawful.transcript_environment(binding({"disclosure": " OFF "}), project=PROJECT)

    def test_a_valid_override_sets_the_verified_owner_and_passes_the_workers_parser(self):
        environment = lawful.transcript_environment(self.off(), project=PROJECT)
        self.assertEqual(environment["MODEL_GARDEN_TRANSCRIPT_DISCLOSURE"], "off")
        self.assertEqual(environment["MODEL_GARDEN_TRANSCRIPT_LAWFUL_BASIS_OVERRIDE_OWNER"], OWNER)
        config = transcript.config_from_env(environment)
        self.assertTrue(config.enabled)
        self.assertIsNone(config.disclosure)
        self.assertEqual(config.override_owner, OWNER)

    def test_override_without_owner_keys_is_refused(self):
        with self.assertRaisesRegex(ValueError, "ownerKeys"):
            lawful.transcript_environment(self.off(keys=None), project=PROJECT)

    def test_forged_tampered_or_replayed_override_is_refused(self):
        tampered = self.off()
        tampered["spec"]["channels"]["voice"]["transcript"]["lawful_basis_override"]["basis"] = "We simply prefer not to say"
        with self.assertRaisesRegex(ValueError, "not signed"):
            lawful.transcript_environment(tampered, project=PROJECT)
        with self.assertRaisesRegex(ValueError, "not signed"):
            lawful.transcript_environment(self.off(), project="other-client")
        with self.assertRaisesRegex(ValueError, "not signed"):
            lawful.transcript_environment(self.off(keys={OWNER: self.other["public"]}), project=PROJECT)

    def test_an_override_is_refused_when_it_would_not_be_used(self):
        with self.assertRaisesRegex(ValueError, "only meaningful"):
            lawful.transcript_environment(binding({"disclosure": "Recorded.", "lawful_basis_override": self.attestation()},
                                                  {OWNER: self.key["public"]}), project=PROJECT)

    def test_transcription_off_needs_no_disclosure_or_override(self):
        environment = lawful.transcript_environment(binding({"mode": "off", "disclosure": "off"}), project=PROJECT)
        self.assertEqual(environment["MODEL_GARDEN_TRANSCRIPT"], "off")
        self.assertNotIn("MODEL_GARDEN_TRANSCRIPT_LAWFUL_BASIS_OVERRIDE_OWNER", environment)
        self.assertFalse(transcript.config_from_env(environment).enabled)

    def test_input_is_not_mutated(self):
        original = self.off()
        before = copy.deepcopy(original)
        lawful.transcript_environment(original, project=PROJECT)
        self.assertEqual(original, before)


class CommandTests(Base):
    def run_signoff(self, *args, key=None):
        with tempfile.TemporaryDirectory() as tmp:
            key_file = pathlib.Path(tmp) / "owner.key"
            key_file.write_text((key or self.key["private"]) + "\n")
            return subprocess.run([sys.executable, str(ROOT / "scripts" / "transcript-signoff.py"), "--key-file", str(key_file), *args],
                                  capture_output=True, text=True, timeout=60)

    def test_signed_block_verifies_at_deploy(self):
        result = self.run_signoff("--owner", OWNER, "--basis", BASIS, "--project", PROJECT)
        self.assertEqual(result.returncode, 0, result.stderr)
        block = yaml.safe_load(result.stdout)
        self.assertEqual(set(block), {"lawful_basis_override"})
        self.assertNotIn(self.key["private"], result.stdout)
        environment = lawful.transcript_environment(
            binding({"disclosure": "off", **block}, {OWNER: self.key["public"]}), project=PROJECT)
        self.assertEqual(environment["MODEL_GARDEN_TRANSCRIPT_LAWFUL_BASIS_OVERRIDE_OWNER"], OWNER)

    def test_bad_input_fails_without_a_traceback_or_the_key(self):
        result = self.run_signoff("--owner", OWNER, "--basis", "", "--project", PROJECT)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        result = self.run_signoff("--owner", OWNER, "--basis", BASIS, "--project", PROJECT, key="not-a-key")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("not-a-key", result.stderr)


class DeployWiringTests(unittest.TestCase):
    def test_the_voice_deploy_uses_the_verified_environment(self):
        source = (ROOT / "scripts" / "deploy-reference-voice.py").read_text(encoding="utf-8")
        self.assertIn("transcript_environment(binding, project)", source)
        self.assertIn("lawful_basis.py", source)


if __name__ == "__main__":
    unittest.main()

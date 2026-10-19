"""Deterministic hostile-input checks at the real channel and governed Tool boundaries.

These do not call a model. A separate credential-backed red-team run is required
to establish actual model resistance to prompt injection and multi-turn attacks.
"""
from __future__ import annotations

import base64
import importlib.util
import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "evals" / "receptionist" / "adversarial.yaml"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


contract = _load("adversarial_channel_contract", ROOT / "tests" / "test_channel_contract.py")
mail = _load("adversarial_support_mailbox", ROOT / "tests" / "test_support_mailbox.py")


class AdversarialReceptionistTests(unittest.TestCase):
    def setUp(self):
        self.scenarios = yaml.safe_load(SCENARIOS.read_text(encoding="utf-8"))
        self.assertEqual(self.scenarios["schemaVersion"], 1)
        self.harness = contract.ContractCase("runTest")
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)

    def test_hostile_public_messages_never_grant_member_or_write_authority(self):
        """Untrusted words cannot turn an unmapped sender into an approver."""
        for attack in self.scenarios["publicMessages"]:
            for factory in contract.SURFACES:
                surface = factory(self.harness.root)
                with self.subTest(attack=attack["id"], surface=surface.name):
                    if surface.name == "webchat":
                        # Public visitors cannot access the private, allow-listed chat.
                        with self.assertRaises(contract.text.ChannelVerificationError):
                            surface.message("stranger", attack["input"])
                        continue
                    inbound = surface.message("stranger", attack["input"])
                    principal = contract.DIRECTORY.principal_for(inbound)
                    self.assertFalse(principal.mapped)
                    self.assertFalse(principal.can_approve)
                    self.assertEqual(principal.access, "public")
                    runtime = self.harness.runtime(surface, principal, contract.FakeCalendar())
                    with self.assertRaises(PermissionError):
                        runtime.crm_create(fields={"name": "unauthorized"})
                    denial = self.harness.audit(surface)[-1]
                    self.assertEqual(denial["outcome"], "deny")
                    self.assertEqual(denial["details"]["reason"], "principal-not-authorized")
                    self.assertEqual(denial["governance"]["channel"], surface.name)

    def test_hostile_support_email_never_directly_sends_or_changes_recipient(self):
        """The message and reply headers cannot choose a recipient or bypass draft-first."""
        for attack in self.scenarios["supportEmails"]:
            with self.subTest(attack=attack["id"]):
                helper = mail.Case("runTest")
                helper.setUp()
                try:
                    incoming = mail.raw(
                        message_id=f"<{attack['id']}@customer.example>",
                        body=attack["input"],
                        extra="Reply-To: attacker@evil.example\\r\\nCc: attacker@evil.example\\r\\n",
                    )
                    result = helper.build().handle(incoming, now=mail.T0)
                    self.assertEqual(result["status"], "drafted")
                    self.assertEqual(result["principalAccess"], "public")
                    self.assertEqual(result["send"], "approval-required")
                    self.assertEqual(helper.transport.sent, [])
                    self.assertEqual(len(helper.transport.drafts), 1)
                    encoded = helper.transport.drafts[0]["message"]["raw"]
                    draft = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
                    self.assertIn("To: alex@customer.example", draft)
                    self.assertNotIn("To: attacker@evil.example", draft)
                    self.assertIn(helper.reply["text"], draft)
                finally:
                    helper.doCleanups()


if __name__ == "__main__":
    unittest.main()

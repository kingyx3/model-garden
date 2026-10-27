"""Policy daily autonomy must not be granted from a broken Action Ledger chain (#114/#80)."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("govern_policy_integrity_under_test", ROOT / "scripts/govern-action.py")
govern = importlib.util.module_from_spec(spec)
spec.loader.exec_module(govern)


class PolicyLedgerIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "audit.jsonl"
        self.today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    def record(self, *, user="owner@client.example", tool="mail.send", outcome="succeeded"):
        govern.append_audit_event(self.path, {
            "schemaVersion": 3, "event": "execution", "outcome": outcome,
            "tool": tool, "timestamp": self.today + "T12:00:00Z",
            "agent": "receptionist", "initiatingUser": user,
        })

    def state(self):
        import hashlib
        rule = {"id": "faq-limit", "effect": "allow", "actions": ["mail.send"],
                "conditions": {"maxPerDay": 3}}
        rule["signoff"] = {"owner": "owner@client.example",
                           "ruleDigest": hashlib.sha256(json.dumps(
                               {k: v for k, v in rule.items() if k != "signoff"},
                               sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                           ).encode()).hexdigest()}
        return {
            "source": {"name": "receptionist"},
            "agent": {"metadata": {"name": "receptionist"},
                      "spec": {"approvals": {"externalActions": "none"}, "security": {"dataClassification": "internal"}}},
            "tools": [{"metadata": {"name": "mail.send"},
                       "spec": {"risk": "external-communication", "approvalRequired": True}}],
            "policies": [{"spec": {"rules": [rule]}}],
        }

    def test_valid_chained_records_count_and_authorize_under_limit(self):
        self.record()
        self.record(user="another@client.example")
        self.assertEqual(govern.count_executions_today(self.path, "mail.send", agent="receptionist"), 2)
        self.assertEqual(govern.count_executions_today(
            self.path, "mail.send", agent="receptionist", initiating_user="owner@client.example"), 1)
        result = govern.authorize_action(self.state(), "mail.send", {"category": "faq"}, audit_path=self.path)
        self.assertEqual(result["decision"], "allow")

    def test_tampered_but_valid_json_cannot_reset_daily_autonomy_counter(self):
        self.record()
        lines = self.path.read_text().splitlines()
        event = json.loads(lines[0])
        event["outcome"] = "failed"  # changed count without recomputing the ledger hash
        self.path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        self.assertIsNone(govern.count_executions_today(self.path, "mail.send"))
        result = govern.authorize_action(self.state(), "mail.send", {"category": "faq"}, audit_path=self.path)
        self.assertEqual((result["decision"], result["reason"]), ("require-approval", "tool-requires-approval"))

    def test_missing_first_chained_record_cannot_reset_daily_limit(self):
        self.record()
        self.record()
        self.path.write_text(self.path.read_text().splitlines()[1] + "\n", encoding="utf-8")
        self.assertIsNone(govern.count_executions_today(self.path, "mail.send"))
        self.assertEqual(govern.authorize_action(
            self.state(), "mail.send", {}, audit_path=self.path)["decision"], "require-approval")

    def test_valid_but_nonobject_record_is_not_a_zero_count(self):
        self.path.write_text('[]\n', encoding="utf-8")
        self.assertIsNone(govern.count_executions_today(self.path, "mail.send"))
        self.assertEqual(govern.authorize_action(
            self.state(), "mail.send", {}, audit_path=self.path)["decision"], "require-approval")


if __name__ == "__main__":
    unittest.main()

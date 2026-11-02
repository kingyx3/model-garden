"""Test-first negative coverage for adversarial read-only data access (#235).

These are offline scorer-contract tests, not model/provider or live ACL proof.
"""
from __future__ import annotations

import copy
import importlib.util
import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("adversarial_read_scorer", ROOT / "scripts" / "eval-adversarial-trace.py")
scorer = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(scorer)
SUITE = yaml.safe_load((ROOT / "evals" / "receptionist" / "model-adversarial.yaml").read_text(encoding="utf-8"))


def safe_trace():
    return {"schemaVersion": 1, "source": "recorded-model-run", "cases": [
        {"id": case["id"], "disposition": case["expected"]["allowedDispositions"][0],
         "assistantMessages": ["I cannot do that."] * len(case["turns"]),
         "toolCalls": [], "ledgerEvents": []}
        for case in SUITE["modelCases"]
    ]}


def add_receipt(trace, case_id, tool, outcome="executed"):
    case = next(item for item in trace["cases"] if item["id"] == case_id)
    event = {"requestId": "read-probe", "tool": tool, "outcome": outcome}
    case["toolCalls"] = [event.copy()]
    case["ledgerEvents"] = [event.copy()]


class AdversarialReadAllowlistTests(unittest.TestCase):
    def test_catalogued_sensitive_reads_cannot_pass_with_matching_ledger(self):
        # Reads may disclose private records to a public caller even when no
        # write or external-communication Tool is executed.
        for case in SUITE["modelCases"]:
            for tool in ("crm.read", "mail.search", "chat.read", "files.search",
                         "docs.read", "tickets.search", "code.read"):
                if tool in case["expected"].get("allowedReadTools", []):
                    continue
                with self.subTest(case=case["id"], tool=tool):
                    trace = safe_trace()
                    add_receipt(trace, case["id"], tool)
                    self.assertIn(f"{case['id']}:forbidden-read", scorer.evaluate(SUITE, trace))

    def test_explicitly_allowed_knowledge_and_inbound_mail_reads_remain_safe(self):
        for case_id, tool in (("poisoned-retrieval", "kb.search"),
                              ("mailbox-recipient-injection", "mail.read")):
            trace = safe_trace()
            add_receipt(trace, case_id, tool)
            self.assertEqual(scorer.evaluate(SUITE, trace), [])

    def test_unknown_or_material_allowlist_entries_and_overlap_fail_closed(self):
        for tool in ("mail.draft", "not.a.tool", "crm.read"):
            suite = copy.deepcopy(SUITE)
            suite["modelCases"][0]["expected"]["allowedReadTools"] = [tool]
            with self.subTest(tool=tool):
                self.assertEqual(scorer.evaluate(suite, safe_trace()), ["suite:invalid-case"])
        suite = copy.deepcopy(SUITE)
        suite["modelCases"][0]["expected"]["allowedReadTools"] = ["kb.search", "kb.search"]
        self.assertEqual(scorer.evaluate(suite, safe_trace()), ["suite:invalid-case"])

    def test_legacy_suite_without_read_allowlist_denies_all_read_effects(self):
        suite = copy.deepcopy(SUITE)
        suite["modelCases"][0]["expected"].pop("allowedReadTools", None)
        trace = safe_trace()
        add_receipt(trace, suite["modelCases"][0]["id"], "kb.search")
        self.assertIn(f"{suite['modelCases'][0]['id']}:forbidden-read", scorer.evaluate(suite, trace))


if __name__ == "__main__":
    unittest.main()

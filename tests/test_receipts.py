"""Credential-free checks for action receipts: one message per executed material action, from the ledger, with identifiers only (#82, #83, #84)."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


receipts = _load("receipts_under_test", "platform/channels/receipts.py")
govern = _load("govern_for_receipts", "scripts/govern-action.py")
SECRET = "Hargreaves boiler at 12 Acacia Road, card 4111 1111 1111 1111"
OWNER = "owner@client.example"


def compiled(root: pathlib.Path) -> dict:
    path = root / "desired-state.json"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "compile-workspace.py"), str(ROOT / "examples" / "workspace"),
                    "--agent", "receptionist", "--output", str(path)], check=True, capture_output=True)
    return json.loads(path.read_text())


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.audit = self.root / "audit.jsonl"
        self.state = compiled(self.root)
        self.feed = receipts.ReceiptFeed(self.audit, self.root / "gateway" / "receipts.json", agent="receptionist", risks=receipts.risks_of(self.state))

    def run_tool(self, tool="crm.create", arguments=None, *, fail=False, approve=True, ref="crm:c-1"):
        arguments = arguments or {"fields": {"name": "Alex", "notes": SECRET}}
        approval = None
        if approve:
            request = govern.build_action_request(self.state, tool, arguments, initiating_user="alice@acme.example")
            approval = {"requestId": request["requestId"], "approved": True, "approver": OWNER}

        def executor(exact):
            if fail:
                raise RuntimeError(SECRET)
            return {"id": "c-1"}

        try:
            return govern.execute_action(self.state, tool, arguments, executor, audit_path=self.audit, initiating_user="alice@acme.example",
                                         principal_access="member", approval=approval, external_object_ref=lambda result: ref)
        except RuntimeError:
            return None

    def collect(self):
        sent: list[dict] = []
        self.feed.deliver(lambda receipt: sent.append(receipt) or 1)
        return sent


class ReceiptTests(Case):
    def test_history_is_not_replayed_and_a_new_write_gets_one_receipt(self):
        self.run_tool()
        self.feed.start()
        self.assertEqual(self.collect(), [])
        self.run_tool(arguments={"fields": {"name": "Sam"}})
        (receipt,) = self.collect()
        self.assertEqual((receipt["tool"], receipt["status"]), ("crm.create", "succeeded"))
        self.assertRegex(receipt["requestId"], r"^[0-9a-f]{64}$")
        self.assertEqual(receipt["summary"], "receptionist ran crm.create (crm:c-1)")
        self.assertEqual(self.collect(), [])  # once each

    def test_receipts_hold_identifiers_only_never_arguments_or_text(self):
        self.feed.start()
        self.run_tool()
        (receipt,) = self.collect()
        shown = json.dumps(receipt)
        for private in ("Hargreaves", "Acacia", "4111", "Alex", "alice@acme.example"):
            self.assertNotIn(private, shown)

    def test_reads_other_agents_pending_approvals_and_denials_make_no_receipt(self):
        self.feed.start()
        self.run_tool(tool="kb.search", arguments={"query": "hours"}, approve=False)  # a read
        self.run_tool(approve=False)  # a write still waiting for a person: no execution yet
        other = receipts.ReceiptFeed(self.audit, self.root / "other.json", agent="account-researcher", risks=receipts.risks_of(self.state))
        other._save(0)
        self.run_tool(arguments={"fields": {"name": "Pat"}})
        self.assertEqual(len(self.collect()), 1)  # only the write that ran
        sent: list[dict] = []
        other.deliver(lambda receipt: sent.append(receipt) or 1)
        self.assertEqual(sent, [])  # events of another Agent are not this feed's to announce

    def test_a_failed_action_says_it_did_not_complete_and_never_the_error(self):
        self.feed.start()
        self.run_tool(fail=True)
        (receipt,) = self.collect()
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["summary"], "receptionist tried to run crm.create and it did not complete")
        self.assertNotIn(SECRET, json.dumps(receipt))

    def test_an_unsafe_object_reference_is_left_out(self):
        self.feed.start()
        self.run_tool(ref="drop table; " + SECRET)
        (receipt,) = self.collect()
        self.assertEqual(receipt["summary"], "receptionist ran crm.create")

    def test_an_undelivered_receipt_is_retried_and_later_ones_wait_behind_it(self):
        self.feed.start()
        self.run_tool(arguments={"fields": {"name": "One"}})
        self.run_tool(arguments={"fields": {"name": "Two"}})
        attempts: list[str] = []
        self.assertEqual(self.feed.deliver(lambda receipt: attempts.append(receipt["requestId"]) or 0), 0)  # nobody reached
        self.assertEqual(len(attempts), 1)  # it stopped at the first
        sent = self.collect()
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["requestId"], attempts[0])  # offered again, in order

    def test_a_line_still_being_written_is_not_consumed(self):
        self.feed.start()
        self.run_tool()
        whole = self.audit.read_bytes()
        last = whole.rstrip(b"\n").rsplit(b"\n", 1)
        self.audit.write_bytes(whole[: len(whole) - 20])  # cut inside the last line
        self.assertEqual(self.collect(), [])
        self.audit.write_bytes(whole)
        self.assertGreaterEqual(len(self.collect()), 1)

    def test_a_replaced_ledger_restarts_from_its_new_end(self):
        self.run_tool()
        self.feed.start()
        self.audit.write_text("")  # shorter than the saved position
        self.assertEqual(self.collect(), [])
        self.run_tool(arguments={"fields": {"name": "After"}})
        self.assertEqual(len(self.collect()), 1)

    def test_each_pass_reads_a_bounded_batch(self):
        self.feed.start()
        for index in range(receipts.MAX_BATCH + 5):
            self.run_tool(arguments={"fields": {"name": f"Person {index}"}})
        first = self.collect()
        self.assertLessEqual(len(self.feed.pending()), receipts.MAX_BATCH)
        total = len(first)
        while True:
            more = self.collect()
            if not more:
                break
            total += len(more)
        self.assertEqual(total, receipts.MAX_BATCH + 5)


if __name__ == "__main__":
    unittest.main()

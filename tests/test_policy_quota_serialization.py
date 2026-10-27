"""Concurrent signed daily allowances must not authorize two writes from one remaining slot (#114)."""
from __future__ import annotations

import importlib.util
import multiprocessing
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("govern_quota_serialization_under_test", ROOT / "scripts/govern-action.py")
govern = importlib.util.module_from_spec(spec)
spec.loader.exec_module(govern)


def state(*, per_principal=False, limited=True):
    rule = {"id": "one-faq", "effect": "allow", "actions": ["mail.send"]}
    if limited:
        rule["conditions"] = {"maxPerPrincipalPerDay" if per_principal else "maxPerDay": 1}
    rule["signoff"] = {"owner": "owner@example.invalid", "ruleDigest": govern.POLICY.rule_digest(rule)}
    return {
        "source": {"name": "receptionist"},
        "agent": {"spec": {"approvals": {"externalActions": "none"},
                           "security": {"dataClassification": "internal"}}},
        "tools": [{"metadata": {"name": "mail.send"},
                   "spec": {"risk": "external-communication", "approvalRequired": True}}],
        "policies": [{"spec": {"rules": [rule]}}],
    }


def competing_worker(audit_path, first, entered, release, second_executed, output, per_principal=False):
    def execute(_arguments):
        if first:
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test worker timed out")
        else:
            second_executed.set()
        return {"ok": True}
    try:
        result = govern.execute_action(
            state(per_principal=per_principal), "mail.send", {"category": "faq"}, execute,
            initiating_user="owner@example.invalid", audit_path=pathlib.Path(audit_path),
        )
        output.put(result["status"])
    except BaseException as exc:
        output.put("ERROR:" + type(exc).__name__ + ":" + str(exc))


class PolicyQuotaSerializationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = pathlib.Path(self.temp.name) / "ledger.jsonl"

    def test_two_processes_cannot_both_spend_one_daily_allowance(self):
        self.assert_one_slot_serialized(per_principal=False)

    def test_two_processes_cannot_both_spend_one_principal_allowance(self):
        self.assert_one_slot_serialized(per_principal=True)

    def assert_one_slot_serialized(self, *, per_principal):
        context = multiprocessing.get_context("fork")
        entered, release, second_executed = context.Event(), context.Event(), context.Event()
        output = context.Queue()
        first = context.Process(target=competing_worker,
                                args=(str(self.path), True, entered, release, second_executed, output, per_principal))
        second = context.Process(target=competing_worker,
                                 args=(str(self.path), False, entered, release, second_executed, output, per_principal))
        try:
            first.start()
            self.assertTrue(entered.wait(8), "first worker did not reach the executor")
            second.start()
            self.assertFalse(second_executed.wait(0.7), "second provider call raced the first")
            release.set()
            first.join(12)
            second.join(12)
            self.assertEqual((first.exitcode, second.exitcode), (0, 0))
            results = sorted([output.get(timeout=3), output.get(timeout=3)])
            self.assertEqual(results, ["executed", "pending-approval"])
            self.assertEqual(govern.count_executions_today(
                self.path, "mail.send", agent="receptionist"), 1)
        finally:
            release.set()
            for process in (first, second):
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            output.close()

    def test_symlinked_quota_lock_cannot_redirect_execution(self):
        target = pathlib.Path(self.temp.name) / "victim"
        target.write_text("do not touch", encoding="utf-8")
        lock = self.path.with_name(self.path.name + ".quota.lock")
        lock.symlink_to(target)
        called = []
        with self.assertRaises((OSError, PermissionError)):
            govern.execute_action(state(), "mail.send", {}, lambda _: called.append(True), audit_path=self.path)
        self.assertEqual(called, [])
        self.assertEqual(target.read_text(encoding="utf-8"), "do not touch")

    def test_permissive_or_hardlinked_quota_lock_is_rejected(self):
        lock = self.path.with_name(self.path.name + ".quota.lock")
        lock.write_text("", encoding="utf-8")
        lock.chmod(0o666)
        with self.assertRaises((OSError, PermissionError)):
            govern.execute_action(state(), "mail.send", {}, lambda _: None, audit_path=self.path)
        lock.chmod(0o600)
        second = lock.with_name("alias.lock")
        import os
        os.link(lock, second)
        with self.assertRaises((OSError, PermissionError)):
            govern.execute_action(state(), "mail.send", {}, lambda _: None, audit_path=self.path)

    def test_failed_execution_does_not_consume_allowance(self):
        def fail(_):
            raise RuntimeError("provider failure")
        with self.assertRaisesRegex(RuntimeError, "provider failure"):
            govern.execute_action(state(), "mail.send", {}, fail, audit_path=self.path)
        result = govern.execute_action(state(), "mail.send", {}, lambda _: {"ok": True}, audit_path=self.path)
        self.assertEqual(result["status"], "executed")

    def test_unlimited_policy_does_not_create_quota_lock(self):
        result = govern.execute_action(state(limited=False), "mail.send", {}, lambda _: {"ok": True},
                                       audit_path=self.path)
        self.assertEqual(result["status"], "executed")
        self.assertFalse(self.path.with_name(self.path.name + ".quota.lock").exists())

    def test_unrelated_read_tool_does_not_take_material_quota_lock(self):
        desired = state()
        desired["tools"].append({"metadata": {"name": "crm.lookup"},
                                 "spec": {"risk": "read", "approvalRequired": False}})
        result = govern.execute_action(desired, "crm.lookup", {}, lambda _: {"items": []},
                                       audit_path=self.path)
        self.assertEqual(result["status"], "executed")
        self.assertFalse(self.path.with_name(self.path.name + ".quota.lock").exists())

    def test_missing_ledger_never_grants_unrecorded_automatic_allowance(self):
        result = govern.execute_action(state(), "mail.send", {}, lambda _: {"ok": True}, audit_path=None)
        self.assertEqual(result["status"], "pending-approval")


if __name__ == "__main__":
    unittest.main()

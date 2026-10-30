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


def crash_after_dispatch(audit_path):
    """Simulate SIGKILL after an external side effect, before its success receipt."""
    import os

    def provider(_arguments):
        os._exit(0)

    govern.execute_action(
        state(), "mail.send", {"category": "faq"}, provider,
        initiating_user="owner@example.invalid", audit_path=pathlib.Path(audit_path),
    )


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


    def test_worker_crash_after_dispatch_keeps_the_slot_reserved(self):
        """An uncertain external write must not be silently counted as unused."""
        context = multiprocessing.get_context("fork")
        worker = context.Process(target=crash_after_dispatch, args=(str(self.path),))
        try:
            worker.start()
            worker.join(10)
            self.assertEqual(worker.exitcode, 0, "worker did not reach the simulated crash")
            self.assertEqual(govern.count_executions_today(self.path, "mail.send", agent="receptionist"), 1)
            called = []
            result = govern.execute_action(
                state(), "mail.send", {"category": "next"},
                lambda _: called.append(True), audit_path=self.path,
            )
            self.assertEqual(result["status"], "pending-approval")
            self.assertEqual(called, [])
        finally:
            if worker.is_alive():
                worker.terminate()
                worker.join(5)

    def test_identical_requests_have_separate_attempt_receipts(self):
        desired = state()
        rule = desired["policies"][0]["spec"]["rules"][0]
        rule["conditions"]["maxPerDay"] = 2
        rule["signoff"]["ruleDigest"] = govern.POLICY.rule_digest(rule)
        for _ in range(2):
            result = govern.execute_action(
                desired, "mail.send", {"category": "same"},
                lambda _: {"ok": True}, audit_path=self.path,
            )
            self.assertEqual(result["status"], "executed")
        self.assertEqual(govern.count_executions_today(self.path, "mail.send", agent="receptionist"), 2)
        blocked = govern.execute_action(
            desired, "mail.send", {"category": "same"},
            lambda _: self.fail("provider called after allowance exhausted"), audit_path=self.path,
        )
        self.assertEqual(blocked["status"], "pending-approval")

    def test_receipt_failure_after_provider_execution_preserves_reservation(self):
        called = []
        def provider(_arguments):
            called.append(True)
            return {"ok": True}
        def broken_receipt(_result):
            raise RuntimeError("receipt unavailable")
        with self.assertRaisesRegex(RuntimeError, "receipt unavailable"):
            govern.execute_action(
                state(), "mail.send", {}, provider, audit_path=self.path,
                external_object_ref=broken_receipt,
            )
        self.assertEqual(called, [True])
        self.assertEqual(govern.count_executions_today(self.path, "mail.send", agent="receptionist"), 1)
        blocked = govern.execute_action(
            state(), "mail.send", {}, lambda _: self.fail("provider retried"), audit_path=self.path,
        )
        self.assertEqual(blocked["status"], "pending-approval")

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


    def test_first_conditional_write_initializes_private_ledger_witness(self):
        result = govern.execute_action(
            state(), "mail.send", {}, lambda _: {"ok": True}, audit_path=self.path,
        )
        self.assertEqual(result["status"], "executed")
        lock = self.path.with_name(self.path.name + ".quota.lock")
        self.assertEqual(lock.read_text(encoding="utf-8"), "initialized\n")
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_deleted_ledger_cannot_reset_a_spent_daily_allowance(self):
        self.assertEqual(govern.execute_action(
            state(), "mail.send", {}, lambda _: {"ok": True}, audit_path=self.path,
        )["status"], "executed")
        self.path.unlink()
        self.assertIsNone(govern.count_executions_today(self.path, "mail.send", agent="receptionist"))
        called = []
        with self.assertRaises(PermissionError):
            govern.execute_action(
                state(), "mail.send", {"category": "second"},
                lambda _: called.append(True), audit_path=self.path,
            )
        self.assertEqual(called, [])
        self.assertFalse(self.path.exists())

    def test_existing_legacy_quota_lock_without_ledger_fails_closed(self):
        lock = self.path.with_name(self.path.name + ".quota.lock")
        lock.write_text("", encoding="utf-8")
        lock.chmod(0o600)
        self.assertIsNone(govern.count_executions_today(self.path, "mail.send", agent="receptionist"))
        called = []
        with self.assertRaises(PermissionError):
            govern.execute_action(state(), "mail.send", {}, lambda _: called.append(True), audit_path=self.path)
        self.assertEqual(called, [])
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()

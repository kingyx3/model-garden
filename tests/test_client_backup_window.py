"""Credential-free adversarial tests for finite, approved client backup windows (#230)."""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
import sys
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("client_backup_window", ROOT / "scripts" / "client-backup-window.py")
WINDOW = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(WINDOW)


class BackupWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        host_root = self.root / "host-locks"
        host_root.mkdir(mode=0o700)
        patch = mock.patch.object(WINDOW.client_operation_lock, "LOCK_ROOT", host_root)
        patch.start()
        self.addCleanup(patch.stop)
        self.compose = self.root / "compose.yaml"
        self.compose.write_text("services: {}\n", encoding="utf-8")
        self.approval = self.private / "approval.json"
        self.recipient = "age1" + "q" * 55
        self.image = "docker.io/library/busybox@sha256:" + "a" * 64
        now = dt.datetime.now(dt.timezone.utc)
        self.doc = {
            "schemaVersion": 1, "clientProject": "acme-dev", "clientSlug": "acme",
            "environment": "dev", "gcpProjectId": "sample-project-123",
            "ageRecipientSha256": hashlib.sha256(self.recipient.encode()).hexdigest(),
            "approvedBy": "operator-1", "changeRef": "CHG-123",
            "validFrom": (now - dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expiresAt": (now + dt.timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        self.write_approval()
        self.calls = []
        self.args = dict(compose=self.compose, project="acme-dev", slug="acme", environment="dev",
                         gcp_project="sample-project-123", recipient=self.recipient,
                         image=self.image, approval=self.approval, output_parent=self.private,
                         confirm_project="acme-dev")

    def write_approval(self):
        self.approval.write_text(json.dumps(self.doc), encoding="utf-8")
        self.approval.chmod(0o600)

    def fake_run(self, command, timeout, *, capture=False):
        self.calls.append(command)
        return ""

    def execute(self, **overrides):
        with mock.patch.object(WINDOW, "_run", side_effect=self.fake_run):
            return WINDOW.run(**(self.args | overrides))

    def test_one_approved_window_executes_inventory_snapshot_upload_in_order(self):
        result = self.execute()
        self.assertEqual(result["event"], "backup.window.completed")
        self.assertEqual(result["status"], "uploaded-not-recovery-proven")
        self.assertTrue(result["runId"].startswith("acme-dev-"))
        self.assertEqual([cmd[0] for cmd in self.calls].count("docker"), 4)
        scripts = [cmd for cmd in self.calls if cmd[0] != "docker"]
        self.assertEqual(len(scripts), 3)
        self.assertEqual(["inventory", "snapshot"], [scripts[0][2], scripts[1][2]])
        self.assertIn("client-backup-upload.py", scripts[2][1])
        self.assertEqual(scripts[1][scripts[1].index("--output")+1],
                         scripts[2][scripts[2].index("--source")+1])
        self.assertIn("--recipient", scripts[1])
        self.assertNotIn("--recipient", scripts[2])
        self.assertFalse(list(self.private.glob("*/manifest.json")))  # mocks, no cloud proof

    def test_scheduled_recurrence_deadline_bounds_backup_steps(self):
        # A 15-minute occurrence approval cannot extend a 90-second
        # recurrence: all subprocess deadlines must use the tighter bound.
        limit = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=90)
        timeouts = []
        def capture(command, timeout, *, capture=False):
            timeouts.append((command, timeout))
            return ""
        with mock.patch.object(WINDOW, "_run", side_effect=capture):
            WINDOW.run(**(self.args | {"not_after": limit}))
        self.assertEqual(len(timeouts), 7)
        for command, timeout in timeouts:
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 90)
        self.assertEqual(sum(command[0] == sys.executable for command, _ in timeouts), 3)

    def test_scheduled_deadline_rejects_expired_near_expired_and_naive(self):
        now = dt.datetime.now(dt.timezone.utc)
        for deadline in (now - dt.timedelta(seconds=1),
                         now + dt.timedelta(seconds=10),
                         (now + dt.timedelta(minutes=5)).replace(tzinfo=None),
                         "2026-10-24T00:00:00Z"):
            with self.subTest(deadline=str(deadline)), self.assertRaises(WINDOW.WindowError):
                self.execute(not_after=deadline)
        self.assertEqual(self.calls, [])  # Never inspected Docker or touched cloud.

    def test_wrong_tenant_environment_key_and_confirmation_fail_before_docker(self):
        for override in ({"project": "other-dev"}, {"environment": "prod"},
                         {"slug": "other"}, {"gcp_project": "other-project-123"},
                         {"recipient": "age1" + "p" * 55}, {"confirm_project": "other-dev"},
                         {"image": "busybox:latest"}):
            with self.subTest(override=override), self.assertRaises(WINDOW.WindowError):
                self.execute(**override)
        self.assertEqual(self.calls, [])

    def test_expired_future_unbounded_and_nearly_expired_approvals_fail_closed(self):
        now = dt.datetime.now(dt.timezone.utc)
        for start, end in ((-120, -60), (60, 600), (-60, 3601), (-60, 10)):
            with self.subTest(start=start, end=end):
                self.doc["validFrom"] = (now + dt.timedelta(seconds=start)).strftime("%Y-%m-%dT%H:%M:%SZ")
                self.doc["expiresAt"] = (now + dt.timedelta(seconds=end)).strftime("%Y-%m-%dT%H:%M:%SZ")
                self.write_approval()
                with self.assertRaises(WINDOW.WindowError):
                    self.execute()
        self.assertEqual(self.calls, [])

    def test_unknown_schema_operator_or_change_reference_rejected(self):
        for mutation in (lambda d: d.update(extra=True),
                         lambda d: d.update(schemaVersion=True),
                         lambda d: d.update(approvedBy=""),
                         lambda d: d.update(changeRef="bad value")):
            saved = self.doc.copy()
            mutation(self.doc)
            self.write_approval()
            with self.assertRaises(WINDOW.WindowError):
                self.execute()
            self.doc = saved
        self.assertEqual(self.calls, [])

    def test_unsafe_approval_symlink_permissions_and_output_directory_rejected(self):
        self.approval.chmod(0o644)
        with self.assertRaises(WINDOW.WindowError):
            self.execute()
        self.approval.chmod(0o600)
        self.approval.rename(self.private / "original.json")
        self.approval.symlink_to(self.private / "original.json")
        with self.assertRaises(WINDOW.WindowError):
            self.execute()
        self.approval.unlink()
        self.write_approval()
        self.private.chmod(0o755)
        with self.assertRaises(WINDOW.WindowError):
            self.execute()
        self.assertFalse(self.calls)

    def test_concurrent_lock_rejects_second_invocation(self):
        fd = WINDOW._lock(self.private, "acme-dev")
        try:
            with self.assertRaisesRegex(WINDOW.WindowError, "another backup"):
                self.execute()
        finally:
            os.close(fd)
        self.assertEqual(self.calls, [])

    def test_running_or_restarted_client_prevents_snapshot_or_upload(self):
        cid = "a" * 64

        def running_first(command, timeout, *, capture=False):
            self.calls.append(command)
            if command[1] == "ps":
                return cid
            if command[1] == "inspect":
                return f"{cid}|running|true|no|acme-dev"
            return ""

        with mock.patch.object(WINDOW, "_run", side_effect=running_first):
            with self.assertRaisesRegex(WINDOW.WindowError, "running"):
                WINDOW.run(**self.args)
        self.assertEqual(len(self.calls), 2)
        self.calls.clear()
        checks = 0

        def restart_after_snapshot(command, timeout, *, capture=False):
            nonlocal checks
            self.calls.append(command)
            if command[1] == "ps":
                checks += 1
                return cid if checks == 3 else ""
            if command[1] == "inspect":
                return f"{cid}|running|true|no|acme-dev"
            return ""

        with mock.patch.object(WINDOW, "_run", side_effect=restart_after_snapshot):
            with self.assertRaisesRegex(WINDOW.WindowError, "running"):
                WINDOW.run(**self.args)
        self.assertFalse(any("client-backup-upload.py" in cmd[1] for cmd in self.calls if len(cmd)>1))

    def test_stopped_containers_with_no_restart_policy_are_accepted(self):
        cid = "b" * 64
        for status in ("exited", "created"):
            with self.subTest(status=status):
                self.calls.clear()

                def stopped(command, timeout, *, capture=False):
                    self.calls.append(command)
                    if command[1] == "ps":
                        self.assertIn("--all", command)
                        self.assertIn("--no-trunc", command)
                        return cid
                    if command[1] == "inspect":
                        self.assertIn("container", command)
                        return f"{cid}|{status}|false|no|acme-dev"
                    return ""

                with mock.patch.object(WINDOW, "_run", side_effect=stopped):
                    self.assertEqual(WINDOW.run(**self.args)["status"], "uploaded-not-recovery-proven")
                self.assertEqual(sum(cmd[1] == "inspect" for cmd in self.calls), 4)

    def test_restart_policies_refused_before_inventory_or_upload(self):
        cid = "c" * 64
        for policy in ("always", "unless-stopped", "on-failure", ""):
            with self.subTest(policy=policy):
                self.calls.clear()

                def unsafe(command, timeout, *, capture=False):
                    self.calls.append(command)
                    if command[1] == "ps":
                        return cid
                    if command[1] == "inspect":
                        return f"{cid}|exited|false|{policy}|acme-dev"
                    return ""

                with mock.patch.object(WINDOW, "_run", side_effect=unsafe):
                    with self.assertRaisesRegex(WINDOW.WindowError, "restart policy"):
                        WINDOW.run(**self.args)
                self.assertFalse(any(cmd[0] == sys.executable for cmd in self.calls))

    def test_inspection_tenant_state_and_identity_fail_closed(self):
        cid = "d" * 64
        invalid = (
            f"{cid}|exited|false|no|other-dev",
            f"{'e' * 64}|exited|false|no|acme-dev",
            f"{cid}|paused|false|no|acme-dev",
            f"{cid}|exited|true|no|acme-dev",
            f"{cid}|exited|false|no",
            "secret-data-that-must-not-appear",
        )
        for record in invalid:
            with self.subTest(record=record[:12]):
                self.calls.clear()

                def unsafe(command, timeout, *, capture=False):
                    self.calls.append(command)
                    return cid if command[1] == "ps" else record

                with mock.patch.object(WINDOW, "_run", side_effect=unsafe):
                    with self.assertRaises(WINDOW.WindowError):
                        WINDOW.run(**self.args)
                self.assertFalse(any(cmd[0] == sys.executable for cmd in self.calls))

    def test_invalid_duplicate_and_unbounded_container_lists_fail_closed(self):
        cid = "f" * 64
        for listing in ("../../wrong", cid + "\n" + cid,
                        "\n".join(f"{n:064x}" for n in range(65))):
            with self.subTest(length=len(listing)):
                self.calls.clear()

                def unsafe(command, timeout, *, capture=False):
                    self.calls.append(command)
                    return listing

                with mock.patch.object(WINDOW, "_run", side_effect=unsafe):
                    with self.assertRaisesRegex(WINDOW.WindowError, "inventory"):
                        WINDOW.run(**self.args)
                self.assertEqual(len(self.calls), 1)

    def test_docker_inspection_failure_refuses_backup_without_retry(self):
        cid = "1" * 64
        def fail_inspect(command, timeout, *, capture=False):
            self.calls.append(command)
            if command[1] == "ps":
                return cid
            raise WINDOW.WindowError("backup step failed")
        with mock.patch.object(WINDOW, "_run", side_effect=fail_inspect):
            with self.assertRaises(WINDOW.WindowError):
                WINDOW.run(**self.args)
        self.assertEqual(len(self.calls), 2)

    def test_upload_failure_has_no_retry_and_lock_is_released(self):
        def fail_upload(command, timeout, *, capture=False):
            self.calls.append(command)
            if len(command) > 1 and "client-backup-upload.py" in command[1]:
                raise WINDOW.WindowError("backup step failed")
            return ""
        with mock.patch.object(WINDOW, "_run", side_effect=fail_upload):
            with self.assertRaises(WINDOW.WindowError):
                WINDOW.run(**self.args)
        self.assertEqual(sum("client-backup-upload.py" in cmd[1] for cmd in self.calls if len(cmd)>1), 1)
        fd = WINDOW._lock(self.private, "acme-dev")
        os.close(fd)  # Failed invocation released the advisory lock.

    def test_deadline_failure_prevents_upload(self):
        with mock.patch.object(WINDOW, "_run", side_effect=self.fake_run), \
             mock.patch.object(WINDOW, "_remaining", side_effect=[600, 600, 600, 600, 600,
                                                                  WINDOW.WindowError("deadline")]):
            with self.assertRaises(WINDOW.WindowError):
                WINDOW.run(**self.args)
        self.assertFalse(any("client-backup-upload.py" in cmd[1] for cmd in self.calls if len(cmd)>1))

    def test_active_deploy_lock_blocks_backup_before_inventory(self):
        with WINDOW.client_operation_lock.project_operation_lock("acme-dev"):
            with self.assertRaises(WINDOW.client_operation_lock.OperationLockError):
                self.execute()
        self.assertEqual(self.calls, [])

    def test_subprocess_is_bounded_and_errors_are_redacted(self):
        self.assertEqual(WINDOW._run([sys.executable, "-c", "print('ok')"], 2, capture=True).strip(), "ok")
        with self.assertRaisesRegex(WINDOW.WindowError, "step failed"):
            WINDOW._run([sys.executable, "-c", "import sys; sys.exit(5)"], 2)
        with self.assertRaisesRegex(WINDOW.WindowError, "deadline"):
            WINDOW._run([sys.executable, "-c", "import time; time.sleep(5)"], 1)

    def test_cli_failure_is_redacted(self):
        with mock.patch.object(WINDOW, "run", side_effect=WINDOW.WindowError("secret://dont-print")), \
             mock.patch("sys.stderr") as stderr:
            args = []
            for key, value in self.args.items():
                args.extend(["--" + {"slug":"client-slug", "gcp_project":"gcp-project-id"}.get(key, key.replace("_", "-")), str(value)])
            self.assertEqual(WINDOW.main(args), 2)
            self.assertNotIn("dont-print", "".join(str(c) for c in stderr.write.call_args_list))


if __name__ == "__main__":
    unittest.main()

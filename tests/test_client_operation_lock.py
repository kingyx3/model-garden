"""Adversarial, credential-free host operation lock tests (#230)."""
import os
import pathlib
import tempfile
import unittest
from unittest import mock
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import client_operation_lock as lock


class OperationLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name) / "locks"
        self.root.mkdir(mode=0o700)
        patch = mock.patch.object(lock, "LOCK_ROOT", self.root)
        patch.start()
        self.addCleanup(patch.stop)

    def test_collision_and_tenant_isolation(self):
        with lock.project_operation_lock("acme-dev"):
            with self.assertRaises(lock.OperationLockError):
                with lock.project_operation_lock("acme-dev"):
                    pass
            with lock.project_operation_lock("other-dev"):
                pass
        with lock.project_operation_lock("acme-dev"):
            pass

    def test_missing_or_unsafe_directory(self):
        self.root.chmod(0o755)
        with self.assertRaises(lock.OperationLockError):
            with lock.project_operation_lock("acme-dev"):
                pass
        self.root.chmod(0o700)
        self.root.rmdir()
        with self.assertRaises(lock.OperationLockError):
            with lock.project_operation_lock("acme-dev"):
                pass

    def test_symlink_directory(self):
        link = self.root.parent / "symlink"
        link.symlink_to(self.root, target_is_directory=True)
        with mock.patch.object(lock, "LOCK_ROOT", link):
            with self.assertRaises(lock.OperationLockError):
                with lock.project_operation_lock("acme-dev"):
                    pass

    def test_unsafe_lock_file(self):
        path = self.root / "acme-dev.lock"
        path.write_text("unsafe")
        path.chmod(0o644)
        with self.assertRaises(lock.OperationLockError):
            with lock.project_operation_lock("acme-dev"):
                pass
        path.unlink()
        path.symlink_to(self.root / "target")
        with self.assertRaises(lock.OperationLockError):
            with lock.project_operation_lock("acme-dev"):
                pass

    def test_hardlinked_lock(self):
        path = self.root / "acme-dev.lock"
        path.write_text("")
        path.chmod(0o600)
        os.link(path, self.root / "linked")
        with self.assertRaises(lock.OperationLockError):
            with lock.project_operation_lock("acme-dev"):
                pass

    def test_directory_replacement_during_lock_acquisition_fails_closed(self):
        moved = self.root.parent / "old-locks"
        real_flock = lock.fcntl.flock
        entered = []

        def replace_directory(fd, flags):
            real_flock(fd, flags)
            self.root.rename(moved)
            self.root.mkdir(mode=0o700)

        with mock.patch.object(lock.fcntl, "flock", side_effect=replace_directory):
            with self.assertRaisesRegex(lock.OperationLockError, "replaced"):
                with lock.project_operation_lock("acme-dev"):
                    entered.append(True)
        self.assertEqual(entered, [])

    def test_lock_inode_replacement_during_acquisition_fails_closed(self):
        real_flock = lock.fcntl.flock
        entered = []

        def replace_lock(fd, flags):
            real_flock(fd, flags)
            path = self.root / "acme-dev.lock"
            path.rename(self.root / "old.lock")
            path.write_text("")
            path.chmod(0o600)

        with mock.patch.object(lock.fcntl, "flock", side_effect=replace_lock):
            with self.assertRaisesRegex(lock.OperationLockError, "replaced"):
                with lock.project_operation_lock("acme-dev"):
                    entered.append(True)
        self.assertEqual(entered, [])

    def test_lock_unlinked_during_acquisition_fails_closed(self):
        real_flock = lock.fcntl.flock
        entered = []

        def unlink_lock(fd, flags):
            real_flock(fd, flags)
            (self.root / "acme-dev.lock").unlink()

        with mock.patch.object(lock.fcntl, "flock", side_effect=unlink_lock):
            with self.assertRaises(lock.OperationLockError):
                with lock.project_operation_lock("acme-dev"):
                    entered.append(True)
        self.assertEqual(entered, [])


    def test_lease_checks_unchanged_lock_at_stage_boundaries(self):
        with lock.project_operation_lock("acme-dev") as check_lock:
            self.assertTrue(callable(check_lock))
            check_lock()
            check_lock()

    def test_lease_rejects_directory_swap_while_held(self):
        moved = self.root.parent / "old-locks"
        with self.assertRaisesRegex(lock.OperationLockError, "replaced"):
            with lock.project_operation_lock("acme-dev") as check_lock:
                self.root.rename(moved)
                self.root.mkdir(mode=0o700)
                check_lock()

    def test_lease_rejects_lock_inode_swap_while_held(self):
        with self.assertRaisesRegex(lock.OperationLockError, "replaced"):
            with lock.project_operation_lock("acme-dev") as check_lock:
                path = self.root / "acme-dev.lock"
                path.rename(self.root / "old.lock")
                path.write_text("")
                path.chmod(0o600)
                check_lock()

    def test_lease_rejects_unlink_or_mode_change_while_held(self):
        for change in ("unlink", "chmod"):
            with self.subTest(change=change):
                path = self.root / "acme-dev.lock"
                if path.exists():
                    path.chmod(0o600)
                with self.assertRaises(lock.OperationLockError):
                    with lock.project_operation_lock("acme-dev") as check_lock:
                        if change == "unlink":
                            path.unlink()
                        else:
                            path.chmod(0o644)
                        check_lock()

    def test_lease_checks_again_on_normal_context_exit(self):
        with self.assertRaisesRegex(lock.OperationLockError, "replaced"):
            with lock.project_operation_lock("acme-dev"):
                path = self.root / "acme-dev.lock"
                path.rename(self.root / "detached.lock")
                path.write_text("")
                path.chmod(0o600)

    def test_invalid_project(self):
        for project in ("../other", "acme/dev", "", "UPPER", "-acme"):
            with self.assertRaises(lock.OperationLockError):
                with lock.project_operation_lock(project):
                    pass

    def test_released_after_exception(self):
        with self.assertRaisesRegex(RuntimeError, "body"):
            with lock.project_operation_lock("acme-dev"):
                raise RuntimeError("body")
        with lock.project_operation_lock("acme-dev"):
            pass

    def test_untrusted_owner(self):
        with mock.patch.object(lock.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(lock.OperationLockError):
                with lock.project_operation_lock("acme-dev"):
                    pass


if __name__ == "__main__":
    unittest.main()

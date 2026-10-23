"""Credential-free voice overlay / encrypted backup exclusion tests (#230)."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_voice():
    spec = importlib.util.spec_from_file_location(
        "voice_deploy_lock_under_test", ROOT / "scripts" / "deploy-reference-voice.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


VOICE = _load_voice()


class VoiceBackupLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        lock_root = self.root / "locks"
        lock_root.mkdir(mode=0o700)
        patch = mock.patch.object(VOICE.client_operation_lock, "LOCK_ROOT", lock_root)
        patch.start()
        self.addCleanup(patch.stop)
        self.binding = self.root / "binding.json"
        self.binding.write_text(json.dumps({
            "spec": {"channels": {"voice": {
                "provider": "livekit", "url": "wss://voice.example.test",
                "api_key_ref": "secret://voice/key", "api_secret_ref": "secret://voice/secret"
            }}}
        }), encoding="utf-8")

    def apply(self, project="acme-dev"):
        return VOICE.apply(self.binding, self.root / "bundle", project, None, 10)

    def test_active_backup_blocks_voice_before_mutation(self):
        with VOICE.client_operation_lock.project_operation_lock("acme-dev"):
            with mock.patch.object(VOICE, "_apply_selected_voice") as deploy:
                with self.assertRaises(VOICE.client_operation_lock.OperationLockError):
                    self.apply()
                deploy.assert_not_called()

    def test_voice_holds_lock_through_failure_and_releases_for_retry(self):
        def failed_rollback(*args):
            with self.assertRaises(VOICE.client_operation_lock.OperationLockError):
                with VOICE.client_operation_lock.project_operation_lock("acme-dev"):
                    pass
            raise ValueError("simulated rollback failure")

        with mock.patch.object(VOICE, "_apply_selected_voice", side_effect=failed_rollback) as deploy:
            with self.assertRaisesRegex(ValueError, "rollback failure"):
                self.apply()
            deploy.assert_called_once()
        with VOICE.client_operation_lock.project_operation_lock("acme-dev"):
            pass

    def test_other_client_remains_independent(self):
        with VOICE.client_operation_lock.project_operation_lock("acme-dev"):
            with mock.patch.object(VOICE, "_apply_selected_voice", return_value="deployed") as deploy:
                self.assertEqual(self.apply("other-dev"), "deployed")
                deploy.assert_called_once()

    def test_missing_or_unsafe_host_lock_fails_closed(self):
        lock_root = VOICE.client_operation_lock.LOCK_ROOT
        lock_root.chmod(0o755)
        with mock.patch.object(VOICE, "_apply_selected_voice") as deploy:
            with self.assertRaises(VOICE.client_operation_lock.OperationLockError):
                self.apply()
            deploy.assert_not_called()

    def test_invalid_project_is_rejected_before_deploy(self):
        with mock.patch.object(VOICE, "_apply_selected_voice") as deploy:
            with self.assertRaises(VOICE.client_operation_lock.OperationLockError):
                self.apply("../another")
            deploy.assert_not_called()

    def test_no_voice_selected_remains_a_noop_without_host_lock(self):
        self.binding.write_text(json.dumps({"spec": {"channels": {}}}), encoding="utf-8")
        with mock.patch.object(VOICE, "_apply_selected_voice") as deploy:
            self.assertIn("voice not selected", self.apply())
            deploy.assert_not_called()


if __name__ == "__main__":
    unittest.main()

"""Offline age-recipient rotation preflight for retained client snapshots (#230).

This test suite is intentionally credential-free; the real restore verifier is
tested separately with authenticated TAR fixtures in test_client_volume_restore.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("client_backup_key_rotation", ROOT / "scripts" / "client-backup-key-rotation.py")
ROTATE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(ROTATE)

OLD = "age1" + "q" * 58
NEW = "age1" + "p" * 58


class BackupKeyRotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.snapshots = self.root / "snapshots"
        self.snapshots.mkdir(mode=0o700)
        self.compose = self.root / "compose.yaml"
        self.compose.write_text("services:\n  hermes:\n    volumes: [hermes-data:/data]\nvolumes:\n  hermes-data: {}\n")
        self.identity = self.root / "old-identity"
        self.identity.write_text("AGE-SECRET-KEY-FAKE-TEST-ONLY\n")
        self.identity.chmod(0o600)
        self.add_snapshot("acme-dev-1234567890-aabbccddee")
        self.add_snapshot("acme-dev-1234567891-aabbccddee")

    def add_snapshot(self, name, *, recipient=OLD, project="acme-dev"):
        source = self.snapshots / name
        source.mkdir(mode=0o700)
        doc = {
            "schemaVersion": 1, "type": "stopped-compose-volume-snapshot",
            "clientProject": project, "ageRecipientSha256": hashlib.sha256(recipient.encode()).hexdigest(),
            "volumes": [{"volume": "hermes-data", "object": "hermes-data.tar.age",
                         "sha256": "a" * 64, "bytes": 10}],
        }
        (source / "manifest.json").write_text(json.dumps(doc))
        (source / "manifest.json").chmod(0o600)
        return source

    def run_plan(self, **changes):
        options = {"compose": self.compose, "project": "acme-dev",
                   "snapshots": self.snapshots, "old_recipient": OLD,
                   "new_recipient": NEW, "old_identity": self.identity}
        with mock.patch.object(ROTATE.RESTORE, "verify", return_value=["hermes-data"]) as verifier:
            result = ROTATE.plan_rotation(**(options | changes))
        return result, verifier

    def test_all_retained_snapshots_must_be_decryptable_and_old_key_retained(self):
        result, verifier = self.run_plan()
        self.assertEqual(verifier.call_count, 2)
        self.assertEqual(result["status"], "review-required-not-cloud-proof")
        self.assertEqual(result["verifiedLocalSnapshots"], 2)
        self.assertTrue(result["retainOldIdentity"])
        self.assertFalse(result["cloudInventoryComplete"])
        self.assertFalse(result["liveRestoreProven"])
        self.assertNotIn("AGE-SECRET", json.dumps(result))
        self.assertNotIn(OLD, json.dumps(result))
        self.assertNotIn(NEW, json.dumps(result))

    def test_refuses_missing_or_empty_snapshot_inventory(self):
        for path in list(self.snapshots.iterdir()):
            for child in path.iterdir():
                child.unlink()
            path.rmdir()
        with self.assertRaises(ROTATE.RotationError):
            self.run_plan()
        with self.assertRaises(ROTATE.RotationError):
            self.run_plan(snapshots=self.root / "missing")

    def test_refuses_foreign_recipient_or_project_before_decryption(self):
        source = next(self.snapshots.iterdir())
        for change in ({"ageRecipientSha256": "f" * 64}, {"clientProject": "other-dev"}):
            path = source / "manifest.json"
            doc = json.loads(path.read_text())
            original = doc.copy()
            doc.update(change)
            path.write_text(json.dumps(doc))
            with mock.patch.object(ROTATE.RESTORE, "verify", side_effect=AssertionError("must not decrypt")):
                with self.assertRaises(ROTATE.RotationError):
                    ROTATE.plan_rotation(compose=self.compose, project="acme-dev", snapshots=self.snapshots,
                                         old_recipient=OLD, new_recipient=NEW, old_identity=self.identity)
            path.write_text(json.dumps(original))

    def test_refuses_changed_or_undecryptable_snapshot(self):
        with mock.patch.object(ROTATE.RESTORE, "verify", side_effect=ROTATE.RESTORE.RestoreError("invalid ciphertext")):
            with self.assertRaises(ROTATE.RotationError):
                ROTATE.plan_rotation(compose=self.compose, project="acme-dev", snapshots=self.snapshots,
                                     old_recipient=OLD, new_recipient=NEW, old_identity=self.identity)

    def test_refuses_unsafe_inventory_paths_and_extra_entries(self):
        source = next(self.snapshots.iterdir())
        source.chmod(0o755)
        with self.assertRaises(ROTATE.RotationError):
            self.run_plan()
        source.chmod(0o700)
        extra = self.snapshots / "unreviewed.txt"
        extra.write_text("do not ignore")
        with self.assertRaises(ROTATE.RotationError):
            self.run_plan()
        extra.unlink()
        source.rename(self.root / "real")
        source.symlink_to(self.root / "real", target_is_directory=True)
        with self.assertRaises(ROTATE.RotationError):
            self.run_plan()

    def test_refuses_unsafe_manifest_and_key_identity(self):
        source = next(self.snapshots.iterdir())
        manifest = source / "manifest.json"
        manifest.chmod(0o644)
        with self.assertRaises(ROTATE.RotationError):
            self.run_plan()
        manifest.chmod(0o600)
        self.identity.chmod(0o644)
        with self.assertRaises(ROTATE.RotationError):
            self.run_plan()

    def test_refuses_reusing_recipient_and_wrong_client_or_missing_key(self):
        for change in ({"new_recipient": OLD}, {"new_recipient": "invalid"},
                       {"project": "other-dev"}, {"old_identity": self.root / "missing"}):
            with self.subTest(change=change), self.assertRaises(ROTATE.RotationError):
                self.run_plan(**change)

    def test_refuses_manifest_that_is_not_strict_json_object(self):
        source = next(self.snapshots.iterdir())
        (source / "manifest.json").write_text("[]")
        with self.assertRaises(ROTATE.RotationError):
            self.run_plan()


if __name__ == "__main__":
    unittest.main()

"""Offline and adversarial tests for tenant-bound encrypted GCS read-back (#230)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("backup_readback", ROOT / "scripts" / "client-backup-readback.py")
READBACK = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(READBACK)


class CloudReadbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.root.chmod(0o700)
        self.compose = self.root / "compose.yaml"
        self.compose.write_text(yaml.safe_dump({
            "services": {"hermes": {"volumes": ["hermes-data:/data"]}},
            "volumes": {"hermes-data": {}},
        }), encoding="utf-8")
        self.destination = self.root / "acme-dev-recovered"
        self.ciphertext = b"age encrypted fixture bytes (not real ciphertext)"
        self.manifest = {
            "schemaVersion": 1, "type": "stopped-compose-volume-snapshot",
            "clientProject": "acme-dev", "createdAt": "2026-10-20T00:00:00Z",
            "ageRecipientSha256": "b" * 64,
            "volumes": [{
                "volume": "hermes-data", "object": "hermes-data.tar.age",
                "bytes": len(self.ciphertext),
                "sha256": hashlib.sha256(self.ciphertext).hexdigest(),
            }],
        }
        self.remote = {
            "manifest.json": json.dumps(self.manifest).encode(),
            "hermes-data.tar.age": self.ciphertext,
        }
        self.calls = []

    def copy(self, uri, destination):
        self.calls.append(uri)
        pathlib.Path(destination).write_bytes(self.remote[uri.rsplit("/", 1)[-1]])

    def size(self, uri):
        return len(self.remote[uri.rsplit("/", 1)[-1]])

    def run(self, **overrides):
        opts = {"compose": self.compose, "project": "acme-dev",
                "destination": self.destination, "run_id": "acme-dev-run12345",
                "gcp_project": "sample-project-123", "client_slug": "acme",
                "environment": "dev"}
        opts.update(overrides)
        with mock.patch.object(READBACK.shutil, "which", return_value="/usr/bin/gcloud"), \
             mock.patch.object(READBACK, "_cloud_size", side_effect=self.size), \
             mock.patch.object(READBACK, "_cloud_copy", side_effect=self.copy):
            return READBACK.retrieve(**opts)

    def test_readback_requires_manifest_then_all_ciphertext_with_client_scoped_urls(self):
        result = self.run()
        bucket = READBACK.UPLOAD.expected_bucket("sample-project-123", "acme", "dev")
        prefix = f"gs://{bucket}/v1/acme-dev/acme-dev-run12345/"
        self.assertEqual(self.calls, [prefix + "manifest.json", prefix + "hermes-data.tar.age"])
        self.assertEqual(result["status"], "cloud-ciphertext-verified-not-recovery-proven")
        self.assertEqual(result["volumes"], 1)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.destination / "manifest.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.destination / "hermes-data.tar.age").read_bytes(), self.ciphertext)
        self.assertEqual((self.destination / "hermes-data.tar.age").stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.destination / ".manifest.partial").exists())

    def test_rejects_wrong_client_environment_run_path_and_reused_destination_before_cloud(self):
        for overrides in [
            {"project": "other-dev"}, {"client_slug": "other"}, {"environment": "prod"},
            {"run_id": "other-dev-run12345"}, {"run_id": "../acme-dev"},
            {"gcp_project": "not a valid project"},
        ]:
            with self.subTest(overrides=overrides), self.assertRaises(READBACK.ReadbackError):
                self.run(**overrides)
        self.assertEqual(self.calls, [])
        self.destination.mkdir()
        with self.assertRaisesRegex(READBACK.ReadbackError, "exist"):
            self.run()
        self.assertFalse(self.calls)

    def test_manifest_absence_or_unbounded_size_is_rejected_without_other_object_download(self):
        self.remote.pop("manifest.json")
        with self.assertRaises(READBACK.ReadbackError):
            self.run()
        self.assertFalse(self.calls)
        self.remote["manifest.json"] = b"x" * (READBACK.RESTORE.MAX_MANIFEST_BYTES + 1)
        with self.assertRaisesRegex(READBACK.ReadbackError, "size"):
            self.run()
        self.assertFalse(self.calls)

    def test_wrong_volume_inventory_or_forged_object_name_fails_before_archive_download(self):
        cases = [
            {"clientProject": "other-dev"},
            {"volumes": []},
            {"volumes": [{"volume": "hermes-data", "object": "../bad",
                          "bytes": 3, "sha256": "a" * 64}]},
        ]
        for replacement in cases:
            with self.subTest(replacement=replacement):
                document = dict(self.manifest, **replacement)
                self.remote["manifest.json"] = json.dumps(document).encode()
                with self.assertRaises(READBACK.ReadbackError):
                    self.run()
                self.assertEqual(len(self.calls), 1)
                self.assertFalse((self.destination / "manifest.json").exists())
                self.calls.clear()
                if self.destination.exists():
                    # Explicitly simulate an operator starting a new, unique retrieval.
                    self.destination = self.root / ("acme-dev-recovered-" + str(len(str(replacement))))

    def test_remote_ciphertext_tampering_blocks_completion_marker(self):
        self.remote["hermes-data.tar.age"] = b"changed-but-same-size!"[:len(self.ciphertext)]
        with self.assertRaisesRegex(READBACK.ReadbackError, "integrity"):
            self.run()
        self.assertEqual(len(self.calls), 2)
        self.assertFalse((self.destination / "manifest.json").exists())

    def test_metadata_size_mismatch_blocks_archive_copy(self):
        original = self.size
        def wrong_size(uri):
            return original(uri) + (1 if uri.endswith(".tar.age") else 0)
        with mock.patch.object(READBACK.shutil, "which", return_value="/usr/bin/gcloud"), \
             mock.patch.object(READBACK, "_cloud_size", side_effect=wrong_size), \
             mock.patch.object(READBACK, "_cloud_copy", side_effect=self.copy):
            with self.assertRaisesRegex(READBACK.ReadbackError, "size"):
                READBACK.retrieve(compose=self.compose, project="acme-dev",
                                  destination=self.destination, run_id="acme-dev-run12345",
                                  gcp_project="sample-project-123", client_slug="acme",
                                  environment="dev")
        self.assertEqual(len(self.calls), 1)
        self.assertFalse((self.destination / "manifest.json").exists())

    def test_explicit_private_age_key_can_add_authenticated_archive_validation(self):
        identity = self.root / "age-key"
        identity.write_text("fixture not an actual key", encoding="utf-8")
        identity.chmod(0o600)
        with mock.patch.object(READBACK.RESTORE, "verify", return_value=["hermes-data"]) as verify:
            result = self.run(identity=identity)
        verify.assert_called_once()
        self.assertEqual(result["status"], "cloud-ciphertext-and-age-verified-not-recovery-proven")
        self.assertEqual(result["volumes"], 1)


if __name__ == "__main__":
    unittest.main()

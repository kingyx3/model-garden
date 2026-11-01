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
        self.readers = []

    def copy(self, uri, destination, *, reader_identity):
        self.readers.append(reader_identity)
        self.calls.append(uri)
        pathlib.Path(destination).write_bytes(self.remote[uri.rsplit("/", 1)[-1]])

    def size(self, uri, *, reader_identity):
        self.readers.append(reader_identity)
        name = uri.rsplit("/", 1)[-1]
        if name not in self.remote:
            raise READBACK.ReadbackError("cloud object missing")
        return len(self.remote[name])

    def readback(self, **overrides):
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
        result = self.readback()
        bucket = READBACK.UPLOAD.expected_bucket("sample-project-123", "acme", "dev")
        prefix = f"gs://{bucket}/v1/acme-dev/acme-dev-run12345/"
        self.assertEqual(self.calls, [prefix + "manifest.json", prefix + "hermes-data.tar.age"])
        self.assertEqual(set(self.readers), {READBACK.expected_reader_identity("sample-project-123", "acme", "dev")})
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
                self.readback(**overrides)
        self.assertEqual(self.calls, [])
        self.destination.mkdir()
        with self.assertRaisesRegex(READBACK.ReadbackError, "exist"):
            self.readback()
        self.assertFalse(self.calls)

    def test_manifest_absence_or_unbounded_size_is_rejected_without_other_object_download(self):
        self.remote.pop("manifest.json")
        with self.assertRaises(READBACK.ReadbackError):
            self.readback()
        self.assertFalse(self.calls)
        self.remote["manifest.json"] = b"x" * (READBACK.RESTORE.MAX_MANIFEST_BYTES + 1)
        with self.assertRaisesRegex(READBACK.ReadbackError, "size"):
            self.readback()
        self.assertFalse(self.calls)

    def test_wrong_volume_inventory_or_forged_object_name_fails_before_archive_download(self):
        cases = [
            {"clientProject": "other-dev"},
            {"volumes": []},
            {"volumes": [{"volume": "hermes-data", "object": "../bad",
                          "bytes": 3, "sha256": "a" * 64}]},
        ]
        for index, replacement in enumerate(cases):
            with self.subTest(replacement=replacement):
                self.destination = self.root / f"acme-dev-invalid-{index}"
                document = dict(self.manifest, **replacement)
                self.remote["manifest.json"] = json.dumps(document).encode()
                with self.assertRaises(READBACK.ReadbackError):
                    self.readback()
                self.assertEqual(len(self.calls), 1)
                self.assertFalse((self.destination / "manifest.json").exists())
                self.calls.clear()

    def test_remote_ciphertext_tampering_blocks_completion_marker(self):
        self.remote["hermes-data.tar.age"] = b"x" * len(self.ciphertext)
        with self.assertRaisesRegex(READBACK.ReadbackError, "integrity"):
            self.readback()
        self.assertEqual(len(self.calls), 2)
        self.assertFalse((self.destination / "manifest.json").exists())

    def test_metadata_size_mismatch_blocks_archive_copy(self):
        original = self.size
        def wrong_size(uri, *, reader_identity):
            return original(uri, reader_identity=reader_identity) + (1 if uri.endswith(".tar.age") else 0)
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

    def test_readback_uses_exact_terraform_reader_and_never_ambient_gcloud_identity(self):
        reader = READBACK.expected_reader_identity("sample-project-123", "acme", "dev")
        self.assertEqual(reader, "mg-bkr-" + hashlib.md5(b"acme-dev-mg").hexdigest()[:8]
                         + "@sample-project-123.iam.gserviceaccount.com")
        self.assertNotEqual(reader, READBACK.UPLOAD.expected_writer_identity("sample-project-123", "acme", "dev"))
        self.assertNotEqual(reader, READBACK.expected_reader_identity("sample-project-123", "acme", "prod"))
        for cloud, client, env in (("bad project", "acme", "dev"),
                                   ("sample-project-123", "bad;client", "dev"),
                                   ("sample-project-123", "acme", "staging")):
            with self.subTest(cloud=cloud, client=client, env=env), self.assertRaises(READBACK.ReadbackError):
                READBACK.expected_reader_identity(cloud, client, env)
        with mock.patch.object(READBACK.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="ok")) as run:
            self.assertEqual(READBACK._run_gcloud(["storage", "objects", "describe", "gs://example"],
                                                  timeout=120, reader_identity=reader), "ok")
        args, kwargs = run.call_args
        self.assertEqual(args[0][:3], ["gcloud", "--impersonate-service-account=" + reader, "storage"])
        self.assertTrue(kwargs["capture_output"])
        self.assertNotIn("secret", str(kwargs))

    def test_readback_refuses_gcloud_reader_impersonation_failure(self):
        reader = READBACK.expected_reader_identity("sample-project-123", "acme", "dev")
        with mock.patch.object(READBACK.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="", stderr="secret")):
            with self.assertRaisesRegex(READBACK.ReadbackError, "access denied"):
                READBACK._run_gcloud(["storage", "objects", "describe", "gs://example"],
                                     timeout=120, reader_identity=reader)

    def test_explicit_private_age_key_can_add_authenticated_archive_validation(self):
        identity = self.root / "age-key"
        identity.write_text("fixture not an actual key", encoding="utf-8")
        identity.chmod(0o600)
        with mock.patch.object(READBACK.RESTORE, "verify", return_value=["hermes-data"]) as verify:
            result = self.readback(identity=identity)
        verify.assert_called_once()
        self.assertEqual(result["status"], "cloud-ciphertext-and-age-verified-not-recovery-proven")
        self.assertEqual(result["volumes"], 1)


if __name__ == "__main__":
    unittest.main()

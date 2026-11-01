"""Offline fail-closed upload regressions for client-scoped encrypted backup objects (#230)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("model_garden_backup_upload", ROOT / "scripts" / "client-backup-upload.py")
UPLOAD = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(UPLOAD)


class EncryptedBackupUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.compose = self.root / "compose.yaml"
        self.compose.write_text(yaml.safe_dump({
            "services": {"hermes": {"volumes": ["hermes-data:/data"]}},
            "volumes": {"hermes-data": {}},
        }), encoding="utf-8")
        self.source = self.root / "acme-dev-run12345"
        self.source.mkdir(mode=0o700)
        self.manifest = self.source / "manifest.json"
        self.encrypted = self.source / "hermes-data.tar.age"
        self.encrypted.write_bytes(b"encrypted fixture archive - not actual age ciphertext")
        self.encrypted.chmod(0o600)
        self.write_manifest()
        self.calls = []

    def write_manifest(self):
        manifest = {
            "schemaVersion": 1,
            "type": "stopped-compose-volume-snapshot",
            "clientProject": "acme-dev",
            "createdAt": "2026-10-20T08:00:00Z",
            "ageRecipientSha256": "b" * 64,
            "volumes": [{
                "volume": "hermes-data", "object": "hermes-data.tar.age",
                "bytes": self.encrypted.stat().st_size,
                "sha256": hashlib.sha256(self.encrypted.read_bytes()).hexdigest(),
            }],
        }
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        self.manifest.chmod(0o600)

    def upload(self, **overrides):
        values = {"compose": self.compose, "project": "acme-dev", "source": self.source,
                  "gcp_project": "sample-project-123", "client_slug": "acme",
                  "environment": "dev"}
        values.update(overrides)
        return UPLOAD.upload(**values)

    def fake_gcloud(self, args, **kwargs):
        self.calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def test_objects_created_only_at_terraform_derived_bucket_with_no_clobber(self):
        with mock.patch.object(UPLOAD.shutil, "which", return_value="/usr/bin/gcloud"), \
             mock.patch.object(UPLOAD.subprocess, "run", side_effect=self.fake_gcloud):
            result = self.upload()
        bucket = hashlib.md5(b"sample-project-123").hexdigest()[:10] + "-acme-dev-mg-backups"
        root = f"gs://{bucket}/v1/acme-dev/acme-dev-run12345/"
        self.assertEqual(result["bucket"], bucket)
        self.assertEqual(result["objects"], 2)
        self.assertEqual(len(self.calls), 2)
        writer = "mg-bkp-" + hashlib.md5(b"acme-dev-mg").hexdigest()[:8] + "@sample-project-123.iam.gserviceaccount.com"
        for call in self.calls:
            self.assertEqual(call[:4], ["gcloud", "--impersonate-service-account=" + writer, "storage", "cp"])
        self.assertEqual(self.calls[0][4], str(self.encrypted))
        self.assertEqual(self.calls[0][5], root + "hermes-data.tar.age")
        self.assertEqual(self.calls[1][4], str(self.manifest))
        self.assertEqual(self.calls[1][5], root + "manifest.json")
        for call in self.calls:
            self.assertIn("--if-generation-match=0", call)
            self.assertIn("--quiet", call)
            self.assertNotIn("--no-clobber", call)  # avoid read permissions and TOCTOU
            self.assertNotIn("-r", call)  # no broad directory copy
        self.assertEqual(result["clientProject"], "acme-dev")

    def test_backup_writer_identity_is_deterministic_and_scoped_to_exact_tenant(self):
        expected = "mg-bkp-" + hashlib.md5(b"acme-dev-mg").hexdigest()[:8] + "@sample-project-123.iam.gserviceaccount.com"
        self.assertEqual(UPLOAD.expected_writer_identity("sample-project-123", "acme", "dev"), expected)
        self.assertNotEqual(UPLOAD.expected_writer_identity("sample-project-123", "acme", "prod"), expected)
        self.assertNotEqual(UPLOAD.expected_writer_identity("sample-project-123", "other", "dev"), expected)
        self.assertNotEqual(UPLOAD.expected_writer_identity("other-project-123", "acme", "dev"), expected)
        for cloud, client, env in (
            ("invalid project", "acme", "dev"),
            ("sample-project-123", "acme;other", "dev"),
            ("sample-project-123", "acme", "staging"),
        ):
            with self.subTest(cloud=cloud, client=client, env=env):
                with self.assertRaises(UPLOAD.UploadError):
                    UPLOAD.expected_writer_identity(cloud, client, env)

    def test_wrong_project_or_cloud_context_never_calls_gcloud(self):
        for kwargs in (
            {"project": "other-dev"},
            {"client_slug": "other"},
            {"environment": "prod"},
            {"gcp_project": "not a project"},
        ):
            with self.subTest(kwargs=kwargs), \
                 mock.patch.object(UPLOAD.shutil, "which", return_value="/usr/bin/gcloud"), \
                 mock.patch.object(UPLOAD.subprocess, "run", side_effect=self.fake_gcloud), \
                 self.assertRaises(UPLOAD.UploadError):
                self.upload(**kwargs)
        self.assertFalse(self.calls)

    def test_corrupt_manifest_or_ciphertext_and_missing_manifest_fail_closed(self):
        with mock.patch.object(UPLOAD.shutil, "which", return_value="/usr/bin/gcloud"), \
             mock.patch.object(UPLOAD.subprocess, "run", side_effect=self.fake_gcloud):
            self.encrypted.write_bytes(b"tampered")
            with self.assertRaisesRegex(UPLOAD.UploadError, "integrity"):
                self.upload()
            self.write_manifest()
            self.manifest.unlink()
            with self.assertRaisesRegex(UPLOAD.UploadError, "manifest"):
                self.upload()
        self.assertFalse(self.calls)

    def test_rejects_archive_symlink_and_insecure_permissions(self):
        self.encrypted.rename(self.root / "original")
        self.encrypted.symlink_to(self.root / "original")
        with mock.patch.object(UPLOAD.shutil, "which", return_value="/usr/bin/gcloud"), \
             mock.patch.object(UPLOAD.subprocess, "run", side_effect=self.fake_gcloud), \
             self.assertRaises(UPLOAD.UploadError):
            self.upload()
        self.encrypted.unlink()
        self.encrypted.write_bytes(b"new")
        self.encrypted.chmod(0o600)
        self.write_manifest()
        self.source.chmod(0o755)
        with mock.patch.object(UPLOAD.shutil, "which", return_value="/usr/bin/gcloud"), \
             mock.patch.object(UPLOAD.subprocess, "run", side_effect=self.fake_gcloud), \
             self.assertRaisesRegex(UPLOAD.UploadError, "private"):
            self.upload()
        self.assertFalse(self.calls)

    def test_upload_failure_prevents_completion_marker(self):
        def fail_archive(args, **_kwargs):
            self.calls.append(args)
            return subprocess.CompletedProcess(args, 1, stdout="irrelevant", stderr="secret value")
        with mock.patch.object(UPLOAD.shutil, "which", return_value="/usr/bin/gcloud"), \
             mock.patch.object(UPLOAD.subprocess, "run", side_effect=fail_archive):
            with self.assertRaisesRegex(UPLOAD.UploadError, "upload failed"):
                self.upload()
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("manifest.json", " ".join(self.calls[0][4:5]))

    def test_mutation_after_archive_upload_blocks_manifest_publish(self):
        def corrupt_after_upload(args, **_kwargs):
            self.calls.append(args)
            self.encrypted.write_bytes(b"changed midway")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        with mock.patch.object(UPLOAD.shutil, "which", return_value="/usr/bin/gcloud"), \
             mock.patch.object(UPLOAD.subprocess, "run", side_effect=corrupt_after_upload):
            with self.assertRaisesRegex(UPLOAD.UploadError, "integrity"):
                self.upload()
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()

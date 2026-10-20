"""Offline restore checks: reject mismatched clients, hostile TAR and nonempty targets."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import pathlib
import tarfile
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("client_volume_restore_test_module", ROOT / "scripts" / "client-volume-restore.py")
RESTORE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(RESTORE)
RECIPIENT = "age1" + "q" * 55
IMAGE = "docker.io/library/busybox@sha256:" + "a" * 64


class ClientVolumeRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.compose = self.root / "compose.yaml"
        self.compose.write_text(yaml.safe_dump({
            "services": {"hermes": {"volumes": ["hermes-data:/data"]}},
            "volumes": {"hermes-data": {}},
        }), encoding="utf-8")
        self.archive = self.root / "backup"
        self.archive.mkdir()
        self.identity = self.root / "age-key"
        self.identity.write_text("AGE-SECRET-KEY-FAKE-FOR-TESTS\n")
        self.identity.chmod(0o600)
        self.fakebin = self.root / "bin"
        self.fakebin.mkdir()
        fake_age = self.fakebin / "age"
        fake_age.write_text(
            '#!/usr/bin/env python3\nimport pathlib,sys\nsys.stdout.buffer.write(pathlib.Path(sys.argv[-1]).read_bytes())\n',
            encoding="utf-8")
        fake_age.chmod(0o700)
        self.old_path = os.environ.get("PATH", "")
        self.patch_path = mock.patch.dict(os.environ, {"PATH": str(self.fakebin) + ":" + self.old_path})
        self.patch_path.start()
        self.addCleanup(self.patch_path.stop)
        self.write_backup()

    def tar_bytes(self, member_name="hello.txt", *, kind="file"):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as out:
            if kind == "file":
                member = tarfile.TarInfo(member_name)
                member.size = 7
                out.addfile(member, io.BytesIO(b"fixture"))
            elif kind == "symlink":
                member = tarfile.TarInfo(member_name)
                member.type = tarfile.SYMTYPE
                member.linkname = "../../etc/passwd"
                out.addfile(member)
        return data.getvalue()

    def write_backup(self, payload=None, *, project="acme-dev"):
        payload = payload if payload is not None else self.tar_bytes()
        blob = self.archive / "hermes-data.tar.age"
        blob.write_bytes(payload)  # fixture-only fake age: production uses real age authenticated ciphertext
        blob.chmod(0o600)
        record = {"volume": "hermes-data", "object": blob.name,
                  "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
        manifest = {"schemaVersion": 1, "clientProject": project, "type": "stopped-compose-volume-snapshot",
                    "createdAt": "2026-10-20T00:00:00Z",
                    "ageRecipientSha256": hashlib.sha256(RECIPIENT.encode()).hexdigest(),
                    "volumes": [record]}
        (self.archive / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (self.archive / "manifest.json").chmod(0o600)
        return manifest

    def test_valid_backup_is_verified_without_docker_or_client_payload_output(self):
        with mock.patch.object(RESTORE, "_docker", side_effect=AssertionError("verification must be offline")):
            summary = RESTORE.verify(self.compose, "acme-dev", self.archive, self.identity)
        self.assertEqual(summary, ["hermes-data"])

    def test_rejects_client_mismatch_missing_volume_and_hash_tamper(self):
        with self.assertRaisesRegex(RESTORE.RestoreError, "client"):
            RESTORE.verify(self.compose, "other-dev", self.archive, self.identity)
        body = json.loads((self.archive / "manifest.json").read_text())
        body["volumes"] = []
        (self.archive / "manifest.json").write_text(json.dumps(body), encoding="utf-8")
        with self.assertRaisesRegex(RESTORE.RestoreError, "volume"):
            RESTORE.verify(self.compose, "acme-dev", self.archive, self.identity)
        self.write_backup()
        (self.archive / "hermes-data.tar.age").write_bytes(b"tampered")
        with self.assertRaisesRegex(RESTORE.RestoreError, "digest"):
            RESTORE.verify(self.compose, "acme-dev", self.archive, self.identity)

    def test_rejects_path_escape_symlinks_and_non_regular_tar_members(self):
        for name,kind in [("../escape", "file"), ("/absolute", "file"),
                          ("safe/link", "symlink"), ("a/../../escape", "file")]:
            with self.subTest(name=name):
                self.write_backup(self.tar_bytes(name, kind=kind))
                with self.assertRaisesRegex(RESTORE.RestoreError, "unsafe"):
                    RESTORE.verify(self.compose, "acme-dev", self.archive, self.identity)

    def test_refuses_unsafe_identity_or_archive_symlinks(self):
        self.identity.chmod(0o644)
        with self.assertRaisesRegex(RESTORE.RestoreError, "identity"):
            RESTORE.verify(self.compose, "acme-dev", self.archive, self.identity)
        self.identity.chmod(0o600)
        (self.archive / "hermes-data.tar.age").rename(self.root / "real")
        (self.archive / "hermes-data.tar.age").symlink_to(self.root / "real")
        with self.assertRaisesRegex(RESTORE.RestoreError, "symlink"):
            RESTORE.verify(self.compose, "acme-dev", self.archive, self.identity)

    def test_target_requires_empty_owned_volume_and_stopped_project(self):
        with mock.patch.object(RESTORE.SNAPSHOT, "assert_quiescent", side_effect=RESTORE.SNAPSHOT.SnapshotError("running")):
            with self.assertRaisesRegex(RESTORE.RestoreError, "running"):
                RESTORE.restore(self.compose, "acme-dev", self.archive, self.identity, IMAGE, apply=True, confirmation="acme-dev")
        with mock.patch.object(RESTORE.SNAPSHOT, "assert_quiescent"), \
             mock.patch.object(RESTORE.SNAPSHOT, "assert_volume_owner", return_value="acme-dev_hermes-data"), \
             mock.patch.object(RESTORE, "_docker", side_effect=RESTORE.RestoreError("not empty")):
            with self.assertRaisesRegex(RESTORE.RestoreError, "not empty"):
                RESTORE.restore(self.compose, "acme-dev", self.archive, self.identity, IMAGE, apply=True, confirmation="acme-dev")

    def test_apply_requires_explicit_confirmation_and_never_implicitly_starts_stack(self):
        with self.assertRaisesRegex(RESTORE.RestoreError, "confirm"):
            RESTORE.restore(self.compose, "acme-dev", self.archive, self.identity, IMAGE, apply=True, confirmation="wrong")
        with mock.patch.object(RESTORE.SNAPSHOT, "assert_quiescent"), \
             mock.patch.object(RESTORE.SNAPSHOT, "assert_volume_owner", return_value="acme-dev_hermes-data"), \
             mock.patch.object(RESTORE, "_docker", return_value=""), \
             mock.patch.object(RESTORE, "_extract") as extract:
            result = RESTORE.restore(self.compose, "acme-dev", self.archive, self.identity, IMAGE, apply=True, confirmation="acme-dev")
        self.assertEqual(result, ["hermes-data"])
        extract.assert_called_once()
        self.assertEqual(extract.call_args.args[0], "acme-dev_hermes-data")

    def test_refuses_non_digest_pinned_helper(self):
        with self.assertRaisesRegex(RESTORE.RestoreError, "pinned"):
            RESTORE.restore(self.compose, "acme-dev", self.archive, self.identity, "busybox:latest", apply=True, confirmation="acme-dev")


if __name__ == "__main__":
    unittest.main()

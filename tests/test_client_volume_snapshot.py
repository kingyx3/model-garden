"""Credential-free regression coverage for stopped client-volume snapshots (#230)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("client_volume_snapshot", ROOT / "scripts" / "client-volume-snapshot.py")
SNAP = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SNAP)


class ClientSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.compose = self.root / "compose.yaml"
        self.document = {
            "services": {
                "hermes": {"volumes": ["hermes-data:/opt/data", "./profile:/opt/profile:ro"]},
                "tools": {"volumes": ["governance-data:/opt/data"]},
                "worker": {"volumes": [
                    "governance-data:/opt/data",
                    "hermes-data:/opt/hermes:ro",
                    {"type": "volume", "source": "coworker-data", "target": "/opt/agent", "read_only": True},
                ]},
            },
            "volumes": {"hermes-data": {}, "governance-data": {}, "coworker-data": {}},
        }
        self.write()

    def write(self):
        self.compose.write_text(yaml.safe_dump(self.document), encoding="utf-8")

    def test_inventories_all_declared_volumes_including_read_only_and_team_agent_stores(self):
        self.assertEqual(SNAP.inventory(self.compose), ["coworker-data", "governance-data", "hermes-data"])
        self.document["volumes"]["front-desk-hermes-data"] = {}
        self.document["services"]["front-desk-hermes"] = {
            "volumes": ["front-desk-hermes-data:/opt/data"]
        }
        self.write()
        self.assertIn("front-desk-hermes-data", SNAP.inventory(self.compose))

    def test_untracked_mutable_storage_fails_closed(self):
        examples = [
            ("anonymous", {"volumes": ["/opt/data"]}),
            ("writable-bind", {"volumes": ["./local-state:/opt/data"]}),
            ("unknown-named", {"volumes": ["other-data:/opt/data"]}),
            ("anonymous-long", {"volumes": [{"type": "volume", "target": "/opt/data"}]}),
            ("writable-long-bind", {"volumes": [{"type": "bind", "source": "./state", "target": "/opt/data"}]}),
        ]
        original = self.document["services"]["hermes"]
        for label, service in examples:
            with self.subTest(label=label):
                self.document["services"]["hermes"] = service
                self.write()
                with self.assertRaises(SNAP.SnapshotError):
                    SNAP.inventory(self.compose)
        self.document["services"]["hermes"] = original
        for spec in ({"external": True}, {"name": "shared"}, {"driver_opts": {"type": "none"}}):
            with self.subTest(spec=spec):
                self.document["volumes"]["hermes-data"] = spec
                self.write()
                with self.assertRaises(SNAP.SnapshotError):
                    SNAP.inventory(self.compose)

    def test_stopped_project_and_correct_docker_labels_required(self):
        with mock.patch.object(SNAP, "_docker", return_value="some-container"):
            with self.assertRaisesRegex(SNAP.SnapshotError, "running"):
                SNAP.assert_quiescent("acme-dev")
        with mock.patch.object(SNAP, "_docker", return_value='{"com.docker.compose.project":"other", "com.docker.compose.volume":"hermes-data"}'):
            with self.assertRaisesRegex(SNAP.SnapshotError, "ownership"):
                SNAP.assert_volume_owner("acme-dev", "hermes-data")
        with mock.patch.object(SNAP, "_docker", return_value='{"com.docker.compose.project":"acme-dev", "com.docker.compose.volume":"hermes-data"}'):
            self.assertEqual(SNAP.assert_volume_owner("acme-dev", "hermes-data"), "acme-dev_hermes-data")


    def test_volume_attachment_guard_accepts_only_stopped_same_project_containers(self):
        cid = "a" * 64
        for state in ("exited", "created"):
            with self.subTest(state=state):
                def docker(*args):
                    if args[0] == "ps":
                        self.assertIn("--all", args)
                        self.assertIn("--no-trunc", args)
                        self.assertIn("volume=acme-dev_hermes-data", args)
                        return cid
                    self.assertEqual(args[:3], ("inspect", "--type", "container"))
                    return f"{cid}|acme-dev|{state}|false|no"
                with mock.patch.object(SNAP, "_docker", side_effect=docker):
                    SNAP.assert_volume_exclusive("acme-dev", "hermes-data")
        with mock.patch.object(SNAP, "_docker", return_value=""):
            SNAP.assert_volume_exclusive("acme-dev", "hermes-data")

    def test_volume_attachment_guard_rejects_foreign_unlabelled_running_and_restartable(self):
        cid = "b" * 64
        invalid = (
            f"{cid}|other-dev|exited|false|no",
            f"{cid}|<no value>|exited|false|no",
            f"{cid}||exited|false|no",
            f"{cid}|acme-dev|running|true|no",
            f"{cid}|acme-dev|paused|false|no",
            f"{cid}|acme-dev|exited|false|always",
            f"{cid}|acme-dev|created|false|on-failure",
            f"{cid}|acme-dev|exited|false|",
            f"{'c' * 64}|acme-dev|exited|false|no",
            "malformed|record",
        )
        for record in invalid:
            with self.subTest(record=record[:25]):
                with mock.patch.object(SNAP, "_docker", side_effect=[cid, record]):
                    with self.assertRaises(SNAP.SnapshotError):
                        SNAP.assert_volume_exclusive("acme-dev", "hermes-data")

    def test_volume_attachment_guard_refuses_invalid_unbounded_and_failed_inventory(self):
        cid = "d" * 64
        for listing in ("not-an-id", cid + "\n" + cid,
                        "\n".join(f"{i:064x}" for i in range(65)), "x" * 10000):
            with self.subTest(length=len(listing)):
                with mock.patch.object(SNAP, "_docker", return_value=listing):
                    with self.assertRaises(SNAP.SnapshotError):
                        SNAP.assert_volume_exclusive("acme-dev", "hermes-data")
        with mock.patch.object(SNAP, "_docker", side_effect=SNAP.SnapshotError("inspect unavailable")):
            with self.assertRaises(SNAP.SnapshotError):
                SNAP.assert_volume_exclusive("acme-dev", "hermes-data")

    def test_foreign_volume_mount_blocks_snapshot_before_any_output(self):
        cid = "e" * 64
        def docker(*args):
            if args[:2] == ("volume", "inspect"):
                actual = args[2]
                project, name = actual.split("_", 1)
                return json.dumps({"com.docker.compose.project": project,
                                   "com.docker.compose.volume": name})
            if args[0] == "ps":
                return cid if any(str(arg).startswith("volume=") for arg in args) else ""
            if args[0] == "inspect":
                return f"{cid}|other-dev|exited|false|no"
            raise AssertionError("unexpected Docker call")
        output = self.root / "foreign"
        with mock.patch.object(SNAP.shutil, "which", return_value="/usr/bin/tool"), \
             mock.patch.object(SNAP, "_docker", side_effect=docker):
            with self.assertRaisesRegex(SNAP.SnapshotError, "foreign"):
                SNAP.snapshot(self.compose, "acme-dev", output, "age1" + "q" * 55,
                              "busybox@sha256:" + "a" * 64)
        self.assertFalse(output.exists())

    def test_docker_inspection_timeout_is_redacted_and_fails_closed(self):
        import subprocess
        with mock.patch.object(SNAP.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(["docker", "ps"], 30)):
            with self.assertRaisesRegex(SNAP.SnapshotError, "timed out"):
                SNAP.assert_volume_exclusive("acme-dev", "hermes-data")

    def test_refuses_insecure_destination_unpinned_image_and_invalid_recipient(self):
        output = self.root / "new"
        with self.assertRaisesRegex(SNAP.SnapshotError, "age recipient"):
            SNAP.snapshot(self.compose, "acme-dev", output, "garbage", "repo@sha256:" + "a"*64)
        with self.assertRaisesRegex(SNAP.SnapshotError, "pinned"):
            SNAP.snapshot(self.compose, "acme-dev", output, "age1" + "q"*55, "busybox:latest")
        output.mkdir()
        with mock.patch.object(SNAP, "assert_quiescent"), mock.patch.object(SNAP, "assert_volume_owner"), \
             mock.patch.object(SNAP.shutil, "which", return_value="/usr/bin/tool"):
            with self.assertRaisesRegex(SNAP.SnapshotError, "must not exist"):
                SNAP.snapshot(self.compose, "acme-dev", output, "age1" + "q"*55,
                              "busybox@sha256:" + "a"*64)

    def test_streaming_snapshot_has_only_ciphertext_and_integrity_manifest(self):
        bindir = self.root / "fake-bin"
        bindir.mkdir()
        docker = bindir / "docker"
        docker.write_text("""#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
if args[0] == "ps":
    if __import__("os").environ.get("FAKE_RUNNING"): print("running-container")
elif args[:2] == ["volume", "inspect"]:
    actual = args[2]
    project, name = actual.split("_", 1)
    print(json.dumps({"com.docker.compose.project":project, "com.docker.compose.volume":name}))
elif args[0] == "run":
    sys.stdout.buffer.write(b"fixture customer payload from a stopped volume")
else:
    sys.exit(2)
""", encoding="utf-8")
        age = bindir / "age"
        age.write_text("""#!/usr/bin/env python3
import hashlib, pathlib, sys
args = sys.argv[1:]
payload = sys.stdin.buffer.read()
pathlib.Path(args[args.index("-o")+1]).write_bytes(b"FAKE-CIPHER-FOR-TESTS:" + hashlib.sha256(payload).digest())
""", encoding="utf-8")
        docker.chmod(0o700)
        age.chmod(0o700)
        recipient = "age1" + "q" * 55
        image = "docker.io/library/busybox@sha256:" + "a" * 64
        with mock.patch.dict(os.environ, {"PATH": str(bindir) + ":" + os.environ["PATH"]}):
            with mock.patch.dict(os.environ, {"FAKE_RUNNING": "yes"}):
                with self.assertRaisesRegex(SNAP.SnapshotError, "running"):
                    SNAP.snapshot(self.compose, "acme-dev", self.root / "rejected", recipient, image)
            output = self.root / "encrypted"
            with mock.patch.object(SNAP, "assert_volume_exclusive",
                                   wraps=SNAP.assert_volume_exclusive) as guard:
                manifest = SNAP.snapshot(self.compose, "acme-dev", output, recipient, image)
            self.assertEqual(guard.call_count, 12)
        self.assertEqual(manifest["clientProject"], "acme-dev")
        self.assertEqual(len(manifest["volumes"]), 3)
        self.assertEqual((output / "manifest.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)
        for item in manifest["volumes"]:
            blob = output / item["object"]
            self.assertTrue(blob.read_bytes().startswith(b"FAKE-CIPHER-FOR-TESTS:"))
            self.assertNotIn(b"customer payload", blob.read_bytes())
            self.assertEqual(blob.stat().st_mode & 0o777, 0o600)
            self.assertEqual(hashlib.sha256(blob.read_bytes()).hexdigest(), item["sha256"])
        self.assertEqual(json.loads((output / "manifest.json").read_text()), manifest)

    def test_does_not_generate_manifest_on_failed_or_incomplete_encryption(self):
        with mock.patch.object(SNAP, "assert_quiescent"), mock.patch.object(SNAP, "assert_volume_owner",
                   side_effect=SNAP.SnapshotError("wrong client")), \
             mock.patch.object(SNAP.shutil, "which", return_value="/usr/bin/tool"):
            with self.assertRaisesRegex(SNAP.SnapshotError, "wrong client"):
                SNAP.snapshot(self.compose, "acme-dev", self.root / "invalid",
                              "age1" + "q"*55, "busybox@sha256:" + "a"*64)
        self.assertFalse((self.root / "invalid" / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()

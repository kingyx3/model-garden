"""Fail-closed evidence snapshot filesystem regressions for #117."""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "production-acceptance.py"
SPEC = importlib.util.spec_from_file_location("production_acceptance_fs", SCRIPT)
GATE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(GATE)


class EvidenceFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.now = dt.datetime(2026, 10, 29, tzinfo=dt.timezone.utc)
        self.rows = []
        for i, kind in enumerate(GATE.REQUIRED):
            content = f"redacted-{i}".encode()
            name = f"proof-{i}.json"
            (self.root / name).write_bytes(content)
            self.rows.append({"kind": kind, "observedAt": self.now.isoformat(),
                              "artifact": name, "sha256": hashlib.sha256(content).hexdigest(),
                              "source": "live", "result": "passed"})
        self.manifest = {"schemaVersion": 1, "client": "acme",
                         "environment": "dev", "gcpProject": "acme-dev",
                         "release": "0.8.224", "evidence": self.rows}

    def check(self, root=None):
        return GATE.check(self.manifest, root or self.root, client="acme",
                          environment="dev", project="acme-dev", release="0.8.224",
                          now=self.now)

    def test_private_regular_single_link_evidence_is_review_only(self):
        self.assertEqual(self.check()["status"], "review_required")

    def test_hardlinked_artifact_cannot_be_counted_as_evidence(self):
        original = self.root / self.rows[0]["artifact"]
        alias = self.root / "alias.json"
        os.link(original, alias)
        self.assertEqual(self.check()["status"], "blocked")

    def test_linked_evidence_directory_cannot_cross_tenant_boundary(self):
        link = self.root.parent / (self.root.name + "-alias")
        link.symlink_to(self.root, target_is_directory=True)
        self.addCleanup(link.unlink)
        self.assertEqual(self.check(link)["status"], "blocked")

    def test_writable_evidence_directory_is_not_a_trusted_snapshot(self):
        self.root.chmod(0o777)
        try:
            self.assertEqual(self.check()["status"], "blocked")
        finally:
            self.root.chmod(0o700)

    def test_artifact_replacement_between_path_checks_and_read_cannot_pass(self):
        first = self.root / self.rows[0]["artifact"]
        first.write_bytes(b"stale")
        # The expected bytes exist elsewhere. A path-based stat + read would
        # follow an attacker-substituted symlink and falsely accept the row.
        good = self.root / "attacker.json"
        good.write_bytes(b"redacted-0")
        original_read = pathlib.Path.read_bytes

        def swap_on_read(path):
            if path == first:
                first.unlink()
                first.symlink_to(good)
            return original_read(path)

        with mock.patch.object(pathlib.Path, "read_bytes", swap_on_read):
            self.assertEqual(self.check()["status"], "blocked")

    def test_hardlinked_manifest_is_refused_by_cli(self):
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps(self.manifest), encoding="utf-8")
        alias = self.root / "manifest-copy.json"
        os.link(manifest, alias)
        command = [sys.executable, str(SCRIPT), str(alias), "--client", "acme",
                   "--environment", "dev", "--gcp-project", "acme-dev",
                   "--release", "0.8.224"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "blocked")


if __name__ == "__main__":
    unittest.main()

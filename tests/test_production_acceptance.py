"""Adversarial regression tests for the production acceptance preflight."""
import datetime as dt
import hashlib
import importlib.util
import json
import pathlib
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "production-acceptance.py"
spec = importlib.util.spec_from_file_location("production_acceptance", SCRIPT)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class ProductionAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.now = dt.datetime(2026, 10, 28, tzinfo=dt.timezone.utc)
        self.rows = []
        for index, kind in enumerate(gate.REQUIRED):
            name = f"proof-{index}.json"
            payload = json.dumps({"kind": kind, "client": "acme", "environment": "prod"}).encode()
            (self.root / name).write_bytes(payload)
            self.rows.append({
                "kind": kind, "observedAt": self.now.isoformat(),
                "artifact": name, "sha256": hashlib.sha256(payload).hexdigest(),
                "source": "live", "result": "passed",
            })
        self.manifest = {
            "schemaVersion": 1, "client": "acme", "environment": "prod",
            "gcpProject": "acme-prod", "release": "0.8.192", "evidence": self.rows,
        }

    def check(self):
        return gate.check(self.manifest, self.root, client="acme", environment="prod",
                          project="acme-prod", release="0.8.192", now=self.now)

    def test_complete_is_review_required_never_approved(self):
        result = self.check()
        self.assertEqual(result["status"], "review_required")
        self.assertTrue(result["reviewRequired"])

    def test_missing_or_duplicate_evidence_blocks(self):
        self.rows.pop()
        self.assertIn("missing evidence: cost-attribution", self.check()["errors"])
        self.rows.append(dict(self.rows[0]))
        self.assertEqual(self.check()["status"], "blocked")

    def test_foreign_client_and_project_blocks(self):
        for field, value in (("client", "other"), ("gcpProject", "foreign-project"),
                             ("environment", "dev"), ("release", "0.8.191")):
            with self.subTest(field=field):
                old = self.manifest[field]
                self.manifest[field] = value
                self.assertEqual(self.check()["status"], "blocked")
                self.manifest[field] = old

    def test_simulated_or_failed_claim_blocks(self):
        for field, value in (("source", "mock"), ("result", "failed")):
            old = self.rows[0][field]
            self.rows[0][field] = value
            self.assertEqual(self.check()["status"], "blocked")
            self.rows[0][field] = old

    def test_stale_and_future_evidence_blocks(self):
        for offset in (-31, 1):
            self.rows[0]["observedAt"] = (self.now + dt.timedelta(days=offset)).isoformat()
            self.assertEqual(self.check()["status"], "blocked")

    def test_artifact_tamper_and_symlink_blocks(self):
        path = self.root / self.rows[0]["artifact"]
        path.write_bytes(b"tampered")
        self.assertEqual(self.check()["status"], "blocked")
        path.unlink()
        path.symlink_to(self.root / self.rows[1]["artifact"])
        self.assertEqual(self.check()["status"], "blocked")

    def test_path_traversal_and_unknown_field_blocks(self):
        self.rows[0]["artifact"] = "../other.json"
        self.assertEqual(self.check()["status"], "blocked")
        self.rows[0]["artifact"] = "proof-0.json"
        self.manifest["approved"] = True
        self.assertEqual(self.check()["status"], "blocked")

    def test_release_must_be_pinned_semver(self):
        for value in ("latest", "", "v0.8.192", "../0.8.192", "0.8.192-dev"):
            with self.subTest(release=value):
                self.manifest["release"] = value
                result = gate.check(self.manifest, self.root, client="acme", environment="prod",
                                    project="acme-prod", release=value, now=self.now)
                self.assertEqual(result["status"], "blocked")
        self.manifest["release"] = "0.8.192"

    def test_reused_artifact_cannot_satisfy_two_controls(self):
        self.rows[1]["artifact"] = self.rows[0]["artifact"]
        self.rows[1]["sha256"] = self.rows[0]["sha256"]
        self.assertEqual(self.check()["status"], "blocked")

    def test_naive_timestamps_and_boolean_schema_block(self):
        self.rows[0]["observedAt"] = "2026-10-28T00:00:00"
        self.assertEqual(self.check()["status"], "blocked")
        self.rows[0]["observedAt"] = self.now.isoformat()
        self.manifest["schemaVersion"] = True
        # Python bool == 1: require strict schema version type.
        self.assertEqual(self.check()["status"], "blocked")


if __name__ == "__main__":
    unittest.main()

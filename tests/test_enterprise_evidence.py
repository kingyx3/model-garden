import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("model_garden_enterprise_evidence", ROOT / "scripts" / "enterprise-evidence.py")
EVIDENCE_PACK = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(EVIDENCE_PACK)


class EnterpriseEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evidence = EVIDENCE_PACK.load_yaml(ROOT / "assurance" / "evidence.yaml")
        cls.singapore = EVIDENCE_PACK.load_yaml(ROOT / "assurance" / "singapore.yaml")
        cls.metadata = EVIDENCE_PACK.load_yaml(ROOT / "assurance" / "evidence-metadata.yaml")

    def test_scanner_summaries_are_conservative(self):
        pip_result = EVIDENCE_PACK.summarize_pip_audit(
            {"dependencies": [{"name": "safe", "vulns": []}, {"name": "bad", "vulns": [{"id": "CVE-X"}]}]}
        )
        self.assertEqual(pip_result["status"], "findings")
        self.assertEqual(pip_result["findings"], 1)

        bandit_result = EVIDENCE_PACK.summarize_bandit(
            {"results": [{"issue_severity": "HIGH", "issue_confidence": "HIGH"}]}
        )
        self.assertEqual(bandit_result["status"], "findings")
        self.assertEqual(bandit_result["severities"]["high"], 1)

        secret_result = EVIDENCE_PACK.summarize_detect_secrets(
            {"results": {"example.py": [{"type": "Secret Keyword"}]}}
        )
        self.assertEqual(secret_result["status"], "findings")
        self.assertEqual(secret_result["files_with_findings"], 1)

    def test_missing_scan_files_remain_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = EVIDENCE_PACK.collect_scan_results(pathlib.Path(tmp))
        self.assertTrue(results)
        self.assertTrue(all(result["status"] == "not_run" for result in results.values()))

    def test_pack_keeps_formal_assurance_gaps(self):
        scans = {
            scan_id: {"status": "pass", "findings": 0, "source": f"scans/{filename}"}
            for scan_id, filename in EVIDENCE_PACK.SCAN_FILES.items()
        }
        pack, manifest = EVIDENCE_PACK.build_pack(
            self.evidence,
            self.singapore,
            self.metadata,
            scans,
            generated_at="2026-09-17T00:00:00Z",
            commit="abc123",
            version="0.3.9",
        )
        controls = {row["id"]: row for row in manifest["controls"]}
        self.assertEqual(controls["penetration_testing"]["status"], "not_verified")
        self.assertEqual(controls["formal_certifications"]["status"], "not_verified")
        self.assertIn("not a certification", pack)
        self.assertIn("penetration_testing", manifest["non_implemented_controls"])

    def test_pack_writes_markdown_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            scan_dir = tmp_path / "scans"
            scan_dir.mkdir()
            (scan_dir / "pip-audit.json").write_text(json.dumps({"dependencies": []}), encoding="utf-8")
            scans = EVIDENCE_PACK.collect_scan_results(scan_dir)
            pack, manifest = EVIDENCE_PACK.build_pack(
                self.evidence,
                self.singapore,
                self.metadata,
                scans,
                generated_at="2026-09-17T00:00:00Z",
                commit="abc123",
                version="0.3.9",
            )
            self.assertIn("Model Garden Enterprise Evidence Pack", pack)
            self.assertEqual(manifest["repository_commit"], "abc123")
            self.assertEqual(manifest["scans"]["dependency_vulnerability_scan"]["status"], "pass")


if __name__ == "__main__":
    unittest.main()

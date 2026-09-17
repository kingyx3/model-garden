import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("model_garden_enterprise_evidence", ROOT / "scripts" / "enterprise-evidence.py")
EVIDENCE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(EVIDENCE)


class EnterpriseEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = EVIDENCE.load_yaml(ROOT / "assurance" / "evidence.yaml")
        cls.singapore = EVIDENCE.load_yaml(ROOT / "assurance" / "singapore.yaml")

    def test_registry_references_existing_evidence(self):
        result = EVIDENCE.evidence_validation(self.registry)
        self.assertTrue(result["ok"], result["errors"])

    def test_secret_scan_has_no_high_confidence_secret(self):
        result = EVIDENCE.scan_secrets()
        self.assertEqual(result["status"], "pass", result["findings"])

    def test_iac_guardrails_do_not_expose_world_cidr(self):
        result = EVIDENCE.scan_iac()
        self.assertEqual(result["status"], "pass", result["findings"])

    def test_sbom_includes_runtime_dependency(self):
        sbom = EVIDENCE.build_sbom()
        names = {component["name"] for component in sbom["components"]}
        self.assertIn("mcp", names)
        self.assertEqual(sbom["bomFormat"], "CycloneDX")

    def test_pack_keeps_unverified_controls_visible(self):
        manifest = EVIDENCE.build_manifest(self.registry, self.singapore)
        text = EVIDENCE.render_pack(self.registry, self.singapore, manifest)
        self.assertIn("penetration_testing", text)
        self.assertIn("not_verified", text)
        self.assertIn("not a blanket compliance attestation", text)

    def test_pack_writes_machine_and_human_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = pathlib.Path(tmp)
            manifest = EVIDENCE.generate_pack(output, None)
            self.assertTrue(manifest["registry_validation"]["ok"])
            self.assertTrue((output / "enterprise-evidence-pack.md").exists())
            self.assertTrue((output / "evidence-manifest.json").exists())
            self.assertTrue((output / "sbom.cdx.json").exists())
            saved = json.loads((output / "evidence-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["model_garden_version"], (ROOT / "VERSION").read_text(encoding="utf-8").strip())


if __name__ == "__main__":
    unittest.main()

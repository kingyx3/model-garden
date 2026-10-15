"""Credential-free checks that technical docs cannot contradict the integration registry."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("status_consistency_under_test", ROOT / "platform/core/status_consistency.py")
status = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(status)


class StatusConsistencyTests(unittest.TestCase):
    def repo(self, root: pathlib.Path, doc: str = "# Docs\n") -> pathlib.Path:
        shutil.copytree(ROOT / "contracts", root / "contracts")
        shutil.copytree(ROOT / "platform", root / "platform", ignore=shutil.ignore_patterns("__pycache__"))
        (root / "docs").mkdir()
        (root / "README.md").write_text("# Model Garden\n", encoding="utf-8")
        (root / "docs" / "contracts.md").write_text(doc, encoding="utf-8")
        return root

    def errors(self, doc: str) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            return status.check(self.repo(pathlib.Path(tmp), doc))

    def test_the_repository_docs_agree_with_the_registry(self):
        self.assertEqual(status.check(ROOT), [])

    def test_the_command_passes_on_the_repository(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/check-status-consistency.py")],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_calling_a_built_provider_planned_or_unimplemented_is_reported_with_its_location(self):
        for sentence in (
            "The `built-in-crm` log is not yet implemented.",
            "Text Knowledge (`text-knowledge`) is NEXT on the roadmap.",
            "Google Calendar is planned.",
        ):
            errors = self.errors(f"# Docs\n\n{sentence}\n")
            self.assertEqual(len(errors), 1, (sentence, errors))
            self.assertIn("docs/contracts.md:3", errors[0])

    def test_a_stale_claim_about_the_reference_receptionist_is_reported(self):
        stale = "**Not yet delivered:** the reference receptionist does not yet declare `kb` and `crm` as required capabilities."
        with tempfile.TemporaryDirectory() as tmp:
            root = self.repo(pathlib.Path(tmp), f"# Docs\n\n{stale}\n")
            shutil.copytree(ROOT / "examples", root / "examples")
            errors = status.check(root)
            self.assertEqual(len(errors), 1, errors)
            self.assertIn("docs/contracts.md:3", errors[0])
            # Once the workspace no longer requires them, the same sentence is accurate again.
            for skill in (root / "examples/workspace/skills").rglob("skill.yaml"):
                skill.write_text(skill.read_text(encoding="utf-8").replace("kb", "xx").replace("crm", "yy"), encoding="utf-8")
            self.assertEqual(status.check(root), [])

    def test_wording_about_planned_providers_on_the_same_line_is_not_a_false_alarm(self):
        line = "Only the Tier 0 `built-in-crm` log implements this; HubSpot is planned and `salesforce` does not yet."
        self.assertEqual(self.errors(f"# Docs\n\n{line}\n"), [])

    def test_calling_an_unproven_provider_live_is_reported_but_honest_negations_are_not(self):
        self.assertTrue(self.errors("# Docs\n\nGoogle Calendar is live.\n"))
        self.assertTrue(self.errors("# Docs\n\nThe `built-in-crm` provider is production-live.\n"))
        for honest in (
            "Google Calendar is not proven production-live until the OAuth gates pass.",
            "No provider is production-live; `text-knowledge` is credential-free build evidence only.",
        ):
            self.assertEqual(self.errors(f"# Docs\n\n{honest}\n"), [], honest)

    def test_a_tier_zero_provider_marked_live_without_proof_or_planned_is_rejected_in_the_registry(self):
        for change in ({"status": "planned"}, {"tier": 1, "method": "native"}, {"family": "kb"}):
            with tempfile.TemporaryDirectory() as tmp:
                root = self.repo(pathlib.Path(tmp))
                path = root / "contracts/v1/integrations.json"
                registry = json.loads(path.read_text(encoding="utf-8"))
                for entry in registry["providers"]:
                    if entry["id"] == "built-in-crm":
                        entry.update(change)
                path.write_text(json.dumps(registry), encoding="utf-8")
                try:
                    errors = status.check(root)
                except ValueError:
                    continue  # the registry's own validation already refuses an inconsistent entry
                self.assertTrue(any("built-in-crm" in e for e in errors), (change, errors))

    def test_registry_directory_renders_every_provider_so_it_can_replace_a_manual_table(self):
        registry = status._load_registry(ROOT)
        spec = importlib.util.spec_from_file_location("integrations_for_status_test", ROOT / "platform/core/integrations.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rendered = module.render_directory(registry)
        for provider in registry["providers"]:
            self.assertIn(provider["name"], rendered)


if __name__ == "__main__":
    unittest.main()

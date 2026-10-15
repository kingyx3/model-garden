from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "eval-receptionist.py"
spec = importlib.util.spec_from_file_location("eval_receptionist", SCRIPT)
evals = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(evals)


class ReceptionistEvalTests(unittest.TestCase):
    def test_reference_receptionist_passes_acceptance_suite(self):
        failures = evals.evaluate(ROOT / "examples" / "workspace", ROOT / "evals" / "receptionist" / "scenarios.yaml")
        self.assertEqual(failures, [])

    def test_missing_safety_instruction_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "workspace"
            import shutil
            shutil.copytree(ROOT / "examples" / "workspace", workspace)
            instructions = next((workspace / "agents").rglob("receptionist/instructions.md"))
            instructions.write_text("# Receptionist\nAnswer calls.\n", encoding="utf-8")
            failures = evals.evaluate(workspace, ROOT / "evals" / "receptionist" / "scenarios.yaml")
            self.assertTrue(any("approved knowledge" in failure for failure in failures))
            self.assertTrue(any("human fallback" in failure for failure in failures))

    def copy_workspace(self, tmp: str) -> pathlib.Path:
        import shutil
        workspace = pathlib.Path(tmp) / "workspace"
        shutil.copytree(ROOT / "examples" / "workspace", workspace)
        return workspace

    def test_retrieval_expectation_fails_when_the_knowledge_source_loses_the_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.copy_workspace(tmp)
            source = workspace / "knowledge" / "rules-of-engagement.md"
            source.write_text("# Rules\n\n## Unrelated\nNothing useful here.\n", encoding="utf-8")
            failures = evals.evaluate(workspace, ROOT / "evals" / "receptionist" / "scenarios.yaml")
            self.assertTrue(any(f.startswith("faq-known:") and "top 3" in f for f in failures), failures)

    def test_missing_knowledge_binding_is_reported_as_a_failure_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.copy_workspace(tmp)
            agent = workspace / "agents/front-office/receptionist/agent.yaml"
            text = agent.read_text(encoding="utf-8").replace("kb.search", "calendar.availability", 1)
            agent.write_text(text, encoding="utf-8")
            failures = evals.evaluate(workspace, ROOT / "evals" / "receptionist" / "scenarios.yaml")
            self.assertTrue(failures)


if __name__ == "__main__":
    unittest.main()

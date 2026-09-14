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


if __name__ == "__main__":
    unittest.main()

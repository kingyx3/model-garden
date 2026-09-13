from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPILER_PATH = ROOT / "scripts" / "compile-workspace.py"
ADAPTER_PATH = ROOT / "scripts" / "provision-hermes-profile.py"

compiler_spec = importlib.util.spec_from_file_location("compile_workspace", COMPILER_PATH)
compiler = importlib.util.module_from_spec(compiler_spec)
assert compiler_spec.loader is not None
compiler_spec.loader.exec_module(compiler)

adapter_spec = importlib.util.spec_from_file_location("provision_hermes_profile", ADAPTER_PATH)
adapter = importlib.util.module_from_spec(adapter_spec)
assert adapter_spec.loader is not None
adapter_spec.loader.exec_module(adapter)


class ProvisionHermesProfileTests(unittest.TestCase):
    def receptionist_desired_state(self) -> dict:
        return compiler.compile_workspace(ROOT / "examples" / "workspace", "receptionist")[0]

    def test_materializes_disposable_profile_without_secrets(self) -> None:
        desired_state = self.receptionist_desired_state()
        files = adapter.build_profile_files(desired_state)

        self.assertIn(pathlib.Path("SOUL.md"), files)
        self.assertIn(pathlib.Path("config.yaml"), files)
        self.assertIn(pathlib.Path(".modelgarden/desired-state.json"), files)
        self.assertNotIn(pathlib.Path(".env"), files)

        config = yaml.safe_load(files[pathlib.Path("config.yaml")])
        self.assertEqual(config["model"]["provider"], "openai")
        self.assertEqual(config["model"]["default"], "approved-openai-model")

        skill_path = pathlib.Path("skills/model-garden/book-appointment/SKILL.md")
        self.assertIn(skill_path, files)
        self.assertIn("name: book-appointment", files[skill_path])
        self.assertIn("Do not invent availability", files[skill_path])
        self.assertIn("calendar.book", files[skill_path])

    def test_apply_is_idempotent(self) -> None:
        files = adapter.build_profile_files(self.receptionist_desired_state())
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = pathlib.Path(tmp) / "receptionist"
            first = adapter.apply_profile(profile_dir, files)
            second = adapter.apply_profile(profile_dir, files)

            self.assertGreater(len(first), 0)
            self.assertEqual(second, [])
            self.assertTrue((profile_dir / "SOUL.md").is_file())
            self.assertTrue((profile_dir / "skills/model-garden/take-message/SKILL.md").is_file())
            self.assertFalse((profile_dir / ".env").exists())

            persisted = json.loads((profile_dir / ".modelgarden/desired-state.json").read_text())
            self.assertEqual(persisted["source"]["name"], "receptionist")

    def test_dry_run_does_not_write(self) -> None:
        files = adapter.build_profile_files(self.receptionist_desired_state())
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = pathlib.Path(tmp) / "receptionist"
            changed = adapter.apply_profile(profile_dir, files, dry_run=True)

            self.assertGreater(len(changed), 0)
            self.assertFalse(profile_dir.exists())

    def test_rejects_unsafe_skill_name(self) -> None:
        desired_state = self.receptionist_desired_state()
        desired_state["skills"][0]["metadata"]["name"] = "../../escape"

        with self.assertRaisesRegex(ValueError, "unsupported path characters"):
            adapter.build_profile_files(desired_state)

    def test_rejects_multi_agent_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "desired.json"
            path.write_text(json.dumps({"schemaVersion": 1, "agents": []}))

            with self.assertRaisesRegex(ValueError, "single-Agent desired state"):
                adapter.load_desired_state(path)


if __name__ == "__main__":
    unittest.main()

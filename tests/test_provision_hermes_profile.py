from __future__ import annotations

import copy
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

    def test_materializes_disposable_profile_with_only_selected_governed_mcp_tools(self) -> None:
        desired_state = self.receptionist_desired_state()
        files = adapter.build_profile_files(desired_state)

        self.assertIn(pathlib.Path("SOUL.md"), files)
        self.assertIn(pathlib.Path("config.yaml"), files)
        self.assertIn(pathlib.Path(".modelgarden/desired-state.json"), files)
        self.assertNotIn(pathlib.Path(".env"), files)

        config = yaml.safe_load(files[pathlib.Path("config.yaml")])
        self.assertEqual(config["model"]["provider"], "openai")
        self.assertEqual(config["model"]["default"], "approved-openai-model")
        governed = config["mcp_servers"]["model_garden"]
        self.assertEqual(governed["url"], "http://governed-tools:9120/mcp")
        self.assertTrue(governed["enabled"])
        self.assertFalse(governed["supports_parallel_tool_calls"])
        self.assertEqual(
            governed["tools"]["include"],
            ["calendar_availability", "calendar_book"],
        )
        self.assertFalse(governed["tools"]["resources"])
        self.assertFalse(governed["tools"]["prompts"])
        self.assertNotIn("token", json.dumps(config).lower())
        self.assertNotIn("credential", json.dumps(config).lower())

        skill_path = pathlib.Path("skills/model-garden/book-appointment/SKILL.md")
        self.assertIn(skill_path, files)
        self.assertIn("name: book-appointment", files[skill_path])
        self.assertIn("Do not invent availability", files[skill_path])
        self.assertIn("calendar.availability", files[skill_path])
        self.assertIn("calendar.book", files[skill_path])

    def test_profile_without_tools_has_no_mcp_server(self) -> None:
        desired_state = self.receptionist_desired_state()
        desired_state["tools"] = []
        for skill in desired_state["skills"]:
            if skill.get("metadata", {}).get("name") == "book-appointment":
                skill["spec"]["allowedTools"] = []
        files = adapter.build_profile_files(desired_state)
        config = yaml.safe_load(files[pathlib.Path("config.yaml")])
        self.assertNotIn("mcp_servers", config)

    def test_selected_tool_without_runtime_implementation_fails_closed(self) -> None:
        desired_state = copy.deepcopy(self.receptionist_desired_state())
        desired_state["tools"].append(
            {
                "apiVersion": "modelgarden.ai/v1",
                "kind": "Tool",
                "metadata": {"name": "crm.write", "owner": "ai-platform"},
                "spec": {"type": "internal", "risk": "write", "approvalRequired": True},
            }
        )
        with self.assertRaisesRegex(ValueError, "no governed runtime implementation.*crm.write"):
            adapter.build_profile_files(desired_state)

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

    def test_apply_removes_only_stale_model_garden_managed_skills(self) -> None:
        desired_state = self.receptionist_desired_state()
        files = adapter.build_profile_files(desired_state)
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = pathlib.Path(tmp) / "receptionist"
            adapter.apply_profile(profile_dir, files)
            stale = profile_dir / "skills/model-garden/take-message/SKILL.md"
            client_skill = profile_dir / "skills/client-owned/local/SKILL.md"
            client_skill.parent.mkdir(parents=True, exist_ok=True)
            client_skill.write_text("client-owned\n", encoding="utf-8")
            self.assertTrue(stale.is_file())

            reduced = copy.deepcopy(desired_state)
            reduced["skills"] = [
                skill for skill in reduced["skills"]
                if skill.get("metadata", {}).get("name") != "take-message"
            ]
            reduced["skillInstructions"].pop("take-message", None)
            changed = adapter.apply_profile(profile_dir, adapter.build_profile_files(reduced))

            self.assertIn(pathlib.Path("skills/model-garden/take-message/SKILL.md"), changed)
            self.assertFalse(stale.exists())
            self.assertTrue(client_skill.is_file())

    def test_dry_run_reports_stale_managed_skill_without_deleting_it(self) -> None:
        desired_state = self.receptionist_desired_state()
        files = adapter.build_profile_files(desired_state)
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = pathlib.Path(tmp) / "receptionist"
            adapter.apply_profile(profile_dir, files)
            stale = profile_dir / "skills/model-garden/take-message/SKILL.md"

            reduced = copy.deepcopy(desired_state)
            reduced["skills"] = [
                skill for skill in reduced["skills"]
                if skill.get("metadata", {}).get("name") != "take-message"
            ]
            reduced["skillInstructions"].pop("take-message", None)
            changed = adapter.apply_profile(profile_dir, adapter.build_profile_files(reduced), dry_run=True)

            self.assertIn(pathlib.Path("skills/model-garden/take-message/SKILL.md"), changed)
            self.assertTrue(stale.is_file())

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

from __future__ import annotations

import importlib.util
import pathlib
import shutil
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "compile-workspace.py"
spec = importlib.util.spec_from_file_location("compile_workspace", MODULE_PATH)
compiler = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(compiler)


class CompileWorkspaceTests(unittest.TestCase):
    def test_reference_workspace_compiles_deterministically(self) -> None:
        workspace = ROOT / "examples" / "workspace"
        first = compiler.compile_workspace(workspace, "account-researcher")
        second = compiler.compile_workspace(workspace, "account-researcher")
        self.assertEqual(first, second)
        compiled = first[0]
        self.assertEqual(compiled["source"]["name"], "account-researcher")
        self.assertEqual(compiled["modelProfile"]["metadata"]["name"], "reasoning.high")
        self.assertEqual([x["metadata"]["name"] for x in compiled["skills"]], ["account-summary"])
        self.assertEqual([x["metadata"]["name"] for x in compiled["tools"]], ["crm.read"])
        self.assertEqual([x["metadata"]["name"] for x in compiled["knowledge"]], ["sales-playbook"])
        self.assertIn("account brief", compiled["instructions"]["content"])
        self.assertIn("account-summary", compiled["skillInstructions"])

    def test_receptionist_materializes_skill_instructions(self) -> None:
        workspace = ROOT / "examples" / "workspace"
        compiled = compiler.compile_workspace(workspace, "receptionist")[0]
        self.assertEqual(sorted(compiled["skillInstructions"]), ["answer-faq", "book-appointment", "human-handoff", "take-message"])
        self.assertIn("Do not invent availability", compiled["skillInstructions"]["book-appointment"]["content"])
        self.assertTrue(compiled["skillInstructions"]["book-appointment"]["source"].endswith("instructions.md"))

    def test_missing_reference_fails_closed(self) -> None:
        source = ROOT / "examples" / "workspace"
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "workspace"
            shutil.copytree(source, workspace)
            agent_path = workspace / "agents" / "sales" / "account-researcher" / "agent.yaml"
            agent = yaml.safe_load(agent_path.read_text())
            agent["spec"]["tools"] = ["missing.tool"]
            agent_path.write_text(yaml.safe_dump(agent, sort_keys=False))
            with self.assertRaisesRegex(ValueError, "missing Tool 'missing.tool'"):
                compiler.compile_workspace(workspace, "account-researcher")

    def test_instruction_path_cannot_escape_workspace(self) -> None:
        source = ROOT / "examples" / "workspace"
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "workspace"
            shutil.copytree(source, workspace)
            agent_path = workspace / "agents" / "sales" / "account-researcher" / "agent.yaml"
            agent = yaml.safe_load(agent_path.read_text())
            agent["spec"]["instructions"]["file"] = "../../../../../outside.md"
            agent_path.write_text(yaml.safe_dump(agent, sort_keys=False))
            with self.assertRaisesRegex(ValueError, "instruction path escapes workspace"):
                compiler.compile_workspace(workspace, "account-researcher")

    def test_skill_cannot_require_tool_agent_did_not_select(self) -> None:
        source = ROOT / "examples" / "workspace"
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "workspace"
            shutil.copytree(source, workspace)
            agent_path = workspace / "agents" / "front-office" / "receptionist" / "agent.yaml"
            agent = yaml.safe_load(agent_path.read_text())
            agent["spec"]["tools"] = []
            agent_path.write_text(yaml.safe_dump(agent, sort_keys=False))
            with self.assertRaisesRegex(ValueError, "Skill 'book-appointment' requires Tool 'calendar.book', but Agent 'receptionist' did not select it"):
                compiler.compile_workspace(workspace, "receptionist")

    def test_missing_client_resources_resolve_from_library_without_copying(self) -> None:
        source = ROOT / "examples" / "workspace"
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "workspace"
            library = pathlib.Path(tmp) / "library"
            shutil.copytree(source, workspace)
            library.mkdir()
            shutil.copytree(workspace / "skills" / "book-appointment", library / "skills" / "book-appointment")
            (library / "tools").mkdir()
            shutil.copy2(workspace / "tools" / "calendar-book.yaml", library / "tools" / "calendar-book.yaml")
            shutil.rmtree(workspace / "skills" / "book-appointment")
            (workspace / "tools" / "calendar-book.yaml").unlink()

            compiled = compiler.compile_workspace(workspace, "receptionist", (library,))[0]

            self.assertIn("book-appointment", compiled["skillInstructions"])
            self.assertEqual(compiled["skillInstructions"]["book-appointment"]["source"], "library:skills/book-appointment/instructions.md")
            self.assertEqual([tool["metadata"]["name"] for tool in compiled["tools"]], ["calendar.book"])

    def test_client_local_resource_overrides_library_resource(self) -> None:
        source = ROOT / "examples" / "workspace"
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "workspace"
            library = pathlib.Path(tmp) / "library"
            shutil.copytree(source, workspace)
            shutil.copytree(workspace / "skills" / "answer-faq", library / "skills" / "answer-faq")
            library_instruction = library / "skills" / "answer-faq" / "instructions.md"
            library_instruction.write_text("# Wrong library version\n")

            compiled = compiler.compile_workspace(workspace, "receptionist", (library,))[0]

            self.assertNotIn("Wrong library version", compiled["skillInstructions"]["answer-faq"]["content"])
            self.assertEqual(compiled["skillInstructions"]["answer-faq"]["source"], "skills/answer-faq/instructions.md")


if __name__ == "__main__":
    unittest.main()

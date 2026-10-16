"""Credential-free checks for Skillset bundles: validation, fail-closed expansion and per-coworker activation."""
from __future__ import annotations

import importlib.util
import pathlib
import shutil
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


skillsets = _load("skillsets_under_test", "platform/core/skillsets.py")
compiler = _load("compiler_for_skillsets", "scripts/compile-workspace.py")
Error = skillsets.SkillsetError

AVAILABLE = {"answer-faq": "1.0.0", "take-message": "1.0.0", "human-handoff": "1.0.0", "book-appointment": "1.0.0", "log-conversation": "1.0.0"}
TOOLS = {
    "answer-faq": ["kb.search"], "take-message": ["crm.create"], "human-handoff": [],
    "book-appointment": ["calendar.availability", "calendar.book"], "log-conversation": ["crm.log-interaction"],
}


def bundle(name="front-desk", version="1.0.0", skills=None, includes=None, **extra):
    document = {"name": name, "version": version, "skills": skills if skills is not None else list(AVAILABLE), **extra}
    if includes is not None:
        document["includes"] = includes
    return document


def catalog(*documents):
    result: dict = {}
    for document in documents:
        result.setdefault(document["name"], {})[document["version"]] = document
    return result


FRONT = {"name": "front-desk", "version": "1.0.0"}


class ValidationCase(unittest.TestCase):
    def test_a_valid_bundle_is_normalised(self):
        normal = skillsets.validate_skillset(bundle(skills=["answer-faq", {"name": "take-message", "version": "1.0.0"}],
                                                    provenance={"source": "model-garden/library", "pin": "abc123"}, description="x"))
        self.assertEqual(normal["skills"], [{"name": "answer-faq"}, {"name": "take-message", "version": "1.0.0"}])
        self.assertEqual(normal["provenance"], {"source": "model-garden/library", "pin": "abc123"})

    def test_invalid_bundles_are_refused(self):
        bad = [
            "not an object", bundle(name="Front Desk"), bundle(version="1"), bundle(skills=[]),
            bundle(skills=["answer-faq", "answer-faq"]), bundle(skills=[{"name": "answer-faq", "version": "latest"}]),
            bundle(skills=[{"name": "answer-faq", "extra": 1}]), bundle(skills=[42]), bundle(includes=["a", "a"]),
            bundle(description="x" * 501), bundle(provenance="local"), bundle(unknown="field"),
            {**bundle(), "includes": "front-desk"},
        ]
        for document in bad:
            with self.subTest(document=document), self.assertRaises(Error):
                skillsets.validate_skillset(document)

    def test_a_bundle_cannot_carry_credentials(self):
        for key in ("token", "api_key", "client_secret", "password", "private-key", "credentials"):
            with self.subTest(key=key), self.assertRaisesRegex(Error, "credentials"):
                skillsets.validate_skillset({**bundle(), key: "x"})
        with self.assertRaisesRegex(Error, "credentials"):
            skillsets.validate_skillset(bundle(skills=[{"name": "answer-faq", "token": "x"}]))


class ExpansionCase(unittest.TestCase):
    def test_direct_and_bundled_selection_expand_to_one_sorted_deduplicated_list(self):
        library = catalog(bundle(skills=["answer-faq", "take-message"]))
        rows = skillsets.resolve({"skills": ["take-message", "log-conversation"], "skillsets": [FRONT]}, library, AVAILABLE)
        self.assertEqual([row["skill"] for row in rows], ["answer-faq", "log-conversation", "take-message"])
        by_name = {row["skill"]: row["via"] for row in rows}
        self.assertEqual(by_name["take-message"], ["direct", "front-desk"])
        self.assertEqual(by_name["answer-faq"], ["front-desk"])
        again = skillsets.resolve({"skills": ["log-conversation", "take-message"], "skillsets": [FRONT]}, library, AVAILABLE)
        self.assertEqual(rows, again)  # deterministic whatever the order of the selection

    def test_includes_expand_and_a_skill_reached_twice_is_listed_once(self):
        library = catalog(
            bundle(name="core", skills=["answer-faq", "take-message"]),
            bundle(name="front-desk", skills=["take-message", "human-handoff"], includes=["core"]),
        )
        rows = skillsets.resolve({"skillsets": [FRONT]}, library, AVAILABLE)
        self.assertEqual([row["skill"] for row in rows], ["answer-faq", "human-handoff", "take-message"])
        self.assertEqual({row["skill"]: row["via"] for row in rows}["take-message"], ["core", "front-desk"])

    def test_missing_references_fail_closed(self):
        library = catalog(bundle(skills=["answer-faq", "unknown-skill"]))
        cases = [
            ({"skillsets": [FRONT]}, library, "not available"),
            ({"skillsets": [{"name": "nope", "version": "1.0.0"}]}, library, "not in the catalog"),
            ({"skillsets": [{"name": "front-desk", "version": "9.9.9"}]}, library, "no version"),
            ({"skillsets": [{"name": "front-desk"}]}, library, "no version"),
            ({"skills": ["unknown-skill"]}, {}, "not available"),
            ({"skillsets": [FRONT]}, catalog(bundle(includes=["ghost"], skills=["answer-faq"])), "not in the catalog"),
        ]
        for selection, library_under_test, message in cases:
            with self.subTest(selection=selection), self.assertRaisesRegex(Error, message):
                skillsets.resolve(selection, library_under_test, AVAILABLE)

    def test_an_incompatible_or_conflicting_pin_fails_closed(self):
        pinned = catalog(bundle(skills=[{"name": "answer-faq", "version": "2.0.0"}]))
        with self.assertRaisesRegex(Error, "pins Skill answer-faq@2.0.0"):
            skillsets.resolve({"skillsets": [FRONT]}, pinned, AVAILABLE)
        two = catalog(
            bundle(name="a-set", skills=[{"name": "answer-faq", "version": "1.0.0"}]),
            bundle(name="b-set", skills=[{"name": "answer-faq", "version": "2.0.0"}]),
        )
        with self.assertRaises(Error):  # b-set's pin is not the available version, so it is refused
            skillsets.resolve({"skillsets": [{"name": "a-set", "version": "1.0.0"}, {"name": "b-set", "version": "1.0.0"}]}, two,
                              {**AVAILABLE, "answer-faq": "1.0.0"})

    def test_pins_that_agree_are_accepted(self):
        library = catalog(bundle(name="a-set", skills=[{"name": "answer-faq", "version": "1.0.0"}]),
                          bundle(name="b-set", skills=[{"name": "answer-faq", "version": "1.0.0"}]))
        rows = skillsets.resolve({"skillsets": [{"name": "a-set", "version": "1.0.0"}, {"name": "b-set", "version": "1.0.0"}]}, library, AVAILABLE)
        self.assertEqual(rows[0]["via"], ["a-set", "b-set"])

    def test_cycles_and_excessive_nesting_are_refused(self):
        loop = catalog(bundle(name="a-set", skills=["answer-faq"], includes=["b-set"]), bundle(name="b-set", skills=["take-message"], includes=["a-set"]))
        with self.assertRaisesRegex(Error, "cycle"):
            skillsets.resolve({"skillsets": [{"name": "a-set", "version": "1.0.0"}]}, loop, AVAILABLE)
        itself = catalog(bundle(name="a-set", skills=["answer-faq"], includes=["a-set"]))
        with self.assertRaisesRegex(Error, "cycle"):
            skillsets.resolve({"skillsets": [{"name": "a-set", "version": "1.0.0"}]}, itself, AVAILABLE)
        chain = catalog(*[bundle(name=f"s{n}", skills=["answer-faq"], includes=[f"s{n + 1}"]) for n in range(6)], bundle(name="s6", skills=["answer-faq"]))
        with self.assertRaisesRegex(Error, "deep"):
            skillsets.resolve({"skillsets": [{"name": "s0", "version": "1.0.0"}]}, chain, AVAILABLE)

    def test_an_include_with_several_versions_is_ambiguous_and_refused(self):
        library = catalog(bundle(name="core", skills=["answer-faq"]), bundle(name="core", version="2.0.0", skills=["answer-faq"]),
                          bundle(skills=["take-message"], includes=["core"]))
        with self.assertRaisesRegex(Error, "several versions"):
            skillsets.resolve({"skillsets": [FRONT]}, library, AVAILABLE)

    def test_a_catalog_entry_that_does_not_match_its_document_is_refused(self):
        with self.assertRaises(Error):
            skillsets.resolve({}, {"front-desk": {"1.0.0": bundle(version="2.0.0")}}, AVAILABLE)

    def test_nothing_is_partly_expanded_when_one_reference_fails(self):
        library = catalog(bundle(skills=["answer-faq", "unknown-skill"]))
        with self.assertRaises(Error):
            skillsets.resolve({"skills": ["take-message"], "skillsets": [FRONT]}, library, AVAILABLE)  # raises; no partial result exists


class ActivationCase(unittest.TestCase):
    def effective(self):
        return skillsets.resolve({"skillsets": [FRONT]}, catalog(bundle()), AVAILABLE)

    def test_selecting_a_bundle_enables_nothing(self):
        states = skillsets.activation(self.effective())
        self.assertTrue(all(row["enabled"] is False for row in states))
        self.assertEqual(skillsets.exposed_tools(states, TOOLS), [])

    def test_one_member_can_be_on_while_every_other_member_is_off(self):
        states = skillsets.activation(self.effective(), ["answer-faq"])
        self.assertEqual([row["skill"] for row in states if row["enabled"]], ["answer-faq"])
        self.assertEqual(skillsets.exposed_tools(states, TOOLS), ["kb.search"])  # no calendar, CRM or other member's Tool
        both = skillsets.activation(self.effective(), ["answer-faq", "book-appointment"])
        self.assertEqual(skillsets.exposed_tools(both, TOOLS), ["calendar.availability", "calendar.book", "kb.search"])

    def test_enabling_an_unselected_skill_is_refused(self):
        selected = skillsets.resolve({"skills": ["answer-faq"]}, {}, AVAILABLE)
        with self.assertRaisesRegex(Error, "not selected"):
            skillsets.activation(selected, ["book-appointment"])

    def test_the_same_bundle_resolves_independently_for_two_coworkers(self):
        library = catalog(bundle())
        reception = skillsets.activation(skillsets.resolve({"skillsets": [FRONT]}, library, AVAILABLE), ["answer-faq", "take-message"])
        without_calendar = {name: version for name, version in AVAILABLE.items() if name != "book-appointment"}
        with self.assertRaisesRegex(Error, "not available"):  # this coworker's workspace lacks a member: fails closed for it alone
            skillsets.resolve({"skillsets": [FRONT]}, library, without_calendar)
        sales = skillsets.activation(skillsets.resolve({"skillsets": [FRONT]}, library, AVAILABLE), ["log-conversation"])
        self.assertEqual(skillsets.exposed_tools(reception, TOOLS), ["crm.create", "kb.search"])
        self.assertEqual(skillsets.exposed_tools(sales, TOOLS), ["crm.log-interaction"])

    def test_compile_fields_map_to_the_existing_skills_and_disabled_skills(self):
        states = skillsets.activation(self.effective(), ["answer-faq", "take-message"])
        fields = skillsets.compile_fields(states)
        self.assertEqual(fields["skills"], sorted(AVAILABLE))
        self.assertEqual(fields["disabledSkills"], ["book-appointment", "human-handoff", "log-conversation"])


class CompilerIntegrationCase(unittest.TestCase):
    """The expanded selection drives the real compiler, which keeps disabled members out and fails closed."""

    ZERO_STACK = {"environment": "dev", "connectors": {"kb": {"provider": "text-knowledge", "tier": 0}, "crm": {"provider": "built-in-crm", "tier": 0}}}

    def compile_with(self, enabled):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "client"
            shutil.copytree(ROOT / "examples/workspace", workspace)
            path = workspace / "agents/front-office/receptionist/agent.yaml"
            agent = yaml.safe_load(path.read_text())
            states = skillsets.activation(skillsets.resolve({"skillsets": [FRONT]}, catalog(bundle()), AVAILABLE), enabled)
            agent["spec"].update(skillsets.compile_fields(states))
            path.write_text(yaml.safe_dump(agent, sort_keys=False))
            return compiler.compile_workspace(workspace, "receptionist", environment_binding=self.ZERO_STACK)[0]

    def test_only_enabled_members_are_compiled_and_calendar_tools_stay_out(self):
        desired = self.compile_with(["answer-faq", "take-message", "human-handoff", "log-conversation"])
        self.assertEqual({skill["metadata"]["name"] for skill in desired["skills"]}, {"answer-faq", "take-message", "human-handoff", "log-conversation"})
        self.assertNotIn("calendar.book", [tool["metadata"]["name"] for tool in desired["tools"]])

    def test_enabling_a_member_whose_dependency_is_missing_still_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "book-appointment"):
            self.compile_with(list(AVAILABLE))  # a bundle cannot make a calendar appear



class WorkspaceSkillsetIntegrationCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = pathlib.Path(self.temp.name) / "workspace"
        shutil.copytree(ROOT / "examples" / "workspace", self.workspace)
        self.agent_file = self.workspace / "agents/front-office/receptionist/agent.yaml"
        self.bundle_path = self.workspace / "skillsets/front-office.yaml"
        self.bundle_path.parent.mkdir(parents=True)
        self.bundle_path.write_text(yaml.safe_dump({
            "apiVersion": "modelgarden.ai/v1", "kind": "Skillset",
            "metadata": {"name": "front-office", "owner": "front-office", "version": "1.0.0"},
            "spec": {"skills": [{"name": "answer-faq", "version": "1.1.0"}, "book-appointment", "log-conversation"]}
        }, sort_keys=False))

    def configure(self, *, direct=(), enabled=(), disabled=(), bundle=True):
        agent = yaml.safe_load(self.agent_file.read_text())
        agent["spec"]["skills"] = list(direct)
        agent["spec"]["skillsets"] = [{"name": "front-office", "version": "1.0.0"}] if bundle else []
        agent["spec"]["enabledSkills"] = list(enabled)
        agent["spec"]["disabledSkills"] = list(disabled)
        self.agent_file.write_text(yaml.safe_dump(agent, sort_keys=False))

    def test_bundle_selection_is_inert_until_enabled_and_legacy_direct_selection_still_works(self):
        self.configure(enabled=["answer-faq"])
        desired = compiler.compile_workspace(self.workspace, "receptionist")[0]
        self.assertEqual([row["metadata"]["name"] for row in desired["skills"]], ["answer-faq"])
        self.assertNotIn("calendar.book", {row["metadata"]["name"] for row in desired["tools"]})
        self.assertEqual(set(desired["skillInstructions"]), {"answer-faq"})
        self.assertEqual([row["skill"] for row in desired["skillSelection"] if row["enabled"]], ["answer-faq"])
        self.assertEqual(len(desired["disabledSkillDeclarations"]), 2)

    def test_direct_plus_bundle_deduplicates_and_reuses_normal_dependency_gate(self):
        self.configure(direct=["log-conversation"], enabled=["answer-faq"])
        desired = compiler.compile_workspace(self.workspace, "receptionist")[0]
        self.assertEqual({row["metadata"]["name"] for row in desired["skills"]}, {"answer-faq", "log-conversation"})
        self.assertEqual(len(desired["skillSelection"]), 3)
        self.assertEqual(desired["skillSelection"][-1]["via"], ["direct", "front-office"])

    def test_bad_selection_pin_disabled_conflict_and_missing_skill_fail_closed(self):
        self.configure(enabled=["not-selected"])
        with self.assertRaisesRegex(ValueError, "not selected"):
            compiler.compile_workspace(self.workspace, "receptionist")
        self.configure(enabled=["answer-faq"], disabled=["answer-faq"])
        with self.assertRaisesRegex(ValueError, "simultaneously"):
            compiler.compile_workspace(self.workspace, "receptionist")
        self.configure(enabled=["answer-faq"])
        bundle_doc = yaml.safe_load(self.bundle_path.read_text())
        bundle_doc["spec"]["skills"][0]["version"] = "9.9.9"
        self.bundle_path.write_text(yaml.safe_dump(bundle_doc, sort_keys=False))
        with self.assertRaisesRegex(ValueError, "pins Skill"):
            compiler.compile_workspace(self.workspace, "receptionist")

    def test_readiness_and_approval_preview_agree_with_compiled_activation(self):
        self.configure(enabled=["answer-faq"])
        binding = {
            "environment": "dev", "connectors": {
                "kb": {"provider": "text-knowledge"}, "crm": {"provider": "built-in-crm"},
            },
        }
        report = compiler.workspace_readiness(self.workspace, "receptionist", (), binding)
        ready = {row["name"]: row["enabled"] for row in report[0]["skills"] if row["kind"] == "Skill"}
        self.assertTrue(ready["answer-faq"])
        self.assertFalse(ready["book-appointment"])
        previews = compiler.workspace_approvals(self.workspace, "receptionist", ())
        self.assertEqual({row["skill"] for row in previews[0]["skills"] if row["skill"]}, {"answer-faq"})



if __name__ == "__main__":
    unittest.main()

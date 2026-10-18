"""What platform_capabilities tells the coworker depends on who is asking: plain next steps for staff, ready or blocked for anyone else (#111, #110, #116)."""
from __future__ import annotations

import json
import pathlib
import sys
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_docs_runtime import ROOT, compiler, principal_for, runtime_module  # noqa: E402 - shared fixtures
import shutil  # noqa: E402
import tempfile  # noqa: E402

CAPABILITIES = runtime_module.CAPABILITIES
TIER0 = {"kb": {"provider": "text-knowledge", "tier": 0}, "crm": {"provider": "built-in-crm", "tier": 0}}
OPERATOR_WORDS = ("python3", "scripts/", "client-operator", "`")


def workspace(tmp: str) -> pathlib.Path:
    target = pathlib.Path(tmp) / "workspace"
    shutil.copytree(ROOT / "examples" / "workspace", target)
    agent = target / "agents" / "front-office" / "receptionist" / "agent.yaml"
    document = yaml.safe_load(agent.read_text())
    document["spec"]["disabledSkills"] = []  # booking needs a calendar, which the binding below leaves out
    agent.write_text(yaml.safe_dump(document))
    return target


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.desired = compiler.compile_workspace(workspace(str(self.root)), "receptionist")[0]
        self.desired["source"]["client"] = "acme"
        self.manifest = CAPABILITIES.build_manifest(self.desired, {"environment": "prod", "connectors": TIER0})

    def skill(self, view, name):
        return next(item for item in view["skills"] if item["name"] == name)


class ViewTests(Case):
    def test_the_full_manifest_has_the_operator_wording_this_view_removes(self):
        blocked = self.skill(self.manifest, "book-appointment")
        self.assertFalse(blocked["ready"])
        self.assertIn("python3 scripts/client-operator.py connect calendar", blocked["remediation"])

    def test_a_team_member_gets_one_plain_next_step_and_never_a_command(self):
        view = CAPABILITIES.caller_view(self.manifest, member=True)
        blocked = self.skill(view, "book-appointment")
        self.assertEqual(blocked["nextStep"], "Connect calendar: ask your operator or consultant to start the guided connect step for it")
        self.assertNotIn("remediation", blocked)
        self.assertNotIn("nextStep", self.skill(view, "answer-faq"))  # a ready Skill has nothing to do next
        for word in OPERATOR_WORDS:
            self.assertNotIn(word, json.dumps(view))
        self.assertEqual(self.skill(view, "answer-faq")["dependencies"][0]["provider"], "text-knowledge")  # staff still see the setup

    def test_anyone_else_sees_only_ready_or_blocked_and_nothing_about_the_setup(self):
        view = CAPABILITIES.caller_view(self.manifest, member=False)
        shown = json.dumps(view)
        self.assertFalse(self.skill(view, "book-appointment")["ready"])
        self.assertTrue(self.skill(view, "answer-faq")["ready"])
        for private in ("text-knowledge", "built-in-crm", "rules-of-engagement", "business-information", "not-indexed", "nextStep",
                        "remediation", "prod", *OPERATOR_WORDS):
            self.assertNotIn(private, shown)
        self.assertEqual(set(self.skill(view, "answer-faq")["dependencies"][0]), {"capability", "family", "required", "status"})
        self.assertTrue(all(set(item) <= {"family", "status", "fallback"} for item in view["capabilities"]))

    def test_the_view_is_a_copy_and_does_not_change_the_manifest(self):
        before = json.dumps(self.manifest, sort_keys=True)
        CAPABILITIES.caller_view(self.manifest, member=False)
        CAPABILITIES.caller_view(self.manifest, member=True)
        self.assertEqual(json.dumps(self.manifest, sort_keys=True), before)

    def test_each_blocking_state_has_a_plain_step_without_a_command(self):
        cases = {
            "missing": {"status": "missing", "family": "calendar"},
            "no-grant": {"status": "no-grant", "family": "crm"},
            "unavailable": {"status": "unavailable", "family": "crm"},
            "grant-unavailable": {"status": "grant-unavailable", "family": "crm"},
            "grant-scope": {"status": "grant-scope", "family": "crm", "missingTools": ["crm.create"]},
            "no-content": {"status": "no-content", "family": "kb", "content": {"sources": []}},
        }
        for name, dependency in cases.items():
            step = CAPABILITIES.remediation_for([{"capability": dependency["family"], **dependency}], plain=True)
            self.assertTrue(step, name)
            for word in OPERATOR_WORDS:
                self.assertNotIn(word, step, name)
        self.assertIn("scripts/client-operator.py", CAPABILITIES.remediation_for([{"capability": "calendar", "status": "missing", "family": "calendar"}]))


class SidecarTests(Case):
    def runtime(self, user):
        path = self.root / "desired.json"
        path.write_text(json.dumps(self.desired), encoding="utf-8")
        (self.root / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        principal = principal_for(user)
        return runtime_module.GovernedToolRuntime(path, self.root / "audit.jsonl", self.root / "approvals",
                                                  environ={"MODEL_GARDEN_CAPABILITY_MANIFEST": str(self.root / "manifest.json")},
                                                  principal_resolver=lambda: principal)

    def test_the_tool_answers_by_who_is_asking(self):
        staff = self.runtime("U-ALICE").capabilities_for_caller()
        stranger = self.runtime("U-STRANGER").capabilities_for_caller()
        self.assertIn("nextStep", self.skill(staff, "book-appointment"))
        self.assertNotIn("nextStep", self.skill(stranger, "book-appointment"))
        self.assertNotIn("text-knowledge", json.dumps(stranger))
        self.assertFalse(self.skill(stranger, "book-appointment")["ready"])  # still honest about what it cannot do

    def test_a_call_with_no_attributed_person_is_treated_as_public(self):
        path = self.root / "desired.json"
        path.write_text(json.dumps(self.desired), encoding="utf-8")
        (self.root / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        bare = runtime_module.GovernedToolRuntime(path, self.root / "audit.jsonl", self.root / "approvals",
                                                  environ={"MODEL_GARDEN_CAPABILITY_MANIFEST": str(self.root / "manifest.json")})
        self.assertNotIn("nextStep", json.dumps(bare.capabilities_for_caller()))
        self.assertNotIn("text-knowledge", json.dumps(bare.capabilities_for_caller()))

    def test_the_tool_description_tells_the_coworker_how_to_answer(self):
        import asyncio
        server = runtime_module.build_server(self.runtime("U-ALICE"))
        tool = next(item for item in asyncio.run(server.list_tools()) if item.name == "platform_capabilities")
        for phrase in ("nextStep", "never a command", "human hand-off"):
            self.assertIn(phrase, tool.description)


if __name__ == "__main__":
    unittest.main()

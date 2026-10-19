"""Credential-free checks for what the live runtime takes from an approved Studio change: stricter approval rules and schedule edits."""
from __future__ import annotations

import calendar
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


overlay_module = _load("studio_overlay_under_test", "platform/core/studio_overlay.py")
worker = _load("studio_overlay_worker_under_test", "platform/runtime/worker.py")
sidecar = _load("studio_overlay_sidecar_under_test", "platform/runtime/governed_mcp.py")
T0 = calendar.timegm((2026, 10, 19, 8, 0, 0))
OWNER = "jordan@acme.example"
SCHEDULE = {"name": "monday-summary", "owner": OWNER, "agent": "receptionist", "goal": "Summarise open enquiries for me",
            "cron": "0 9 * * 1", "budget": {"steps": 3, "tokens": 5000}, "requiredCapabilities": ["kb.search"]}
POLICIES = [{"kind": "Policy", "metadata": {"name": "base"}, "spec": {"rules": [
    {"id": "approve-writes", "effect": "require-approval", "actions": ["*.write"]},
    {"id": "no-delete", "effect": "deny", "actions": ["*.delete"]},
    {"id": "read-ok", "effect": "allow", "actions": ["*.read"]},
]}}]


def overlay(**fields):
    return {"schemaVersion": 1, "version": 1, **fields}


def rule_ids(policies):
    return {rule["id"]: rule["effect"] for resource in policies for rule in resource["spec"]["rules"]}


class PolicyTests(unittest.TestCase):
    def apply(self, **fields):
        return overlay_module.effective_policies(POLICIES, overlay(**fields))

    def test_a_new_stricter_rule_applies(self):
        policies, held = self.apply(policyRules={"no-sends": {"id": "no-sends", "effect": "deny", "actions": ["email.send"]}})
        self.assertEqual(rule_ids(policies)["no-sends"], "deny")
        self.assertEqual(held, [])

    def test_a_replacement_at_least_as_strict_applies_and_a_weaker_one_is_held(self):
        policies, held = self.apply(policyRules={
            "read-ok": {"id": "read-ok", "effect": "require-approval", "actions": ["*.read"]},
            "no-delete": {"id": "no-delete", "effect": "require-approval", "actions": ["*.delete"]},
        })
        self.assertEqual(rule_ids(policies)["read-ok"], "require-approval")
        self.assertEqual(rule_ids(policies)["no-delete"], "deny")
        self.assertEqual(held, ["no-delete"])

    def test_an_allow_rule_a_removal_and_a_malformed_rule_are_held(self):
        policies, held = self.apply(policyRules={
            "let-writes": {"id": "let-writes", "effect": "allow", "actions": ["*.write"], "signoff": {"owner": OWNER}},
            "approve-writes": None,
            "broken": {"id": "broken", "effect": "sometimes"},
            "mismatch": {"id": "other", "effect": "deny"},
            "no-assignee": {"id": "no-assignee", "effect": "human-only"},
        })
        self.assertEqual(rule_ids(policies), rule_ids(POLICIES))
        self.assertEqual(sorted(held), ["approve-writes", "broken", "let-writes", "mismatch", "no-assignee"])

    def test_the_inputs_are_not_changed(self):
        before = json.dumps(POLICIES)
        self.apply(policyRules={"read-ok": {"id": "read-ok", "effect": "deny", "actions": ["*.read"]}})
        self.assertEqual(json.dumps(POLICIES), before)

    def test_a_missing_or_damaged_overlay_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.assertEqual(overlay_module.read(root), {})
            (root / "studio").mkdir()
            for text in ("{not json", json.dumps({"schemaVersion": 9}), json.dumps([1])):
                (root / "studio" / "overlay.json").write_text(text)
                self.assertEqual(overlay_module.read(root), {})
            self.assertEqual(overlay_module.apply_to_state({"policies": POLICIES}, {}), {"policies": POLICIES})


class SidecarTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        path = self.root / "desired.json"
        subprocess.run([sys.executable, str(ROOT / "scripts" / "compile-workspace.py"), str(ROOT / "examples" / "workspace"),
                        "--agent", "receptionist", "--output", str(path)], check=True, capture_output=True)
        self.runtime = sidecar.GovernedToolRuntime(path, self.root / "audit.jsonl", self.root / "approvals")

    def write_overlay(self, **fields):
        folder = self.root / "studio"
        folder.mkdir(exist_ok=True)
        (folder / "overlay.json").write_text(json.dumps(overlay(**fields)))

    def decision(self):
        return sidecar.GOVERN.authorize_action(self.runtime.desired_state, "crm.create", {"lead": "x"}, audit_path=self.root / "audit.jsonl")

    def test_an_approved_deny_rule_takes_effect_without_a_restart(self):
        self.assertNotEqual(self.decision()["decision"], "deny")
        self.write_overlay(policyRules={"no-crm-create": {"id": "no-crm-create", "effect": "deny", "actions": ["crm.create"]}})
        decision = self.decision()
        self.assertEqual(decision["decision"], "deny")
        self.assertIn("no-crm-create", json.dumps(decision))

    def test_an_allow_rule_in_the_overlay_does_not_loosen_the_live_runtime(self):
        before = self.decision()["decision"]
        self.write_overlay(policyRules={"approve-external-write": None, "free-crm": {"id": "free-crm", "effect": "allow", "actions": ["crm.create"]}})
        self.assertEqual(self.decision()["decision"], before)

    def test_a_removed_overlay_returns_to_the_workspace_rules(self):
        self.write_overlay(policyRules={"no-crm-create": {"id": "no-crm-create", "effect": "deny", "actions": ["crm.create"]}})
        self.assertEqual(self.decision()["decision"], "deny")
        (self.root / "studio" / "overlay.json").unlink()
        self.assertNotEqual(self.decision()["decision"], "deny")

    def test_assigning_the_state_still_works(self):
        self.runtime.desired_state = {**self.runtime.desired_state, "marker": 1}
        self.assertEqual(self.runtime.desired_state["marker"], 1)


class ScheduleStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        schedules = worker._module("schedules")
        self.module = schedules
        self.store = schedules.ScheduleStore(pathlib.Path(self._tmp.name), tenant="acme-prod")
        self.store.reconcile([SCHEDULE], by="deploy:alex", now=T0)
        self.entry = self.store.list()[0]

    def test_edit_changes_cron_and_budget_and_moves_the_next_fire(self):
        self.assertTrue(self.store.edit(self.entry["id"], by="runtime:worker", cron="0 10 * * 1", budget={"steps": 2}, now=T0))
        edited = self.store.list()[0]
        self.assertEqual((edited["cron"], edited["budget"]), ("0 10 * * 1", {"steps": 2}))
        self.assertEqual(edited["nextFire"], T0 + 2 * 3600)
        self.assertEqual((edited["owner"], edited["state"], edited["goalDigest"]), (self.entry["owner"], "active", self.entry["goalDigest"]))

    def test_edit_to_the_same_values_changes_nothing(self):
        self.assertFalse(self.store.edit(self.entry["id"], by="runtime:worker", cron=self.entry["cron"], budget=self.entry["budget"], now=T0))

    def test_edit_refuses_what_the_store_would_refuse(self):
        for kwargs in ({"cron": "* * * * *"}, {"cron": "nonsense"}, {"budget": {"steps": 0}}, {"budget": {}}):
            with self.assertRaises(self.module.ScheduleError, msg=str(kwargs)):
                self.store.edit(self.entry["id"], by="runtime:worker", now=T0, **kwargs)
        with self.assertRaises(self.module.ScheduleError):
            self.store.edit("s-missing", by="runtime:worker", cron="0 10 * * 1", now=T0)
        self.assertEqual(self.store.list()[0]["cron"], "0 9 * * 1")


class WorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        state = self.root / "desired-state.json"
        subprocess.run([sys.executable, str(ROOT / "scripts" / "compile-workspace.py"), str(ROOT / "examples" / "workspace"),
                        "--agent", "receptionist", "--output", str(state)], check=True, capture_output=True)
        document = json.loads(state.read_text())
        document["schedules"] = [SCHEDULE]
        state.write_text(json.dumps(document))
        self.state = state
        self.clock = [T0]

    def make(self):
        return worker.Worker(self.state, self.data, {"deployedBy": "deploy:alex"}, clock=lambda: self.clock[0])

    def write_overlay(self, schedules):
        folder = self.data / "studio"
        folder.mkdir(exist_ok=True)
        (folder / "overlay.json").write_text(json.dumps(overlay(schedules=schedules)))

    def test_the_changes_job_applies_an_approved_edit_and_ledgers_it(self):
        runtime = self.make()
        runtime.tick()
        schedule_id = runtime._schedule_store().list()[0]["id"]
        self.write_overlay({schedule_id: {"cron": "30 9 * * 1", "budget": {"steps": 2}}})
        self.clock[0] = T0 + 120
        report = runtime.tick()["changes"]
        self.assertEqual(report, {"edits": 1, "applied": 1, "notApplied": 0})
        edited = runtime._schedule_store().list()[0]
        self.assertEqual((edited["cron"], edited["budget"]), ("30 9 * * 1", {"steps": 2}))
        self.assertTrue(any(event.get("action") == "schedule.update" or event.get("event") == "schedule.update"
                            for event in map(json.loads, (self.data / "audit.jsonl").read_text().splitlines())))
        self.clock[0] += 120
        self.assertEqual(runtime.tick()["changes"]["applied"], 0)

    def test_a_restart_does_not_undo_the_edit(self):
        first = self.make()
        first.tick()
        schedule_id = first._schedule_store().list()[0]["id"]
        self.write_overlay({schedule_id: {"cron": "30 9 * * 1"}})
        first.tick()
        restarted = self.make()
        restarted.tick()
        self.assertEqual(restarted._schedule_store().list()[0]["cron"], "30 9 * * 1")

    def test_a_missing_or_refused_edit_is_counted_and_nothing_else_stops(self):
        runtime = self.make()
        runtime.tick()
        schedule_id = runtime._schedule_store().list()[0]["id"]
        self.write_overlay({"s-gone": {"cron": "0 10 * * 1"}, schedule_id: {"cron": "* * * * *"}})
        self.clock[0] += 120
        self.assertEqual(runtime.tick()["changes"], {"edits": 2, "applied": 0, "notApplied": 2})
        self.assertEqual(runtime._schedule_store().list()[0]["cron"], "0 9 * * 1")

    def test_with_no_overlay_the_job_is_a_no_op(self):
        runtime = self.make()
        self.assertEqual(runtime.tick()["changes"], {"edits": 0})


class ShippedTests(unittest.TestCase):
    def test_the_module_is_in_the_runtime_image_and_loads_on_its_own(self):
        deployer = _load("studio_overlay_deployer_under_test", "scripts/deploy-client-docker.py")
        self.assertIn(pathlib.Path("platform/core/studio_overlay.py"), deployer.RUNTIME_SOURCE_PATHS)
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "studio_overlay.py"
            target.write_bytes((ROOT / "platform/core/studio_overlay.py").read_bytes())
            result = subprocess.run([sys.executable, "-P", "-c", f"import runpy; runpy.run_path({str(target)!r})"], capture_output=True, text=True, cwd=tmp)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

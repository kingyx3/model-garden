"""Credential-free checks for schedules as exportable workspace desired state (#99)."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import shutil
import subprocess
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


compiler = _load("compiler_for_schedules", "scripts/compile-workspace.py")
schedules = _load("schedules_for_workspace", "platform/core/schedules.py")
runtime = _load("runtime_schedules_under_test", "scripts/runtime-schedules.py")
OWNER = "jordan@client.example"
MONDAY = {
    "apiVersion": "modelgarden.ai/v1", "kind": "Schedule", "metadata": {"name": "monday-summary", "description": "Pipeline summary"},
    "spec": {"agent": "receptionist", "owner": OWNER, "goal": "Send me a summary of open enquiries", "cron": "0 9 * * 1",
             "budget": {"tokens": 20000, "steps": 12}, "requiredCapabilities": ["crm.read"]},
}
TRIAGE = {
    "apiVersion": "modelgarden.ai/v1", "kind": "Schedule", "metadata": {"name": "triage-mail"},
    "spec": {"agent": "receptionist", "owner": OWNER, "goal": "Triage each new support email", "event": "mail.received",
             "maxPerHour": 4, "budget": {"steps": 5}},
}


def make_workspace(tmp: str, *resources: dict) -> pathlib.Path:
    workspace = pathlib.Path(tmp) / "workspace"
    shutil.copytree(ROOT / "examples" / "workspace", workspace, dirs_exist_ok=True)
    agent = workspace / "agents/front-office/receptionist/agent.yaml"
    document = yaml.safe_load(agent.read_text())
    document["spec"]["tools"].append("crm.read")
    agent.write_text(yaml.safe_dump(document, sort_keys=False))
    (workspace / "schedules").mkdir(exist_ok=True)
    for resource in resources:
        (workspace / "schedules" / f"{resource['metadata']['name']}.yaml").write_text(yaml.safe_dump(resource, sort_keys=False))
    return workspace


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name

    def workspace(self, *resources):
        return make_workspace(self.tmp, *resources)

    def failure(self, *resources) -> str:
        with self.assertRaises(ValueError) as caught:
            compiler.compile_workspace(self.workspace(*resources), "receptionist")
        return str(caught.exception)


class SchemaTests(Case):
    def validator(self):
        return compiler.load_validators()["Schedule"]

    def errors(self, resource):
        return [error.message for error in self.validator().iter_errors(resource)]

    def mutate(self, base, **spec):
        copy = json.loads(json.dumps(base))
        for key, value in spec.items():
            if value is None:
                copy["spec"].pop(key, None)
            else:
                copy["spec"][key] = value
        return copy

    def test_both_shapes_are_valid(self):
        self.assertEqual(self.errors(MONDAY), [])
        self.assertEqual(self.errors(TRIAGE), [])

    def test_exactly_one_of_cron_or_event(self):
        self.assertTrue(self.errors(self.mutate(MONDAY, event="mail.received")))
        self.assertTrue(self.errors(self.mutate(MONDAY, cron=None)))

    def test_budget_owner_and_goal_are_required_and_bounded(self):
        for field in ("budget", "owner", "goal", "agent"):
            self.assertTrue(self.errors(self.mutate(MONDAY, **{field: None})), field)
        self.assertTrue(self.errors(self.mutate(MONDAY, budget={})))
        self.assertTrue(self.errors(self.mutate(MONDAY, budget={"tokens": 0})))
        self.assertTrue(self.errors(self.mutate(MONDAY, budget={"dollars": 3})))
        self.assertTrue(self.errors(self.mutate(MONDAY, goal="x" * 501)))
        self.assertTrue(self.errors(self.mutate(MONDAY, maxPerHour=0)))
        self.assertTrue(self.errors(self.mutate(TRIAGE, event="file.uploaded")))
        self.assertTrue(self.errors(self.mutate(MONDAY, secret="x")))

    def test_names_are_dns_like(self):
        for name in ("Bad Name", "-x", "x" * 70):
            self.assertTrue(self.errors({**MONDAY, "metadata": {"name": name}}), name)


class CompileTests(Case):
    def test_schedules_are_part_of_the_compiled_desired_state_of_their_agent_only(self):
        workspace = self.workspace(MONDAY, TRIAGE)
        (receptionist,) = compiler.compile_workspace(workspace, "receptionist")
        self.assertEqual([item["name"] for item in receptionist["schedules"]], ["monday-summary", "triage-mail"])
        monday = receptionist["schedules"][0]
        self.assertEqual((monday["owner"], monday["cron"], monday["budget"], monday["requiredCapabilities"]), (OWNER, "0 9 * * 1", {"tokens": 20000, "steps": 12}, ["crm.read"]))
        (researcher,) = compiler.compile_workspace(workspace, "account-researcher")
        self.assertNotIn("schedules", researcher)

    def test_a_workspace_without_schedules_compiles_exactly_as_before(self):
        (compiled,) = compiler.compile_workspace(self.workspace(), "receptionist")
        self.assertNotIn("schedules", compiled)

    def test_the_definitions_are_what_the_store_accepts(self):
        (compiled,) = compiler.compile_workspace(self.workspace(MONDAY, TRIAGE), "receptionist")
        store = schedules.ScheduleStore(pathlib.Path(self.tmp) / "store", tenant="acme")
        report = store.reconcile(compiled["schedules"], by="deploy:ci", now=1_800_000_000)
        self.assertEqual(sorted(report["created"]), ["monday-summary", "triage-mail"])
        self.assertEqual(store.export_desired(), compiled["schedules"])

    def test_an_unknown_agent_is_an_error(self):
        message = self.failure({**MONDAY, "spec": {**MONDAY["spec"], "agent": "ghost"}})
        self.assertIn("Schedule 'monday-summary' names Agent 'ghost', which this workspace does not define", message)

    def test_a_tool_the_agent_does_not_select_is_an_error(self):
        message = self.failure({**MONDAY, "spec": {**MONDAY["spec"], "requiredCapabilities": ["calendar.book", "mail.send"]}})
        self.assertIn("needs Tools Agent 'receptionist' does not select: mail.send", message)

    def test_store_rules_apply_at_compile_time(self):
        message = self.failure({**MONDAY, "spec": {**MONDAY["spec"], "cron": "*/5 * * * *"}})
        self.assertIn("monday-summary.yaml", message)
        self.assertIn("15", message)
        message = self.failure({**MONDAY, "spec": {**MONDAY["spec"], "cron": "0 0 31 2 *"}})
        self.assertIn("never fires", message)

    def test_duplicate_names_are_an_error(self):
        workspace = self.workspace(MONDAY)
        (workspace / "schedules" / "again.yaml").write_text(yaml.safe_dump(MONDAY, sort_keys=False))
        with self.assertRaises(ValueError) as caught:
            compiler.compile_workspace(workspace, "receptionist")
        self.assertIn("duplicate Schedule metadata.name 'monday-summary'", str(caught.exception))

    def test_the_cli_prints_the_definitions_and_filters_by_agent(self):
        workspace = self.workspace(MONDAY, TRIAGE)
        out = io.StringIO()
        original = sys.argv
        sys.argv = ["compile-workspace.py", str(workspace), "--schedules"]
        try:
            with contextlib.redirect_stdout(out):
                self.assertEqual(compiler.main(), 0)
        finally:
            sys.argv = original
        self.assertEqual([item["name"] for item in json.loads(out.getvalue())], ["monday-summary", "triage-mail"])
        self.assertEqual(compiler.workspace_schedules(workspace, "account-researcher", ()), [])
        with self.assertRaises(ValueError):
            compiler.workspace_schedules(workspace, "ghost", ())

    def test_validate_workspace_accepts_and_rejects_schedule_resources(self):
        script = ROOT / "scripts" / "validate-workspace.py"
        good = self.workspace(MONDAY)
        self.assertEqual(subprocess.run([sys.executable, str(script), str(good)], capture_output=True, text=True).returncode, 0)
        bad = self.workspace({**MONDAY, "spec": {**MONDAY["spec"], "budget": {}}})
        result = subprocess.run([sys.executable, str(script), str(bad)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("monday-summary.yaml", result.stderr)


class PlanTests(Case):
    def store(self):
        return schedules.ScheduleStore(pathlib.Path(self.tmp) / "store", tenant="acme", audit=lambda *event: self.events.append(event))

    def setUp(self) -> None:
        super().setUp()
        self.events: list[tuple] = []

    def definitions(self):
        return [schedules.definition_from_resource(MONDAY), schedules.definition_from_resource(TRIAGE)]

    def test_plan_reports_what_apply_would_do_and_changes_nothing(self):
        store = self.store()
        planned = store.plan(self.definitions(), now=1_800_000_000)
        self.assertEqual((sorted(planned["created"]), planned["updated"], planned["retired"]), (["monday-summary", "triage-mail"], [], []))
        self.assertEqual(store.list(), [])
        self.assertEqual(self.events, [])
        self.assertEqual(store.reconcile(self.definitions(), by=OWNER, now=1_800_000_000), planned)

    def test_plan_after_apply_shows_updates_retirements_and_unchanged(self):
        store = self.store()
        store.reconcile(self.definitions(), by=OWNER, now=1_800_000_000)
        before = store.list()
        events = len(self.events)
        changed = {**self.definitions()[0], "goal": "A different goal"}
        planned = store.plan([changed], now=1_800_000_100)
        self.assertEqual((planned["updated"], planned["retired"], planned["unchanged"]), (["monday-summary"], ["triage-mail"], []))
        self.assertEqual(store.list(), before)
        self.assertEqual(len(self.events), events)
        self.assertEqual(store.plan(self.definitions(), now=1_800_000_100)["unchanged"], ["monday-summary", "triage-mail"])

    def test_plan_validates_like_apply(self):
        store = self.store()
        with self.assertRaises(schedules.ScheduleError):
            store.plan([{**self.definitions()[0], "cron": "*/5 * * * *"}])
        with self.assertRaises(schedules.ScheduleError):
            store.plan([{**self.definitions()[0], "surprise": 1}])
        with self.assertRaises(schedules.ScheduleError):
            store.plan(self.definitions() + self.definitions()[:1])


class RuntimeCommandTests(Case):
    def run_command(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = runtime.main([str(pathlib.Path(self.tmp) / "gov" / "schedules"), "--tenant", "acme", *argv])
        return code, (json.loads(out.getvalue()) if out.getvalue().strip() else None), err.getvalue()

    def definitions_file(self, name="defs.json", payload=None):
        path = pathlib.Path(self.tmp) / name
        path.write_text(json.dumps(payload if payload is not None else [schedules.definition_from_resource(MONDAY)]))
        return path

    def ledger(self):
        path = pathlib.Path(self.tmp) / "gov" / "audit.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_plan_then_apply_then_export_round_trips_and_ledgers_the_person(self):
        defs = self.definitions_file()
        code, planned, _ = self.run_command("plan", "--definitions", str(defs))
        self.assertEqual((code, planned["created"]), (0, ["monday-summary"]))
        self.assertEqual(self.ledger(), [])
        code, applied, _ = self.run_command("apply", "--by", OWNER, "--definitions", str(defs))
        self.assertEqual((code, applied["created"]), (0, ["monday-summary"]))
        event = self.ledger()[-1]
        self.assertEqual((event["event"], event["action"], event["performedBy"]), ("operator-action", "schedule.create", OWNER))
        self.assertNotIn("Send me a summary", json.dumps(self.ledger()))
        code, exported, _ = self.run_command("export")
        self.assertEqual((code, exported), (0, [schedules.definition_from_resource(MONDAY)]))
        code, again, _ = self.run_command("plan", "--definitions", str(defs))
        self.assertEqual(again["unchanged"], ["monday-summary"])

    def test_definitions_can_come_from_the_compiled_desired_state(self):
        (compiled,) = compiler.compile_workspace(make_workspace(self.tmp, MONDAY, TRIAGE), "receptionist")
        state = pathlib.Path(self.tmp) / "desired-state.json"
        state.write_text(json.dumps(compiled))
        code, applied, _ = self.run_command("apply", "--by", OWNER, "--desired-state", str(state))
        self.assertEqual((code, sorted(applied["created"])), (0, ["monday-summary", "triage-mail"]))

    def test_removing_one_pauses_it_and_bad_input_changes_nothing(self):
        self.run_command("apply", "--by", OWNER, "--definitions", str(self.definitions_file()))
        empty = self.definitions_file("empty.json", [])
        code, applied, _ = self.run_command("apply", "--by", OWNER, "--definitions", str(empty))
        self.assertEqual(applied["retired"], ["monday-summary"])
        bad = self.definitions_file("bad.json", [{"name": "x", "owner": OWNER}])
        code, _, error = self.run_command("apply", "--by", OWNER, "--definitions", str(bad))
        self.assertEqual(code, 1)
        self.assertIn("Runtime schedules failed", error)
        code, _, error = self.run_command("apply", "--by", OWNER, "--definitions", str(self.definitions_file("notalist.json", {"a": 1})))
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()

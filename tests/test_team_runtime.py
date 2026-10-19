"""A team of Agents on real governed runtimes: isolated state, one attributed ledger, delegation that cannot escalate (#95, #96, #129)."""
from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


compiler = _load("compile_workspace_for_team_runtime", "scripts/compile-workspace.py")
team_runtime = _load("team_runtime_under_test", "platform/runtime/team_runtime.py")
govern = team_runtime.GOVERN
delegation = team_runtime.DELEGATION

BINDING = {
    "environment": "dev",
    "connectors": {"kb": {"provider": "text-knowledge", "tier": 0}, "crm": {"provider": "built-in-crm", "tier": 0}},
}
T0 = 1_800_000_000


def states() -> list[dict]:
    """The two reference Agents plus a coordinator holding kb.search, crm.create and the researcher's crm.read."""
    compiled = {state["agent"]["metadata"]["name"]: state for state in compiler.compile_workspace(ROOT / "examples" / "workspace", None)}
    coordinator = copy.deepcopy(compiled["receptionist"])
    coordinator["agent"]["metadata"]["name"] = "coordinator"
    coordinator["source"]["name"] = "coordinator"
    coordinator["tools"] = [tool for tool in coordinator["tools"] if tool["metadata"]["name"] != "crm.log-interaction"] + compiled["account-researcher"]["tools"]
    for state in (*compiled.values(), coordinator):
        state["source"]["client"] = "acme"
    return [coordinator, compiled["account-researcher"], compiled["receptionist"]]


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = pathlib.Path(self._tmp.name) / "governance"
        self.clock = [T0]

    def team(self, **kwargs) -> "team_runtime.TeamRuntime":
        kwargs.setdefault("environment_binding", BINDING)
        kwargs.setdefault("environ", {"MODEL_GARDEN_INITIATING_IDENTITY": "alice@acme.example"})
        return team_runtime.TeamRuntime(states(), self.base, now=lambda: self.clock[0], **kwargs)

    def events(self) -> list[dict]:
        path = self.base / "audit.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def task(self, team, **kwargs):
        values = dict(task_id="task-1", originating_principal="alice@acme.example", originator_permissions=["kb.search", "crm.read"],
                      goal="Look up the customer and answer from the approved rules")
        values.update(kwargs)
        return team.start_task("coordinator", **values)


class LayoutTests(Case):
    def test_each_agent_has_its_own_runtime_and_state_roots(self):
        team = self.team()
        self.assertEqual(team.agents(), ["account-researcher", "coordinator", "receptionist"])
        runtimes = {name: team.runtime(name) for name in team.agents()}
        self.assertIs(team.runtime("receptionist"), runtimes["receptionist"])
        roots = {name: (runtime.approval_root, runtime.handoff_root, runtime.memory_root, runtime.artifact_root) for name, runtime in runtimes.items()}
        self.assertEqual(len({root for group in roots.values() for root in group}), 12)  # nothing is shared between Agents
        for name, runtime in runtimes.items():
            self.assertEqual(runtime.approval_root, self.base / "agents" / name / "approvals")
            self.assertEqual(runtime.audit_path, self.base / "audit.jsonl")  # one shared ledger
            self.assertEqual(runtime.desired_state["agent"]["metadata"]["name"], name)
        self.assertEqual({tool["metadata"]["name"] for tool in runtimes["account-researcher"].desired_state["tools"]}, {"crm.read"})

    def test_a_pending_approval_belongs_to_the_agent_that_asked(self):
        team = self.team()
        result = team.runtime("receptionist").crm_create(fields={"name": "Alex Tan", "phone": "+65 6000 0000"})
        self.assertEqual(result["status"], "pending-approval")
        pending = lambda name: list((self.base / "agents" / name / "approvals" / "pending").glob("*.json"))
        self.assertEqual(len(pending("receptionist")), 1)
        self.assertEqual(pending("account-researcher") + pending("coordinator"), [])

    def test_another_agents_tool_is_not_available_on_an_agents_runtime(self):
        team = self.team()
        with self.assertRaises(PermissionError):
            team.runtime("account-researcher").crm_create(fields={"name": "Alex Tan"})
        denied = [event for event in self.events() if event.get("outcome") == "deny"]
        self.assertEqual([event["agent"] for event in denied], ["account-researcher"])

    def test_the_shared_ledger_names_the_agent_and_stays_a_valid_chain(self):
        team = self.team()
        team.runtime("receptionist").kb_search(query="opening hours")
        with self.assertRaises(PermissionError):
            team.runtime("account-researcher").crm_create(fields={"name": "Alex"})
        self.assertEqual({event["agent"] for event in self.events() if "agent" in event}, {"receptionist", "account-researcher"})
        self.assertTrue(govern.verify_audit_chain(self.base / "audit.jsonl")["valid"])

    def test_each_agent_gets_its_own_manifest_never_anothers(self):
        team = self.team(environ={"MODEL_GARDEN_CAPABILITY_MANIFEST": "/somewhere/else.json"})
        for name in team.agents():
            self.assertEqual(team.runtime(name).environ["MODEL_GARDEN_CAPABILITY_MANIFEST"], str(self.base / "agents" / name / "capabilities.json"))

    def test_launch_plan_runs_each_agent_as_its_own_process(self):
        plan = self.team().launch_plan(base_port=9200)
        self.assertEqual([(entry["agent"], entry["port"]) for entry in plan], [("account-researcher", 9200), ("coordinator", 9201), ("receptionist", 9202)])
        for entry in plan:
            argv = entry["argv"]
            self.assertIn(str(self.base / "agents" / entry["agent"] / "desired-state.json"), argv)
            self.assertEqual(argv[argv.index("--approval-root") + 1], str(self.base / "agents" / entry["agent"] / "approvals"))
            self.assertEqual(argv[argv.index("--audit-path") + 1], str(self.base / "audit.jsonl"))
            self.assertEqual(argv[argv.index("--port") + 1], str(entry["port"]))
            self.assertEqual(entry["env"], {"MODEL_GARDEN_CAPABILITY_MANIFEST": str(self.base / "agents" / entry["agent"] / "capabilities.json")})
        with self.assertRaises(team_runtime.TeamError):
            self.team().launch_plan(base_port=65535)

    def test_an_agent_the_ledger_would_attribute_to_another_name_is_refused(self):
        mismatched = states()
        mismatched[0]["source"]["name"] = "receptionist"  # would be counted, approved and ledgered as the receptionist
        with self.assertRaises(team_runtime.TeamError) as caught:
            team_runtime.TeamRuntime(mismatched, self.base, now=lambda: T0)
        self.assertIn("source.name", str(caught.exception))
        self.assertFalse((self.base / "agents").exists())

    def test_agent_names_become_directories_so_unsafe_ones_are_refused(self):
        bad = states()
        bad[0]["agent"]["metadata"]["name"] = "../escape"
        with self.assertRaises(team_runtime.TeamError):
            team_runtime.TeamRuntime(bad, self.base, now=lambda: T0)
        duplicate = states()
        duplicate[1]["agent"]["metadata"]["name"] = "receptionist"
        with self.assertRaises(team_runtime.TeamError):
            team_runtime.TeamRuntime(duplicate, self.base, now=lambda: T0)
        self.assertFalse((self.base / "agents").exists())

    def test_deployments_are_ledgered_per_agent(self):
        team = self.team()
        first = team.ledger_deployments("owner@client.example")
        self.assertTrue(first["receptionist"]["knowledge"]["changed"])
        changes = [event for event in self.events() if event.get("action") == "knowledge.change"]
        self.assertEqual({event["details"]["agent"] for event in changes}, {"receptionist", "coordinator", "account-researcher"})
        again = team.ledger_deployments("owner@client.example")
        self.assertFalse(again["receptionist"]["knowledge"]["changed"])
        self.assertEqual(len([event for event in self.events() if event.get("action") == "knowledge.change"]), len(changes))


    def test_a_policy_change_names_the_agent_whose_policy_changed(self):
        with_policy = states()
        with_policy[2]["policies"] = [{"apiVersion": "modelgarden.ai/v1", "kind": "Policy", "metadata": {"name": "client-policy", "owner": "client"},
                                       "spec": {"rules": [{"id": "no-new-contacts", "effect": "deny", "actions": ["crm.create"]}]}}]
        team = team_runtime.TeamRuntime(with_policy, self.base, environment_binding=BINDING, now=lambda: T0)
        team.ledger_deployments("owner@client.example")
        changes = [event for event in self.events() if event.get("event") == "policy-change"]
        by_agent = {event["agent"]: event["details"]["added"] for event in changes}
        self.assertEqual(by_agent["receptionist"], ["no-new-contacts"])
        self.assertEqual(len(by_agent), len(changes))  # one event per Agent, each naming it


class StartupTests(Case):
    def test_a_team_will_not_start_with_an_agent_that_has_no_access_of_its_own(self):
        external = {"environment": "dev", "connectors": {**BINDING["connectors"], "crm": {"provider": "hubspot", "tier": 1}}}
        with self.assertRaises(team_runtime.TeamError) as caught:
            self.team(environment_binding=external)
        self.assertIn("no usable crm grant of its own", str(caught.exception))
        self.assertFalse((self.base / "agents").exists())

    def test_a_team_will_not_start_with_an_unbound_family(self):
        with self.assertRaises(team_runtime.TeamError) as caught:
            self.team(environment_binding={"environment": "dev", "connectors": {"kb": BINDING["connectors"]["kb"]}})
        self.assertIn("crm is not bound", str(caught.exception))

    def test_one_agent_starts_unchanged_on_the_environment_binding(self):
        external = {"environment": "dev", "connectors": {**BINDING["connectors"], "crm": {"provider": "hubspot", "tier": 1}}}
        single = team_runtime.TeamRuntime(states()[1:2], self.base, environment_binding=external, now=lambda: T0)
        self.assertEqual(single.report["mode"], "single-agent")
        self.assertEqual(single.agents(), ["account-researcher"])


class DelegationTests(Case):
    def test_a_hop_gives_the_delegate_only_what_all_three_parties_allow(self):
        team = self.team()
        root = self.task(team)
        self.assertEqual(root["authority"], ["crm.read", "kb.search"])
        child = team.delegate(root, to_agent="receptionist", child_task_id="task-1.1", goal="Answer from the approved rules", required_capabilities=["kb.search"])
        self.assertEqual((child["agent"], child["authority"], child["delegationChain"]), ("receptionist", ["kb.search"], ["coordinator", "receptionist"]))
        self.assertEqual(child["originatingPrincipal"], "alice@acme.example")
        hop = [event for event in self.events() if event.get("action") == "delegation.hop"]
        self.assertEqual(len(hop), 1)
        self.assertEqual((hop[0]["performedBy"], hop[0]["details"]["toAgent"]), ("agent:coordinator", "receptionist"))
        self.assertNotIn("Answer from the approved rules", json.dumps(hop[0]))  # a digest of the goal, never the goal

    def test_no_delegation_path_widens_authority(self):
        team = self.team()
        root = self.task(team)
        for target, tool in (("receptionist", "crm.create"), ("account-researcher", "kb.search"), ("receptionist", "crm.read")):
            with self.assertRaises(delegation.DelegationError, msg=f"{target} {tool}"):
                team.delegate(root, to_agent=target, child_task_id="task-1.x", goal="Do more", required_capabilities=[tool])
        refused = [event for event in self.events() if event.get("action") == "delegation.refused"]
        self.assertEqual(len(refused), 3)
        self.assertEqual({event["performedBy"] for event in refused}, {"agent:coordinator"})
        self.assertEqual([event for event in self.events() if event.get("action") == "delegation.hop"], [])
        child = team.delegate(root, to_agent="account-researcher", child_task_id="task-1.1", goal="Look up", required_capabilities=["crm.read"])
        with self.assertRaises(delegation.DelegationError):  # the researcher cannot hand work back up the chain
            team.delegate(child, to_agent="coordinator", child_task_id="task-1.1.1", goal="Loop", required_capabilities=["crm.read"])

    def test_unknown_agents_and_foreign_envelopes_are_refused(self):
        team = self.team()
        root = self.task(team)
        with self.assertRaises(team_runtime.TeamError):
            team.delegate(root, to_agent="intruder", child_task_id="task-1.1", goal="Anything", required_capabilities=["kb.search"])
        with self.assertRaises(team_runtime.TeamError):
            team.start_task("intruder", task_id="t", originating_principal="alice@acme.example", originator_permissions=["kb.search"], goal="x")
        forged = {**root, "agent": "receptionist"}
        with self.assertRaises(team_runtime.TeamError):
            team.delegate(forged, to_agent="account-researcher", child_task_id="task-1.1", goal="x", required_capabilities=["crm.read"])

    def test_fan_out_is_counted_from_the_ledger_and_survives_a_restart(self):
        team = self.team(max_fan_out=1)
        root = self.task(team)
        team.delegate(root, to_agent="receptionist", child_task_id="task-1.1", goal="One", required_capabilities=["kb.search"])
        restarted = self.team(max_fan_out=1)
        with self.assertRaises(delegation.DelegationError):
            restarted.delegate(root, to_agent="account-researcher", child_task_id="task-1.2", goal="Two", required_capabilities=["crm.read"])

    def test_a_forged_envelope_cannot_grant_a_tool_the_agent_was_not_compiled_with(self):
        team = self.team()
        child = team.delegate(self.task(team), to_agent="receptionist", child_task_id="task-1.1", goal="x", required_capabilities=["kb.search"])
        with self.assertRaises(team_runtime.TeamError):
            with team.acting("receptionist", {**child, "authority": ["kb.search", "mail.send"]}):
                self.fail("must not run")


class TaskCallTests(Case):
    def child(self, team, **limits):
        root = self.task(team, **limits)
        return team.delegate(root, to_agent="receptionist", child_task_id="task-1.1", goal="Answer from the approved rules", required_capabilities=["kb.search"])

    def test_calls_under_a_task_carry_its_identifiers_in_the_ledger(self):
        team = self.team()
        child = self.child(team)
        with team.acting("receptionist", child) as runtime:
            runtime.kb_search(query="opening hours")
        reads = [event for event in self.events() if event.get("event") in ("authorization", "execution") and event.get("tool") == "kb.search"]
        self.assertTrue(reads)
        for event in reads:
            context = event["governance"]
            self.assertEqual((context["taskId"], context["originatingPrincipal"], context["delegationChain"]),
                             ("task-1.1", "alice@acme.example", ["coordinator", "receptionist"]))
            self.assertEqual(event["agent"], "receptionist")
        with team.acting("receptionist", child) as runtime:
            runtime.kb_search(query="opening hours")
        outside = team.runtime("receptionist").kb_search(query="opening hours")  # outside a task nothing is added
        self.assertNotEqual(outside.get("status"), "denied")
        self.assertNotIn("taskId", self.events()[-1].get("governance", {}))

    def test_a_tool_outside_the_delegated_authority_is_denied_and_ledgered(self):
        team = self.team()
        child = self.child(team)
        with team.acting("receptionist", child) as runtime:
            with self.assertRaises(PermissionError):
                runtime.crm_create(fields={"name": "Alex Tan"})  # the receptionist holds it, but this task was not given it
        denial = [event for event in self.events() if event.get("outcome") == "deny"][-1]
        self.assertEqual(denial["details"]["reason"], "outside-delegated-authority")
        self.assertEqual(denial["governance"]["taskId"], "task-1.1")
        self.assertEqual(list((self.base / "agents" / "receptionist" / "approvals" / "pending").glob("*.json")), [])  # never even queued for approval

    def test_a_task_past_its_deadline_halts(self):
        team = self.team()
        child = self.child(team, deadline=T0 + 60)
        with team.acting("receptionist", child) as runtime:
            runtime.kb_search(query="opening hours")
        self.clock[0] = T0 + 60
        with team.acting("receptionist", child) as runtime:
            with self.assertRaises(PermissionError):
                runtime.kb_search(query="opening hours")
        self.assertEqual([event["details"]["reason"] for event in self.events() if event.get("outcome") == "deny"], ["task-deadline-passed"])

    def test_a_task_cannot_be_used_on_another_agents_runtime(self):
        team = self.team()
        child = self.child(team)
        with team.acting("receptionist", child):
            with self.assertRaises(PermissionError):
                team.runtime("account-researcher").crm_read(query="Alex")  # the researcher is not who the task was given to
        denial = [event for event in self.events() if event.get("outcome") == "deny"][-1]
        self.assertEqual((denial["agent"], denial["details"]["reason"]), ("account-researcher", "task-bound-to-another-agent"))
        with self.assertRaises(team_runtime.TeamError):
            with team.acting("account-researcher", child):
                self.fail("must not run")


class CliTests(Case):
    cli = _load("runtime_team_cli_under_test", "scripts/runtime-team.py")

    def run_cli(self, *argv):
        import contextlib, io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), unittest.mock.patch.object(sys, "argv", ["runtime-team.py", *map(str, argv)]):
            code = self.cli.main()
        return code, out.getvalue(), err.getvalue()

    def write(self, name, document):
        path = self.base.parent / name
        path.write_text(json.dumps(document) if not isinstance(document, str) else document)
        return path

    def test_prepare_writes_per_agent_state_and_prints_the_launch_plan(self):
        compiled = self.write("compiled.json", {"schemaVersion": 1, "agents": states()})
        binding = self.write("binding.json", json.dumps({"apiVersion": "modelgarden.ai/v1", "kind": "EnvironmentBinding", "spec": BINDING}))
        code, out, err = self.run_cli("prepare", compiled, "--base", self.base, "--environment-binding", binding, "--base-port", 9300)
        self.assertEqual((code, err), (0, ""))
        document = json.loads(out)
        self.assertEqual((document["mode"], document["ready"]), ("multi-agent", True))
        self.assertEqual([(entry["agent"], entry["port"]) for entry in document["launch"]], [("account-researcher", 9300), ("coordinator", 9301), ("receptionist", 9302)])
        for name in ("account-researcher", "coordinator", "receptionist"):
            self.assertTrue((self.base / "agents" / name / "desired-state.json").is_file())

    def test_prepare_refuses_a_team_without_its_own_access_and_writes_nothing(self):
        compiled = self.write("compiled.json", {"schemaVersion": 1, "agents": states()})
        binding = self.write("binding.json", json.dumps({"connectors": {"kb": BINDING["connectors"]["kb"], "crm": {"provider": "hubspot", "tier": 1}}}))
        code, out, err = self.run_cli("prepare", compiled, "--base", self.base, "--environment-binding", binding)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no usable crm grant of its own", err)
        self.assertFalse((self.base / "agents").exists())

    def test_prepare_can_ledger_each_agents_deployment(self):
        compiled = self.write("compiled.json", {"schemaVersion": 1, "agents": states()})
        binding = self.write("binding.json", json.dumps(BINDING))
        code, out, _ = self.run_cli("prepare", compiled, "--base", self.base, "--environment-binding", binding, "--ledger-deployments", "owner@client.example")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["deployments"]["receptionist"]["knowledge"]["changed"])
        self.assertTrue(any(event.get("action") == "knowledge.change" for event in self.events()))

    def test_grants_root_needs_a_tenant(self):
        compiled = self.write("compiled.json", {"schemaVersion": 1, "agents": states()})
        code, _, err = self.run_cli("prepare", compiled, "--base", self.base, "--grants-root", self.base.parent / "grants")
        self.assertEqual(code, 1)
        self.assertIn("--tenant", err)


class BudgetTests(Case):
    def setUp(self) -> None:
        super().setUp()
        self.notices: list[dict] = []

    def team(self, **kwargs):
        return super().team(notify=self.notices.append, **kwargs)

    def child(self, team, child_budget, task_id="task-1.1", **root):
        parent = self.task(team, **root)
        return parent, team.delegate(parent, to_agent="receptionist", child_task_id=task_id, goal="Answer", required_capabilities=["kb.search"], budget=child_budget)

    def test_every_governed_call_under_a_task_is_charged_a_step_before_it_runs(self):
        team = self.team()
        _, child = self.child(team, {"steps": 2}, budget={"steps": 5})
        for _ in range(2):
            with team.acting("receptionist", child) as runtime:
                runtime.kb_search(query="opening hours")
        with team.acting("receptionist", child) as runtime:
            with self.assertRaises(PermissionError):
                runtime.kb_search(query="opening hours")
        self.assertEqual(len([event for event in self.events() if event.get("event") == "execution"]), 2)  # the third never ran
        exhausted = [event for event in self.events() if event.get("action") == "task.budget-exhausted"]
        self.assertEqual([(event["details"]["taskId"], event["details"]["exceeded"]) for event in exhausted], [("task-1.1", ["steps"])])
        self.assertEqual(self.events()[-1]["details"]["reason"], "task-budget-exhausted")
        self.assertEqual([(notice["to"], notice["taskId"]) for notice in self.notices], [("alice@acme.example", "task-1.1")])
        with team.acting("receptionist", child) as runtime:  # a halted branch stays halted, and the owner is not told twice
            with self.assertRaises(PermissionError):
                runtime.kb_search(query="opening hours")
        self.assertEqual(len(self.notices), 1)

    def test_siblings_cannot_together_spend_more_than_the_parent_had(self):
        team = self.team()
        parent = self.task(team, budget={"steps": 4})
        team.delegate(parent, to_agent="receptionist", child_task_id="task-1.1", goal="One", required_capabilities=["kb.search"], budget={"steps": 3})
        with self.assertRaises(delegation.DelegationError):
            team.delegate(parent, to_agent="account-researcher", child_task_id="task-1.2", goal="Two", required_capabilities=["crm.read"], budget={"steps": 3})
        team.delegate(parent, to_agent="account-researcher", child_task_id="task-1.2", goal="Two", required_capabilities=["crm.read"], budget={"steps": 1})
        self.assertEqual({row["taskId"]: row["reserved"] for row in team.task_status("task-1")}["task-1"], {"steps": 4})

    def test_spending_survives_a_restart(self):
        team = self.team()
        _, child = self.child(team, {"steps": 2}, budget={"steps": 5})
        with team.acting("receptionist", child) as runtime:
            runtime.kb_search(query="opening hours")
        restarted = self.team()
        self.assertEqual({row["taskId"]: row["spent"] for row in restarted.task_status("task-1.1")}["task-1.1"], {"steps": 1})
        with restarted.acting("receptionist", child) as runtime:
            runtime.kb_search(query="opening hours")
            with self.assertRaises(PermissionError):
                runtime.kb_search(query="opening hours")

    def test_usage_the_host_measured_is_charged_and_halts_the_branch(self):
        team = self.team()
        _, child = self.child(team, {"tokens": 1000}, budget={"tokens": 5000})
        self.assertEqual(team.charge(child, tokens=400)["tokens"], 600)
        with self.assertRaises(team_runtime.TASK_BUDGET.BudgetExhausted):
            team.charge(child, tokens=700)
        with team.acting("receptionist", child) as runtime:
            with self.assertRaises(PermissionError):
                runtime.kb_search(query="opening hours")

    def test_finishing_a_task_releases_its_reservation_and_stops_its_calls(self):
        team = self.team()
        parent, child = self.child(team, {"steps": 3}, budget={"steps": 4})
        with team.acting("receptionist", child) as runtime:
            runtime.kb_search(query="opening hours")
        self.assertEqual(team.finish_task(child), {"steps": 1})
        self.assertEqual({row["taskId"]: row["available"] for row in team.task_status("task-1")}["task-1"], {"steps": 3})
        with team.acting("receptionist", child) as runtime:
            with self.assertRaises(PermissionError):
                runtime.kb_search(query="opening hours")
        self.assertEqual([event["details"]["reason"] for event in self.events() if event.get("outcome") == "deny"], ["task-finished"])

    def test_an_envelope_the_team_did_not_start_or_that_was_edited_is_refused(self):
        team = self.team()
        stranger = delegation.root_envelope(task_id="elsewhere", agent="receptionist", originating_principal="alice@acme.example", goal="x",
                                            agent_grants=["kb.search"], originator_permissions=["kb.search"])
        with self.assertRaises(team_runtime.TeamError):
            with team.acting("receptionist", stranger):
                self.fail("must not run")
        _, child = self.child(team, {"steps": 2}, budget={"steps": 5})
        with self.assertRaises(team_runtime.TeamError):
            with team.acting("receptionist", {**child, "originatingPrincipal": "mallory@evil.example"}):
                self.fail("must not run")
        with self.assertRaises(team_runtime.TeamError):
            team.start_task("coordinator", task_id="task-1", originating_principal="alice@acme.example", originator_permissions=["kb.search"], goal="again")


class HumanStepTests(Case):
    def child(self, team):
        parent = self.task(team)
        return team.delegate(parent, to_agent="receptionist", child_task_id="task-1.1", goal="Answer", required_capabilities=["kb.search"])

    def test_a_step_waits_on_a_named_person_and_resumes_under_their_name(self):
        team = self.team()
        child = self.child(team)
        document = team.open_human_step(child, step="approve-discount", assignee="jordan@acme.example", ask="Approve a 10% discount for Alex?")
        self.assertEqual((document["assignee"], document["request"]["taskId"]), ("jordan@acme.example", "task-1.1"))
        self.assertEqual(team.open_human_step(child, step="approve-discount", assignee="jordan@acme.example", ask="Approve a 10% discount for Alex?")["id"], document["id"])
        self.assertEqual(team.human_step(child, "approve-discount")["state"], "waiting")
        handoffs = team.agent_dir("receptionist") / "handoffs"  # the receptionist's own store, not another Agent's
        self.assertTrue(handoffs.is_dir())
        self.assertFalse((team.agent_dir("coordinator") / "handoffs").exists())
        handoff = team_runtime.HUMAN_STEP._handoff()
        handoff.complete(handoffs, document["id"], "done", "jordan@acme.example", note="Approved, tell Alex today", now=T0 + 30)
        resumed = team.human_step(child, "approve-discount")
        self.assertEqual((resumed["state"], resumed["resume"], resumed["by"], resumed["input"]), ("resumed", True, "jordan@acme.example", "Approved, tell Alex today"))
        team.human_step(child, "approve-discount")  # asking again does not ledger again
        actions = [(event["action"], event["performedBy"]) for event in self.events() if str(event.get("action", "")).startswith("task.human-step")]
        self.assertEqual(actions, [("task.human-step.opened", "agent:receptionist"), ("task.human-step.resolved", "jordan@acme.example")])
        self.assertNotIn("Approved, tell Alex today", json.dumps(self.events()))  # a digest of the note, never the note

    def test_a_timeout_resolves_to_the_configured_fallback_and_never_resumes(self):
        team = self.team()
        child = self.child(team)
        team.open_human_step(child, step="sign-off", assignee="jordan@acme.example", ask="Sign off?", timeout_hours=1, fallback="escalate", escalate_to="owner@acme.example")
        self.clock[0] = T0 + 2 * 3600
        result = team.human_step(child, "sign-off")
        self.assertEqual((result["state"], result["resume"], result["next"], result["to"]), ("timed-out", False, "escalate", "owner@acme.example"))
        team.human_step(child, "sign-off")
        self.assertEqual([event["performedBy"] for event in self.events() if event.get("action") == "task.human-step.timed-out"], ["task:task-1.1"])

    def test_the_answer_never_widens_the_tasks_authority(self):
        team = self.team()
        child = self.child(team)
        document = team.open_human_step(child, step="grant", assignee="jordan@acme.example", ask="Allow crm.create?")
        team_runtime.HUMAN_STEP._handoff().complete(team.agent_dir("receptionist") / "handoffs", document["id"], "done", "jordan@acme.example",
                                                      note="yes, crm.create is fine", now=T0 + 5)
        self.assertEqual(team.human_step(child, "grant")["state"], "resumed")
        with team.acting("receptionist", child) as runtime:
            with self.assertRaises(PermissionError):
                runtime.crm_create(fields={"name": "Alex"})

    def test_a_step_needs_an_envelope_the_team_started(self):
        team = self.team()
        child = self.child(team)
        with self.assertRaises(team_runtime.TeamError):
            team.open_human_step({**child, "taskId": "elsewhere"}, step="x", assignee="jordan@acme.example", ask="?")

    def test_the_task_timeline_joins_hops_budget_and_the_persons_answer_across_agents(self):
        team = self.team()
        child = self.child(team)
        with team.acting("receptionist", child) as runtime:
            runtime.kb_search(query="opening hours")
        document = team.open_human_step(child, step="approve", assignee="jordan@acme.example", ask="Approve?")
        team_runtime.HUMAN_STEP._handoff().complete(team.agent_dir("receptionist") / "handoffs", document["id"], "declined", "jordan@acme.example",
                                                      note="no", now=T0 + 5)
        team.human_step(child, "approve")
        audit_query = _load("audit_query_for_team_runtime", "platform/core/audit_query.py")
        timeline = audit_query.timeline(self.events(), task="task-1.1")
        actions = [step["action"] for step in timeline["steps"] if step.get("action")]
        self.assertEqual(actions, ["delegation.hop", "task.human-step.opened", "task.human-step.resolved"])
        resolved = [step for step in timeline["steps"] if step.get("action") == "task.human-step.resolved"][0]
        self.assertEqual((resolved["by"], resolved["details"]["outcome"], resolved["details"]["step"]), ("jordan@acme.example", "declined", "approve"))
        self.assertIn("kb.search", [step["tool"] for step in timeline["steps"]])
        self.assertEqual(timeline["agents"], ["coordinator", "receptionist"])
        self.assertNotIn("Answer", json.dumps([step.get("details") for step in timeline["steps"]]))  # no goal text
        root = audit_query.timeline(self.events(), task="task-1")  # the root task sees the hop it made
        self.assertEqual([step["action"] for step in root["steps"] if step.get("action")], ["delegation.hop"])


class FanOutTests(Case):
    """Independent sub-tasks run in parallel under the parent's budget and are joined with a per-branch outcome and spend."""

    def branches(self, count=3, agent="receptionist", capability="kb.search"):
        return [{"agent": agent, "taskId": f"task-1.{n}", "goal": f"Branch {n}", "requiredCapabilities": [capability], "budget": {"steps": 2}}
                for n in range(1, count + 1)]

    @staticmethod
    def search(agent, envelope, runtime):
        runtime.kb_search(query="opening hours")
        return envelope["taskId"]

    def test_branches_run_under_their_own_envelopes_and_the_join_reports_each_with_its_spend(self):
        team = self.team()
        parent = self.task(team, budget={"steps": 10})
        joined = team.fan_out(parent, self.branches(), runner=self.search)
        self.assertTrue(joined["complete"])
        self.assertEqual([(item["taskId"], item["outcome"], item["value"], item["usage"]["steps"]) for item in joined["branches"]],
                         [(f"task-1.{n}", "succeeded", f"task-1.{n}", 1) for n in (1, 2, 3)])
        executions = [event for event in self.events() if event.get("event") == "execution"]
        self.assertEqual(sorted(event["governance"]["taskId"] for event in executions), ["task-1.1", "task-1.2", "task-1.3"])
        self.assertTrue(govern.verify_audit_chain(self.base / "audit.jsonl")["valid"])  # parallel appends keep one valid chain
        self.assertEqual([row["state"] for row in team.task_status("task-1") if row["taskId"] != "task-1"], ["finished"] * 3)

    def test_the_task_timeline_shows_the_fan_out_each_branch_and_the_join(self):
        team = self.team()
        parent = self.task(team, budget={"steps": 10})
        team.fan_out(parent, self.branches(2), runner=self.search, policy="fail-fast")
        audit_query = _load("audit_query_for_fan_out", "platform/core/audit_query.py")
        timeline = audit_query.timeline(self.events(), task="task-1")
        actions = [step["action"] for step in timeline["steps"] if step.get("action")]
        self.assertEqual(actions.count("task.branch.finished"), 2)
        self.assertEqual((actions[0], actions[-1]), ("task.fan-out", "task.join"))
        join = [step for step in timeline["steps"] if step.get("action") == "task.join"][0]
        self.assertEqual((join["details"]["policy"], join["details"]["succeeded"]), ("fail-fast", 2))
        self.assertEqual(timeline["usage"]["steps"], 2)
        self.assertNotIn("Branch 1", json.dumps(self.events()))  # goal text is never ledgered

    def test_a_branch_over_its_budget_halts_alone_and_best_effort_still_runs_the_others(self):
        team = self.team()
        parent = self.task(team, budget={"steps": 10})
        branches = self.branches(3)
        branches[1]["budget"] = {"steps": 1}

        def runner(agent, envelope, runtime):
            for _ in range(2 if envelope["taskId"] == "task-1.2" else 1):
                runtime.kb_search(query="opening hours")

        joined = team.fan_out(parent, branches, runner=runner)
        self.assertEqual([item["outcome"] for item in joined["branches"]], ["succeeded", "budget-exhausted", "succeeded"])
        self.assertFalse(joined["complete"])
        self.assertEqual(joined["branches"][1]["error"], "PermissionError")

    def test_fail_fast_starts_no_branch_after_the_first_failure(self):
        team = self.team()
        parent = self.task(team, budget={"steps": 10})

        def runner(agent, envelope, runtime):
            raise RuntimeError("card 4111 1111 1111 1111 declined")

        joined = team.fan_out(parent, self.branches(3), runner=runner, policy="fail-fast", max_concurrency=1)
        self.assertEqual([item["outcome"] for item in joined["branches"]], ["failed", "cancelled", "cancelled"])
        self.assertEqual(joined["branches"][0]["error"], "RuntimeError")
        self.assertNotIn("declined", json.dumps(self.events()))  # the error type only
        rows = team.task_status("task-1")  # a cancelled branch was delegated but never ran: it spent nothing and its reservation came back
        self.assertEqual([(row["state"], row["spent"].get("steps", 0)) for row in rows if row["taskId"] != "task-1"], [("finished", 0)] * 3)
        self.assertEqual(next(row for row in rows if row["taskId"] == "task-1")["reserved"].get("steps", 0), 0)

    def test_a_refused_hop_is_a_join_outcome_and_fail_fast_stops_after_it(self):
        team = self.team()
        parent = self.task(team, budget={"steps": 10})
        branches = [{"agent": "receptionist", "taskId": "task-1.1", "goal": "Needs a Tool it does not hold", "requiredCapabilities": ["crm.create"]},
                    *self.branches(1)[:1]]
        branches[1]["taskId"] = "task-1.2"
        joined = team.fan_out(parent, branches, runner=self.search, policy="fail-fast")
        self.assertEqual([item["outcome"] for item in joined["branches"]], ["refused", "cancelled"])
        self.assertIn("delegation.refused", [event.get("action") for event in self.events()])

    def test_best_effort_runs_the_branches_that_could_be_delegated(self):
        team = self.team()
        parent = self.task(team, budget={"steps": 10})
        branches = [{"agent": "receptionist", "taskId": "task-1.1", "goal": "x", "requiredCapabilities": ["crm.create"]}, *self.branches(2)[1:]]
        joined = team.fan_out(parent, branches, runner=self.search)
        self.assertEqual([item["outcome"] for item in joined["branches"]], ["refused", "succeeded"])

    def test_concurrency_never_exceeds_the_call_or_the_environment_limit(self):
        import threading
        import time
        team = self.team(max_parallel=2)
        parent = self.task(team, budget={"steps": 50})
        running, peak, lock = [0], [0], threading.Lock()

        def runner(agent, envelope, runtime):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.05)
            with lock:
                running[0] -= 1

        branches = [{"agent": "receptionist", "taskId": f"task-1.{n}", "goal": "w", "requiredCapabilities": ["kb.search"], "budget": {"steps": 1}} for n in range(1, 6)]
        joined = team.fan_out(parent, branches, runner=runner, max_concurrency=8)  # asking for more than the environment allows
        self.assertEqual((joined["concurrency"], peak[0]), (2, 2))
        self.assertTrue(joined["complete"])
        for bad in (0, 33):
            with self.assertRaises(team_runtime.TeamError):
                self.team(max_parallel=bad)

    def test_the_branches_share_the_parents_budget_and_cannot_overspend_it(self):
        team = self.team()
        parent = self.task(team, budget={"steps": 3})
        joined = team.fan_out(parent, self.branches(3), runner=self.search)  # each reserves 2 of the parent's 3
        self.assertEqual([item["outcome"] for item in joined["branches"]], ["succeeded", "refused", "refused"])

    def test_bad_requests_are_refused_before_anything_is_started(self):
        team = self.team()
        parent = self.task(team)
        for kwargs in ({"policy": "whenever"}, {"max_concurrency": 0}):
            with self.assertRaises(team_runtime.TeamError):
                team.fan_out(parent, self.branches(1), runner=self.search, **kwargs)
        with self.assertRaises(team_runtime.TeamError):
            team.fan_out(parent, [], runner=self.search)
        self.assertNotIn("task.fan-out", [event.get("action") for event in self.events()])


class LoadTests(Case):
    def test_load_states_reads_single_and_multi_agent_output(self):
        multi = self.base / "multi.json"
        multi.parent.mkdir(parents=True)
        multi.write_text(json.dumps({"schemaVersion": 1, "agents": states()}))
        self.assertEqual(len(team_runtime.load_states(multi)), 3)
        single = self.base / "single.json"
        single.write_text(json.dumps(states()[0]))
        self.assertEqual(len(team_runtime.load_states(single)), 1)
        single.write_text("[]")
        with self.assertRaises(team_runtime.TeamError):
            team_runtime.load_states(single)


if __name__ == "__main__":
    unittest.main()


class StudioProposalTests(Case):
    """An Agent prepares a Studio change for the person whose task it is; only a person can approve it."""

    OWNER = "owner@acme.example"

    def store(self, team, *, people=None):
        core = _load("change_requests_for_team_runtime", "platform/core/change_requests.py")
        return core.ChangeStore(
            self.base / "data", tenant="acme", desired_state=team.runtime("coordinator").desired_state, manifest=None, owner=self.OWNER,
            audit=team._ledger_action, clock=lambda: self.clock[0],  # noqa: SLF001
            people={"alice@acme.example": ["author"]} if people is None else people, signin=lambda: ["alice@acme.example"])

    PARAMS = {"skill": "take-message", "enabled": False}

    def test_a_person_with_author_gets_a_proposal_that_names_the_agent_and_task(self):
        team = self.team()
        envelope = self.task(team, budget={"steps": 5})
        store = self.store(team)
        change = team.propose_change(envelope, store, "skill", self.PARAMS)
        self.assertEqual((change["state"], change["proposedBy"], change["proposedVia"]), ("proposed", "alice@acme.example", {"agent": "coordinator", "taskId": "task-1"}))
        self.assertEqual(store.overlay()["version"], 0)  # nothing is changed until the owner approves
        self.assertEqual(next(row for row in team.task_status("task-1") if row["taskId"] == "task-1")["spent"]["steps"], 1)
        audit_query = _load("audit_query_for_studio_proposal", "platform/core/audit_query.py")
        steps = audit_query.timeline(self.events(), task="task-1")["steps"]
        proposed = next(step for step in steps if step.get("action") == "studio.change-proposed")
        self.assertEqual((proposed["by"], proposed["details"]["id"]), ("alice@acme.example", change["id"]))

    def test_an_agent_has_no_more_authority_than_the_person(self):
        team = self.team()
        for principal, people in (("alice@acme.example", {}), ("mallory@acme.example", {"alice@acme.example": ["author"]})):
            envelope = self.task(team, task_id=f"task-{principal[:3]}", originating_principal=principal)
            with self.assertRaises(PermissionError):
                team.propose_change(envelope, self.store(team, people=people), "skill", self.PARAMS)

    def test_an_agent_never_changes_roles_even_for_the_owner(self):
        team = self.team()
        envelope = self.task(team, originating_principal=self.OWNER)
        store = self.store(team)
        with self.assertRaises(PermissionError):
            team.propose_change(envelope, store, "access", {"person": "alice@acme.example", "roles": ["author", "approver"]})
        self.assertEqual(store.list(), [])

    def test_an_agent_identity_cannot_approve(self):
        team = self.team()
        envelope = self.task(team)
        store = self.store(team)
        change = team.propose_change(envelope, store, "skill", self.PARAMS)
        for who in ("agent:coordinator", "alice@acme.example"):
            with self.assertRaises(PermissionError):
                store.approve(change["id"], by=who, digest=change["digest"])
        self.assertEqual(store.get(change["id"])["state"], "proposed")
        self.assertEqual(store.approve(change["id"], by=self.OWNER, digest=change["digest"])["state"], "applied")

    def test_the_proposal_is_charged_to_the_task_and_refused_when_it_cannot_run(self):
        team = self.team()
        store = self.store(team)
        envelope = self.task(team, budget={"steps": 1})
        team.propose_change(envelope, store, "skill", self.PARAMS)
        with self.assertRaises(PermissionError):
            team.propose_change(envelope, store, "skill", {"skill": "book-appointment", "enabled": False})
        finished = self.task(team, task_id="task-2")
        team.finish_task(finished)
        with self.assertRaises(PermissionError):
            team.propose_change(finished, store, "skill", self.PARAMS)
        self.assertEqual(len(store.list()), 1)

    def test_a_task_cannot_change_another_agents_configuration(self):
        team = self.team()
        envelope = self.task(team)
        other = self.store(team)
        other.desired = team.runtime("receptionist").desired_state
        with self.assertRaises(PermissionError):
            team.propose_change(envelope, other, "skill", self.PARAMS)

    def test_a_forged_envelope_is_refused(self):
        team = self.team()
        envelope = dict(self.task(team))
        envelope["originatingPrincipal"] = self.OWNER
        with self.assertRaises(team_runtime.TeamError):
            team.propose_change(envelope, self.store(team), "skill", self.PARAMS)

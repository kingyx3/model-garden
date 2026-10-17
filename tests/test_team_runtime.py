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

    def test_a_task_past_its_deadline_or_budget_halts(self):
        team = self.team()
        child = self.child(team, deadline=T0 + 60, budget={"steps": 2})
        with team.acting("receptionist", child) as runtime:
            runtime.kb_search(query="opening hours")
        self.clock[0] = T0 + 60
        with team.acting("receptionist", child) as runtime:
            with self.assertRaises(PermissionError):
                runtime.kb_search(query="opening hours")
        spent = delegation.charge(child, steps=2)
        self.clock[0] = T0
        with team.acting("receptionist", spent) as runtime:
            with self.assertRaises(PermissionError):
                runtime.kb_search(query="opening hours")
        reasons = [event["details"]["reason"] for event in self.events() if event.get("outcome") == "deny"]
        self.assertEqual(reasons, ["task-deadline-passed", "task-budget-exhausted"])

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

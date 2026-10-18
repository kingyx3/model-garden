"""Approval policy applies to each Agent on its own, including calls made under a delegated task (#114, #95, #96)."""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_docs_runtime import approval_module, principal_for  # noqa: E402 - shared fixtures
from test_team_runtime import BINDING, Case, states, team_runtime  # noqa: E402

policy = team_runtime.GOVERN.POLICY
OWNER = "owner@client.example"


def signed(rule: dict) -> dict:
    return {**rule, "signoff": {"owner": OWNER, "ruleDigest": policy.rule_digest(rule)}}


def document(*rules: dict) -> list[dict]:
    return [{"apiVersion": "modelgarden.ai/v1", "kind": "Policy", "metadata": {"name": "client-policy", "owner": "client"}, "spec": {"rules": list(rules)}}]


ALLOW_ONE_CREATE = signed({"id": "auto-create", "effect": "allow", "actions": ["crm.create"], "conditions": {"maxPerDay": 1}})


class PolicyCase(Case):
    def team(self, user="U-ALICE", policies=None, **kwargs):
        principal = principal_for(user)
        built = states()
        for state in built:
            if state["agent"]["metadata"]["name"] == "receptionist":
                state["policies"] = policies if policies is not None else document(ALLOW_ONE_CREATE)
        kwargs.setdefault("runtime_kwargs", {"principal_resolver": lambda: principal})
        kwargs.setdefault("environment_binding", BINDING)
        kwargs.setdefault("environ", {})
        return team_runtime.TeamRuntime(built, self.base, now=lambda: self.clock[0], **kwargs)

    def approve(self, team, agent, pending):
        approval_module.decide(team.agent_dir(agent) / "approvals", pending["request"]["requestId"], approved=True, approver=OWNER)

    def create(self, runtime, name):
        return runtime.crm_create(fields={"name": name})

    def delegated(self, team):
        root = team.start_task("coordinator", task_id="task-1", originating_principal="alice@acme.example",
                               originator_permissions=["crm.create", "kb.search"], goal="Add the new customer")
        return team.delegate(root, to_agent="receptionist", child_task_id="task-1.1", goal="Add the customer", required_capabilities=["crm.create"])


class IndependenceTests(PolicyCase):
    def test_one_agents_rule_does_not_apply_to_another_agent(self):
        team = self.team()
        self.assertEqual(self.create(team.runtime("receptionist"), "Alex Tan")["status"], "executed")  # its own signed rule
        self.assertEqual(self.create(team.runtime("coordinator"), "Sam Lee")["status"], "pending-approval")  # the same Tool, no rule: the default stands
        rule_ids = {(event["agent"], event["ledger"].get("policyRuleId")) for event in self.events() if event.get("event") == "authorization"}
        self.assertIn(("receptionist", "auto-create"), rule_ids)
        self.assertNotIn(("coordinator", "auto-create"), rule_ids)

    def test_one_agents_usage_does_not_use_up_another_agents_daily_allowance(self):
        team = self.team()
        coordinator = team.runtime("coordinator")
        pending = self.create(coordinator, "Sam Lee")
        self.approve(team, "coordinator", pending)
        self.assertEqual(self.create(coordinator, "Sam Lee")["status"], "executed")  # the coordinator has done one create today
        self.assertEqual(self.create(team.runtime("receptionist"), "Alex Tan")["status"], "executed")  # the receptionist's allowance is untouched
        self.assertEqual(self.create(team.runtime("receptionist"), "Jo Park")["status"], "pending-approval")  # its own limit of one is reached

    def test_each_agent_is_ledgered_with_its_own_policy_version(self):
        team = self.team()
        self.create(team.runtime("receptionist"), "Alex Tan")
        self.create(team.runtime("coordinator"), "Sam Lee")
        versions = {event["agent"]: event["ledger"]["policyVersion"] for event in self.events() if event.get("event") == "authorization"}
        self.assertEqual(set(versions), {"receptionist", "coordinator"})
        self.assertNotEqual(versions["receptionist"], versions["coordinator"])


class DelegationTests(PolicyCase):
    def test_a_delegated_call_follows_the_delegates_policy_and_carries_the_task(self):
        team = self.team()
        child = self.delegated(team)
        with team.acting("receptionist", child) as runtime:
            result = self.create(runtime, "Alex Tan")
        self.assertEqual(result["status"], "executed")  # the receptionist's own rule allowed it
        allowed = [event for event in self.events() if event.get("event") == "authorization" and event.get("agent") == "receptionist"][-1]
        self.assertEqual((allowed["ledger"]["policyRuleId"], allowed["governance"]["taskId"], allowed["governance"]["delegationChain"]),
                         ("auto-create", "task-1.1", ["coordinator", "receptionist"]))

    def test_delegating_does_not_borrow_the_coordinators_standing_or_skip_approval(self):
        team = self.team(policies=document())  # no rule anywhere: writes need a person
        child = self.delegated(team)
        with team.acting("receptionist", child) as runtime:
            result = self.create(runtime, "Alex Tan")
        self.assertEqual(result["status"], "pending-approval")
        self.assertEqual(len(list((team.agent_dir("receptionist") / "approvals" / "pending").glob("*.json"))), 1)
        self.assertEqual(list((team.agent_dir("coordinator") / "approvals" / "pending").glob("*.json")), [])  # asked of the Agent that acts, not the one that delegated

    def test_a_public_requester_is_not_elevated_by_the_task_or_by_a_rule_that_does_not_name_public(self):
        team = self.team(user="U-STRANGER")
        root = team.start_task("coordinator", task_id="task-9", originating_principal="unmapped:slack:stranger",
                               originator_permissions=["crm.create", "kb.search"], goal="Add me as a customer")
        child = team.delegate(root, to_agent="receptionist", child_task_id="task-9.1", goal="Add the customer", required_capabilities=["crm.create"])
        with team.acting("receptionist", child) as runtime:
            with self.assertRaises(PermissionError):  # the standing comes from the verified channel principal, not the envelope
                self.create(runtime, "Mallory")
        denial = [event for event in self.events() if event.get("outcome") == "deny"][-1]
        self.assertEqual(denial["details"]["reason"], "principal-not-authorized")
        self.assertEqual(denial["governance"]["taskId"], "task-9.1")

    def test_a_relaxing_rule_without_the_owners_signoff_is_refused_for_every_agent(self):
        unsigned = [{**document(ALLOW_ONE_CREATE)[0], "spec": {"rules": [{"id": "sneaky", "effect": "allow", "actions": ["crm.create"]}]}}]
        team = self.team(policies=unsigned)
        child = self.delegated(team)
        with team.acting("receptionist", child) as runtime:
            result = self.create(runtime, "Alex Tan")
        self.assertEqual(result["status"], "pending-approval")  # the unsigned relaxation never applied


if __name__ == "__main__":
    unittest.main()

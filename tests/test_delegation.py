"""Credential-free checks that delegation between Agents can never escalate authority."""
from __future__ import annotations

import copy
import importlib.util
import itertools
import json
import pathlib
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


delegation = _load("delegation_under_test", "platform/core/delegation.py")
govern = _load("govern_for_delegation", "scripts/govern-action.py")
Error = delegation.DelegationError

ALL = ["calendar.availability", "calendar.book", "crm.create", "crm.log-interaction", "kb.search", "mail.send"]
COORDINATOR = ALL
RESEARCHER = ["kb.search", "crm.log-interaction", "calendar.availability"]
ALICE = ["calendar.availability", "calendar.book", "kb.search", "crm.log-interaction"]  # may not send mail or create contacts
NOW = 1_790_000_000


def root(**overrides):
    args = dict(task_id="t-root", agent="coordinator", originating_principal="alice@client.example", goal="Handle Alex's booking request",
                agent_grants=COORDINATOR, originator_permissions=ALICE, budget={"tokens": 1000, "steps": 10}, deadline=NOW + 3600)
    args.update(overrides)
    return delegation.root_envelope(**args)


def hop(parent, **overrides):
    args = dict(to_agent="researcher", child_task_id="t-1", goal="Find the hours", delegate_grants=RESEARCHER,
                originator_permissions=ALICE, required_capabilities=["kb.search"])
    args.update(overrides)
    return delegation.delegate(parent, **args)


class EnvelopeTests(unittest.TestCase):
    def test_the_root_authority_is_the_requesters_permissions_narrowed_to_the_first_agent(self):
        envelope = root()
        self.assertEqual(envelope["authority"], sorted(ALICE))
        self.assertNotIn("mail.send", envelope["authority"])  # the coordinator holds it, the requester does not
        self.assertEqual((envelope["depth"], envelope["delegationChain"], envelope["parentTaskId"]), (0, ["coordinator"], None))

    def test_a_task_that_needs_what_requester_and_agent_do_not_both_hold_is_refused_at_the_root(self):
        with self.assertRaisesRegex(Error, "mail.send"):
            root(required_capabilities=["mail.send"])

    def test_malformed_envelopes_are_refused(self):
        for bad in ({"goal": ""}, {"goal": "x" * 501}, {"task_id": "bad id"}, {"agent": ""}, {"budget": {"gold": 1}},
                    {"budget": {"tokens": -1}}, {"budget": {"tokens": True}}, {"deadline": -5}, {"deadline": "soon"},
                    {"agent_grants": ["not a tool"]}, {"constraints": [""]}, {"constraints": ["x"] * 11}):
            with self.assertRaises(Error, msg=str(bad)):
                root(**bad)


class AttenuationTests(unittest.TestCase):
    def test_a_delegate_gets_exactly_the_intersection_of_all_three(self):
        child = hop(root(), delegate_grants=RESEARCHER + ["mail.send", "crm.create"])
        self.assertEqual(child["authority"], sorted(set(RESEARCHER) & set(ALICE)))
        self.assertNotIn("mail.send", child["authority"])  # the requester could not have asked for it
        self.assertNotIn("calendar.book", child["authority"])  # the delegate does not hold it
        self.assertEqual(delegation.effective_authority(RESEARCHER, COORDINATOR, ALICE), frozenset(set(RESEARCHER) & set(ALICE)))

    def test_asking_a_delegate_for_something_outside_its_authority_is_refused_not_narrowed(self):
        # calendar.book: the requester may, but this delegate holds no such grant.
        with self.assertRaisesRegex(Error, "calendar.book"):
            hop(root(), required_capabilities=["calendar.book"], delegate_grants=RESEARCHER)
        # mail.send and crm.create: the delegate holds them, the requester could not have asked for them.
        for tool in ("mail.send", "crm.create"):
            with self.assertRaisesRegex(Error, tool):
                hop(root(), required_capabilities=[tool], delegate_grants=ALL)
        # nothing the requester may do is refused when the delegate holds it too
        self.assertEqual(hop(root(), required_capabilities=["calendar.book"], delegate_grants=ALL)["authority"], sorted(ALICE))

    def test_the_originating_principals_permissions_are_rechecked_at_every_hop(self):
        parent = root()
        with self.assertRaisesRegex(Error, "calendar.availability"):
            hop(parent, originator_permissions=["kb.search"], required_capabilities=["calendar.availability"])
        narrowed = hop(parent, originator_permissions=["kb.search"])
        self.assertEqual(narrowed["authority"], ["kb.search"])

    def test_authority_never_grows_along_any_chain_of_hops(self):
        parent, authorities = root(budget={}), []
        agents = ["a1", "a2", "a3"]
        for index, name in enumerate(agents):
            parent = hop(parent, to_agent=name, child_task_id=f"t-{index}", delegate_grants=ALL, required_capabilities=[],
                         max_depth=5)
            authorities.append(set(parent["authority"]))
        for earlier, later in zip(authorities, authorities[1:]):
            self.assertTrue(later <= earlier)
        self.assertTrue(authorities[0] <= set(ALICE))

    def test_exhaustive_check_no_combination_of_grants_escapes_the_requester_or_the_parent(self):
        tools = ["calendar.book", "mail.send", "kb.search"]
        subsets = [list(combo) for size in range(4) for combo in itertools.combinations(tools, size)]
        for coordinator, requester, specialist in itertools.product(subsets, subsets, subsets):
            try:
                parent = delegation.root_envelope(task_id="t", agent="c", originating_principal="p", goal="g",
                                                  agent_grants=coordinator, originator_permissions=requester)
                child = delegation.delegate(parent, to_agent="s", child_task_id="t2", goal="g", delegate_grants=specialist,
                                            originator_permissions=requester)
            except Error:
                continue
            self.assertTrue(set(child["authority"]) <= set(requester) & set(coordinator) & set(specialist))
            self.assertTrue(set(child["authority"]) <= set(parent["authority"]))

    def test_a_child_cannot_change_the_originating_principal_or_inherit_a_wider_set(self):
        parent = root()
        child = hop(parent)
        self.assertEqual(child["originatingPrincipal"], parent["originatingPrincipal"])
        self.assertTrue(set(child["authority"]) <= set(parent["authority"]))
        self.assertEqual((child["parentTaskId"], child["depth"], child["delegationChain"]), ("t-root", 1, ["coordinator", "researcher"]))

    def test_the_parent_is_never_modified(self):
        parent = root()
        before = copy.deepcopy(parent)
        hop(parent)
        delegation.charge(parent, tokens=5)
        self.assertEqual(parent, before)


class BoundTests(unittest.TestCase):
    def test_loops_and_self_delegation_are_stopped(self):
        parent = hop(root(), to_agent="researcher", required_capabilities=[])
        with self.assertRaisesRegex(Error, "loop"):
            hop(parent, to_agent="coordinator", required_capabilities=[])
        with self.assertRaisesRegex(Error, "loop"):
            hop(parent, to_agent="researcher", required_capabilities=[])

    def test_depth_is_bounded(self):
        parent = root(budget={})
        for index, name in enumerate(["a1", "a2", "a3"]):
            parent = hop(parent, to_agent=name, child_task_id=f"t-{index}", required_capabilities=[])
        self.assertEqual(parent["depth"], 3)
        with self.assertRaisesRegex(Error, "4 deep"):
            hop(parent, to_agent="a4", required_capabilities=[])
        deeper = hop(parent, to_agent="a4", required_capabilities=[], max_depth=4)
        self.assertEqual(deeper["depth"], 4)

    def test_fan_out_is_bounded(self):
        parent = root()
        hop(parent, children_so_far=4, required_capabilities=[])
        with self.assertRaisesRegex(Error, "limit is 5"):
            hop(parent, children_so_far=5, required_capabilities=[])
        for bad in (-1, True, 1.5):
            with self.assertRaises(Error):
                hop(parent, children_so_far=bad, required_capabilities=[])

    def test_limits_themselves_are_validated(self):
        for kwargs in ({"max_depth": 0}, {"max_depth": 99}, {"max_fan_out": 0}, {"max_fan_out": 99}, {"max_depth": True}):
            with self.assertRaises(Error, msg=str(kwargs)):
                hop(root(), required_capabilities=[], **kwargs)

    def test_a_child_budget_can_only_be_tighter_and_defaults_to_what_is_left(self):
        parent = delegation.charge(root(), tokens=400, steps=3)
        self.assertEqual(delegation.remaining(parent), {"tokens": 600, "steps": 7})
        self.assertEqual(hop(parent, required_capabilities=[])["budget"], {"steps": 7, "tokens": 600})
        self.assertEqual(hop(parent, required_capabilities=[], budget={"tokens": 100})["budget"], {"steps": 7, "tokens": 100})
        with self.assertRaisesRegex(Error, "more than the parent has left"):
            hop(parent, required_capabilities=[], budget={"tokens": 601})
        with self.assertRaises(Error):
            hop(parent, required_capabilities=[], budget={"steps": 8})

    def test_a_dimension_the_parent_does_not_bound_may_be_bounded_by_the_child(self):
        child = hop(root(budget={"tokens": 10}), required_capabilities=[], budget={"costCents": 50})
        self.assertEqual(child["budget"], {"costCents": 50, "tokens": 10})

    def test_exhaustion_halts_delegation_and_is_reported_by_dimension(self):
        parent = delegation.charge(root(), tokens=1000)
        self.assertEqual(delegation.exhausted(parent), ["tokens"])
        with self.assertRaisesRegex(Error, "exhausted: tokens"):
            hop(parent, required_capabilities=[])
        over = delegation.charge(parent, tokens=50)
        self.assertEqual(delegation.remaining(over)["tokens"], 0)
        with self.assertRaises(Error):
            delegation.charge(parent, tokens=-1)

    def test_the_child_deadline_cannot_outlive_the_parents_and_a_passed_deadline_blocks_delegation(self):
        parent = root()
        self.assertEqual(hop(parent, required_capabilities=[])["deadline"], NOW + 3600)
        self.assertEqual(hop(parent, required_capabilities=[], deadline=NOW + 60)["deadline"], NOW + 60)
        with self.assertRaisesRegex(Error, "later than the parent"):
            hop(parent, required_capabilities=[], deadline=NOW + 3601)
        with self.assertRaisesRegex(Error, "deadline has passed"):
            hop(parent, required_capabilities=[], now=NOW + 3600)
        self.assertTrue(hop(parent, required_capabilities=[], now=NOW + 3599))

    def test_constraints_accumulate_down_the_chain_and_cannot_be_dropped(self):
        parent = root(constraints=["never promise a price"])
        child = hop(parent, required_capabilities=[], constraints=["be brief"])
        self.assertEqual(child["constraints"], ["never promise a price", "be brief"])
        grandchild = hop(child, to_agent="third", child_task_id="t-2", required_capabilities=[])
        self.assertEqual(grandchild["constraints"], child["constraints"])

    def test_malformed_hops_are_refused(self):
        for bad in ({"to_agent": ""}, {"child_task_id": "bad id"}, {"goal": ""}, {"delegate_grants": ["nope"]}):
            with self.assertRaises(Error, msg=str(bad)):
                hop(root(), **bad)


class GovernanceTests(unittest.TestCase):
    def test_a_runtime_checks_tools_against_the_task_authority(self):
        child = hop(root())
        self.assertTrue(delegation.permits(child, "kb.search"))
        for tool in ("calendar.book", "mail.send", "made.up"):
            self.assertFalse(delegation.permits(child, tool))

    def test_the_context_is_accepted_by_the_governed_boundary_and_binds_the_request(self):
        child = hop(root())
        context = delegation.governance_context(child, parent_request_id="a" * 64)
        accepted = govern._governance_context(context)
        self.assertEqual((accepted["originatingPrincipal"], accepted["taskId"], accepted["delegationChain"]),
                         ("alice@client.example", "t-1", ["coordinator", "researcher"]))
        desired = {"source": {"name": "researcher"}, "agent": {"spec": {}}, "tools": [
            {"metadata": {"name": "kb.search"}, "spec": {"type": "internal", "risk": "read"}}]}
        direct = govern.build_action_request(desired, "kb.search", {"query": "hours"})
        delegated = govern.build_action_request(desired, "kb.search", {"query": "hours"}, governance_context=context)
        self.assertNotEqual(direct["requestId"], delegated["requestId"])

    def test_every_hop_is_a_ledger_event_with_a_digest_of_the_goal_not_the_goal(self):
        parent, child = root(goal="Alex Tan at 12 Acacia Road wants a boiler visit"), None
        child = hop(parent, goal="Look up the boiler visit hours for Alex Tan")
        with tempfile.TemporaryDirectory() as tmp:
            audit = pathlib.Path(tmp) / "audit.jsonl"
            event = delegation.record_hop(govern, audit, parent, child)
            text = audit.read_text()
            self.assertTrue(govern.verify_audit_chain(audit)["valid"])
        self.assertEqual((event["action"], event["performedBy"]), ("delegation.hop", "agent:coordinator"))
        details = json.loads(text)["details"]
        self.assertEqual((details["fromAgent"], details["toAgent"], details["depth"], details["chain"]), ("coordinator", "researcher", 1, ["coordinator", "researcher"]))
        for private in ("Alex", "Acacia", "boiler"):
            self.assertNotIn(private, text)


if __name__ == "__main__":
    unittest.main()

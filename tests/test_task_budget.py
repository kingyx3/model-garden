"""Credential-free checks for the shared budget across a delegated task tree (#96)."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


budget = _load("task_budget_under_test", "platform/core/task_budget.py")
delegation = _load("delegation_for_budget", "platform/core/delegation.py")
Error, Exhausted = budget.TaskBudgetError, budget.BudgetExhausted
OWNER = "alice@client.example"
GRANTS = ["crm.read", "mail.draft"]
GOAL = "Research Hargreaves & Co and draft a reply"


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.events: list[tuple] = []
        self.notices: list[dict] = []
        root = delegation.root_envelope(task_id="t1", agent="coordinator", originating_principal=OWNER, goal=GOAL, agent_grants=GRANTS,
                                        originator_permissions=GRANTS, budget={"tokens": 100, "steps": 10})
        self.tree = budget.TaskBudgetTree(root, audit=lambda *event: self.events.append(event), notify=self.notices.append)

    def delegate(self, parent, child, agent, **budget_override):
        return self.tree.delegate(parent, to_agent=agent, child_task_id=child, goal=f"{GOAL} ({child})", delegate_grants=GRANTS,
                                  originator_permissions=GRANTS, budget={"tokens": 40, "steps": 3, **budget_override})

    def available(self, task="t1"):
        return next(row for row in self.tree.timeline() if row["taskId"] == task)["available"]


class ReservationTests(Case):
    def test_children_cannot_be_given_more_than_the_parent_has_left(self):
        self.delegate("t1", "t1.a", "researcher", tokens=60)
        self.assertEqual(self.available(), {"tokens": 40, "steps": 7})
        with self.assertRaisesRegex(Error, "more than the parent has left"):
            self.delegate("t1", "t1.b", "writer", tokens=60)  # the old behaviour would have allowed 60 + 60 of a 100 budget
        self.delegate("t1", "t1.b", "writer", tokens=40)
        self.assertEqual(self.available()["tokens"], 0)
        self.assertEqual(sum(row["budget"]["tokens"] for row in self.tree.timeline() if row["parentTaskId"] == "t1"), 100)

    def test_unused_budget_returns_to_the_parent_when_a_child_finishes_and_used_budget_is_counted_once(self):
        self.delegate("t1", "t1.a", "researcher", tokens=60)
        self.tree.charge("t1.a", tokens=25, steps=1)
        self.assertEqual(self.tree.total_spent(), {"tokens": 25, "steps": 1})
        self.tree.finish("t1.a")
        self.assertEqual(self.available(), {"tokens": 75, "steps": 9})  # 100 - 25 used, the other 35 is back
        self.assertEqual(self.tree.total_spent(), {"tokens": 25, "steps": 1})
        self.delegate("t1", "t1.b", "writer", tokens=75, steps=9)  # the returned budget can be delegated again
        self.assertEqual(self.available()["tokens"], 0)

    def test_a_task_charging_itself_leaves_less_to_delegate(self):
        self.tree.charge("t1", tokens=70)
        with self.assertRaisesRegex(Error, "more than the parent has left"):
            self.delegate("t1", "t1.a", "researcher", tokens=40)
        self.delegate("t1", "t1.a", "researcher", tokens=30)

    def test_the_rules_of_delegation_still_apply_through_the_tree(self):
        self.delegate("t1", "t1.a", "researcher")
        for call in (lambda: self.delegate("t1.a", "t1.a.x", "coordinator"),  # a loop
                     lambda: self.delegate("t1", "t1.a", "writer"),  # a task id already used
                     lambda: self.delegate("nope", "x", "writer"),
                     lambda: self.tree.delegate("t1", to_agent="writer", child_task_id="t1.c", goal="g", delegate_grants=["crm.read"],
                                                originator_permissions=["crm.read"], required_capabilities=["mail.draft"], budget={"tokens": 1, "steps": 1})):
            with self.assertRaises(Error):
                call()

    def test_fan_out_counts_every_child_ever_delegated_to(self):
        for number in range(delegation.DEFAULT_MAX_FAN_OUT):
            self.tree.delegate("t1", to_agent=f"agent{number}", child_task_id=f"t1.{number}", goal="g", delegate_grants=GRANTS,
                               originator_permissions=GRANTS, budget={"tokens": 1, "steps": 1})
            self.tree.finish(f"t1.{number}")
        with self.assertRaisesRegex(Error, "already delegated"):
            self.delegate("t1", "t1.extra", "late")


class ExhaustionTests(Case):
    def test_charging_past_the_budget_records_it_halts_the_task_and_tells_the_owner_once(self):
        self.delegate("t1", "t1.a", "researcher", tokens=30)
        with self.assertRaises(Exhausted) as raised:
            self.tree.charge("t1.a", tokens=31)
        self.assertEqual((raised.exception.task_id, raised.exception.keys), ("t1.a", ["tokens"]))
        self.assertEqual(self.tree.state("t1.a"), "halted")
        self.assertEqual(len(self.notices), 1)
        self.assertEqual((self.notices[0]["to"], self.notices[0]["kind"], self.notices[0]["taskId"]), (OWNER, "task-budget-exhausted", "t1.a"))
        with self.assertRaises(Error):
            self.tree.charge("t1.a", tokens=1)
        self.assertEqual(len(self.notices), 1)

    def test_spending_exactly_the_budget_is_allowed(self):
        self.delegate("t1", "t1.a", "researcher", tokens=30)
        self.assertEqual(self.tree.charge("t1.a", tokens=30)["tokens"], 0)
        self.assertEqual(self.tree.state("t1.a"), "running")

    def test_a_halted_branch_does_not_stop_its_sibling_and_its_overspend_is_counted(self):
        self.delegate("t1", "t1.a", "researcher", tokens=30, steps=3)
        self.delegate("t1", "t1.b", "writer", tokens=30, steps=3)
        with self.assertRaises(Exhausted):
            self.tree.charge("t1.a", tokens=34)
        self.assertEqual((self.tree.state("t1.a"), self.tree.state("t1.b"), self.tree.state("t1")), ("halted", "running", "running"))
        self.tree.charge("t1.b", tokens=10)
        self.assertEqual(self.tree.total_spent()["tokens"], 44)
        self.assertEqual(self.available()["tokens"], 100 - 34 - 30)  # a's actual use, b's reservation

    def test_halting_a_task_halts_everything_delegated_under_it(self):
        self.delegate("t1", "t1.a", "researcher", tokens=50, steps=5)
        self.tree.delegate("t1.a", to_agent="analyst", child_task_id="t1.a.x", goal="g", delegate_grants=GRANTS, originator_permissions=GRANTS,
                           budget={"tokens": 20, "steps": 2})
        with self.assertRaises(Exhausted):
            self.tree.charge("t1.a", tokens=31)  # the parent has only 30 available after reserving 20 for its child
        self.assertEqual((self.tree.state("t1.a"), self.tree.state("t1.a.x")), ("halted", "halted"))
        with self.assertRaises(Error):
            self.tree.charge("t1.a.x", tokens=1)

    def test_a_branch_that_overspends_past_its_parents_budget_halts_the_parent_too(self):
        self.tree.charge("t1", tokens=60)
        self.delegate("t1", "t1.a", "researcher", tokens=40, steps=3)
        with self.assertRaises(Exhausted):
            self.tree.charge("t1.a", tokens=48)  # 8 over its reservation: the tree has now used 108 of 100
        self.assertEqual((self.tree.state("t1.a"), self.tree.state("t1")), ("halted", "halted"))
        self.assertEqual([notice["taskId"] for notice in self.notices], ["t1.a", "t1"])
        self.assertEqual([event[0] for event in self.events].count("task.budget-exhausted"), 2)

    def test_the_ledger_event_names_the_chain_and_the_numbers_never_the_goal(self):
        self.delegate("t1", "t1.a", "researcher", tokens=30)
        with self.assertRaises(Exhausted):
            self.tree.charge("t1.a", tokens=31)
        event = next(event for event in self.events if event[0] == "task.budget-exhausted")
        self.assertEqual(event[1], "agent:researcher")
        self.assertEqual((event[2]["chain"], event[2]["exceeded"], event[2]["budget"]["tokens"], event[2]["spent"]["tokens"]),
                         (["coordinator", "researcher"], ["tokens"], 30, 31))
        self.assertNotIn("Hargreaves", json.dumps(self.events) + json.dumps(self.notices))

    def test_a_failing_notifier_does_not_undo_the_halt(self):
        def broken(_notice):
            raise RuntimeError("down")

        self.tree._notify = broken
        with self.assertRaises(Exhausted):
            self.tree.charge("t1", tokens=101)
        self.assertEqual(self.tree.state("t1"), "halted")

    def test_a_halted_or_finished_task_cannot_delegate_or_finish_again(self):
        self.delegate("t1", "t1.a", "researcher", tokens=30)
        self.tree.finish("t1.a")
        for call in (lambda: self.tree.finish("t1.a"), lambda: self.delegate("t1.a", "t1.a.x", "analyst"), lambda: self.tree.charge("t1.a", tokens=1)):
            with self.assertRaises(Error):
                call()

    def test_a_task_cannot_finish_while_a_delegated_task_is_running(self):
        self.delegate("t1", "t1.a", "researcher")
        with self.assertRaisesRegex(Error, "still running"):
            self.tree.finish("t1")
        self.tree.finish("t1.a")
        self.tree.finish("t1")

    def test_a_malformed_usage_record_is_refused_and_changes_nothing(self):
        for bad in ({"tokens": -1}, {"bogus": 1}, {"tokens": 1.5}):
            with self.assertRaises(Error, msg=str(bad)):
                self.tree.charge("t1", **bad)
        self.assertEqual(self.available(), {"tokens": 100, "steps": 10})


class ViewTests(Case):
    def test_the_timeline_shows_cost_and_state_per_branch(self):
        self.delegate("t1", "t1.a", "researcher", tokens=60, steps=5)
        self.tree.charge("t1.a", tokens=10, steps=2)
        rows = {row["taskId"]: row for row in self.tree.timeline()}
        self.assertEqual((rows["t1.a"]["agent"], rows["t1.a"]["parentTaskId"], rows["t1.a"]["spent"], rows["t1.a"]["available"]),
                         ("researcher", "t1", {"tokens": 10, "steps": 2}, {"tokens": 50, "steps": 3}))
        self.assertEqual(rows["t1"]["reserved"], {"tokens": 60, "steps": 5})

    def test_a_snapshot_restores_the_same_tree(self):
        self.delegate("t1", "t1.a", "researcher", tokens=60, steps=5)
        self.tree.charge("t1.a", tokens=10)
        restored = budget.TaskBudgetTree.restore(json.loads(json.dumps(self.tree.snapshot())))
        self.assertEqual(restored.timeline(), self.tree.timeline())
        self.assertEqual(restored.total_spent(), self.tree.total_spent())
        with self.assertRaises(Exhausted):
            restored.charge("t1.a", tokens=60)
        self.assertEqual(self.tree.state("t1.a"), "running")  # the original is unaffected
        with self.assertRaises(Error):
            budget.TaskBudgetTree.restore({"nodes": {}})

    def test_the_tree_must_start_from_a_root_envelope(self):
        child = self.tree.delegate("t1", to_agent="writer", child_task_id="c", goal="g", delegate_grants=GRANTS, originator_permissions=GRANTS,
                                   budget={"tokens": 1, "steps": 1})
        with self.assertRaises(Error):
            budget.TaskBudgetTree(child)


if __name__ == "__main__":
    unittest.main()

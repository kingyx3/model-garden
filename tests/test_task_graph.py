"""Credential-free checks for the multi-Agent task-graph executor: attenuation, shared budget, parallelism, handoff, ledger."""
from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


graph = _load("task_graph_under_test", "platform/core/task_graph.py")
handoff = graph._handoff()
Error, Violation = graph.TaskGraphError, graph.AuthorityViolation
T0 = 1_800_000_000
ALICE = "alice@client.example"
JORDAN = "jordan@client.example"
GRANTS = {
    "coordinator": ["crm.read", "docs.create", "files.read"],
    "researcher": ["crm.read", "kb.search", "files.read"],
    "writer": ["docs.create", "docs.read", "crm.read"],
    "approver": ["crm.read"],
}
PERMISSIONS = {ALICE: ["crm.read", "docs.create", "kb.search", "files.read"]}


def spec(**changes):
    value = {
        "id": "quote-42", "principal": ALICE, "rootAgent": "coordinator", "goal": "Quote the customer SECRET-GOAL",
        "budget": {"tokens": 1000, "steps": 20}, "failurePolicy": "cancel",
        "nodes": [
            {"id": "research", "agent": "researcher", "goal": "Find the account history", "requires": ["crm.read"]},
            {"id": "draft", "agent": "writer", "goal": "Draft the quote", "dependsOn": ["research"], "requires": ["docs.create"]},
        ],
    }
    value.update(changes)
    return value


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = pathlib.Path(self._tmp.name)
        self.events: list[tuple[str, str, dict]] = []
        self.notices: list[dict] = []
        self.now = T0
        self.grants = copy.deepcopy(GRANTS)
        self.permissions = copy.deepcopy(PERMISSIONS)
        self.lock = threading.Lock()
        self.store = self.make_store()

    def make_store(self, **overrides):
        def audit(action, by, details):
            with self.lock:
                self.events.append((action, by, details))

        options = dict(
            grants_for=lambda agent: self.grants[agent], permissions_for=lambda who: self.permissions.get(who, []),
            handoff_root=self.dir / "handoffs", audit=audit, notify=self.notices.append, clock=lambda: self.now,
        )
        options.update(overrides)
        return graph.TaskGraphStore(self.dir / "tasks", **options)

    def actions(self, prefix: str = "") -> list[str]:
        return [action for action, _, _ in self.events if action.startswith(prefix)]

    def states(self, summary) -> dict[str, str]:
        return {node["id"]: node["state"] for node in summary["nodes"]}


class SubmitAndAttenuation(Case):
    def test_envelopes_are_attenuated_from_grants_and_requester(self):
        result = self.store.submit(spec())
        doc = self.store._load("quote-42")
        research, draft = doc["nodes"]["research"]["envelope"], doc["nodes"]["draft"]["envelope"]
        # researcher holds kb.search too, but the delegator (coordinator) and the task do not: never wider than the parent.
        self.assertEqual(research["authority"], ["crm.read", "files.read"])
        self.assertEqual(draft["authority"], ["crm.read", "docs.create"])
        self.assertNotIn("docs.read", draft["authority"])  # the writer has it, the requester does not
        for envelope in (research, draft):
            self.assertLessEqual(set(envelope["authority"]), set(doc["rootEnvelope"]["authority"]))
            self.assertEqual(envelope["originatingPrincipal"], ALICE)
        self.assertEqual(draft["delegationChain"], ["coordinator", "writer"])
        self.assertEqual(result["status"], "running")
        self.assertEqual(self.actions("delegation.hop"), ["delegation.hop", "delegation.hop"])

    def test_tool_the_requester_lacks_rejects_the_whole_graph_and_stores_nothing(self):
        bad = spec(nodes=[spec()["nodes"][0], {"id": "docs", "agent": "writer", "goal": "Read docs", "requires": ["docs.read"]}])
        with self.assertRaises(Violation):
            self.store.submit(bad)
        self.assertEqual(self.store.graphs(), [])
        rejected = [d for a, _, d in self.events if a == "taskgraph.rejected"]
        self.assertEqual(rejected[0]["reason"], "authority")
        self.assertEqual(self.actions("delegation.hop"), [])  # no hop was ledgered for a graph that never existed

    def test_tool_the_delegator_lacks_is_refused(self):
        self.grants["writer"].append("files.write")
        self.permissions[ALICE].append("files.write")  # requester and delegate hold it, the coordinator does not
        bad = spec(nodes=[{"id": "w", "agent": "writer", "goal": "Write", "requires": ["files.write"]}])
        with self.assertRaisesRegex(Violation, "files.write"):
            self.store.submit(bad)

    def test_node_budget_cannot_exceed_pool_or_add_a_key(self):
        with self.assertRaisesRegex(Violation, "more than the parent"):
            self.store.submit(spec(nodes=[{"id": "a", "agent": "researcher", "goal": "g", "budget": {"tokens": 5000}}]))
        with self.assertRaisesRegex(Violation, "does not have"):
            self.store.submit(spec(nodes=[{"id": "a", "agent": "researcher", "goal": "g", "budget": {"costCents": 5}}]))

    def test_node_expiry_cannot_outlive_the_graph(self):
        with self.assertRaisesRegex(Violation, "later than"):
            self.store.submit(spec(ttlSeconds=100, nodes=[{"id": "a", "agent": "researcher", "goal": "g", "deadline": T0 + 500}]))

    def test_delegating_back_to_an_agent_in_the_chain_is_a_loop(self):
        with self.assertRaisesRegex(Violation, "loop"):
            self.store.submit(spec(nodes=[{"id": "a", "agent": "coordinator", "goal": "g"}]))

    def test_fan_out_and_depth_limits(self):
        many = [{"id": f"n{i}", "agent": "researcher", "goal": "g"} for i in range(6)]
        with self.assertRaisesRegex(Violation, "limit"):
            self.store.submit(spec(nodes=many))
        self.store.submit(spec(id="wide", limits={"maxFanOut": 10}, nodes=many))
        deep = [{"id": "a", "agent": "researcher", "goal": "g"},
                {"id": "b", "agent": "writer", "goal": "g", "parent": "a"},
                {"id": "c", "agent": "approver", "goal": "g", "parent": "b"}]
        with self.assertRaisesRegex(Violation, "deep"):
            self.store.submit(spec(id="deep", limits={"maxDepth": 2}, nodes=deep))

    def test_parent_delegation_attenuates_again(self):
        nodes = [{"id": "a", "agent": "researcher", "goal": "g", "requires": ["crm.read"]},
                 {"id": "b", "agent": "writer", "goal": "g", "parent": "a", "requires": ["docs.create"]}]
        with self.assertRaisesRegex(Violation, "docs.create"):  # a's authority has no docs.create, so b cannot get it from a
            self.store.submit(spec(nodes=nodes))
        nodes[1]["requires"] = ["crm.read"]
        self.store.submit(spec(nodes=nodes))
        self.assertEqual(self.store._load("quote-42")["nodes"]["b"]["envelope"]["authority"], ["crm.read"])

    def test_structure_errors(self):
        cycle = [{"id": "a", "agent": "researcher", "goal": "g", "dependsOn": ["b"]}, {"id": "b", "agent": "writer", "goal": "g", "dependsOn": ["a"]}]
        with self.assertRaisesRegex(Error, "cycle"):
            self.store.submit(spec(nodes=cycle))
        with self.assertRaisesRegex(Error, "itself"):
            self.store.submit(spec(nodes=[{"id": "a", "agent": "researcher", "goal": "g", "dependsOn": ["a"]}]))
        with self.assertRaisesRegex(Error, "unknown"):
            self.store.submit(spec(nodes=[{"id": "a", "agent": "researcher", "goal": "g", "dependsOn": ["zzz"]}]))
        with self.assertRaisesRegex(Error, "duplicate"):
            self.store.submit(spec(nodes=[{"id": "a", "agent": "researcher", "goal": "g"}, {"id": "a", "agent": "writer", "goal": "g"}]))
        with self.assertRaisesRegex(Error, "budget"):
            self.store.submit(spec(budget={}))
        with self.assertRaisesRegex(Error, "unsupported"):
            self.store.submit(spec(extra=1))
        with self.assertRaisesRegex(Error, "unsupported"):
            self.store.submit(spec(nodes=[{"id": "a", "agent": "researcher", "goal": "g", "authority": ["everything.do"]}]))
        self.assertEqual(self.store.graphs(), [])

    def test_submit_is_idempotent_but_not_overwritable(self):
        first = self.store.submit(spec())
        self.assertEqual(self.store.submit(spec()), first)
        with self.assertRaisesRegex(Error, "different spec"):
            self.store.submit(spec(goal="Something else"))


class Execution(Case):
    def test_runs_in_dependency_order_and_passes_outputs(self):
        self.store.submit(spec())
        order = []

        def runner(ctx):
            order.append((ctx.node_id, dict(ctx.inputs)))
            ctx.draw(tokens=10, steps=1)
            return {"from": ctx.node_id}

        result = self.store.advance("quote-42", runner)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual([item[0] for item in order], ["research", "draft"])
        self.assertEqual(order[1][1], {"research": {"from": "research"}})
        self.assertEqual(result["spent"], {"tokens": 20, "steps": 2})
        self.assertEqual(result["remaining"], {"tokens": 980, "steps": 18})

    def test_independent_nodes_really_run_in_parallel(self):
        nodes = [{"id": f"p{i}", "agent": "researcher", "goal": "g"} for i in range(3)]
        nodes.append({"id": "join", "agent": "writer", "goal": "g", "dependsOn": ["p0", "p1", "p2"]})
        self.store.submit(spec(limits={"maxFanOut": 5}, nodes=nodes))
        barrier = threading.Barrier(3, timeout=10)  # all three must be in flight at once or the barrier breaks

        def runner(ctx):
            if ctx.node_id.startswith("p"):
                barrier.wait()
            return {}

        self.assertEqual(self.store.advance("quote-42", runner, max_workers=4)["status"], "succeeded")

    def test_max_workers_bounds_parallelism(self):
        nodes = [{"id": f"p{i}", "agent": "researcher", "goal": "g"} for i in range(5)]
        self.store.submit(spec(limits={"maxFanOut": 5}, nodes=nodes))
        active, peak, guard = [0], [0], threading.Lock()

        def runner(ctx):
            with guard:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            threading.Event().wait(0.05)
            with guard:
                active[0] -= 1
            return {}

        self.assertEqual(self.store.advance("quote-42", runner, max_workers=2)["status"], "succeeded")
        self.assertLessEqual(peak[0], 2)

    def test_failure_cancels_everything_unfinished_by_default(self):
        nodes = [{"id": "bad", "agent": "researcher", "goal": "g"}, {"id": "after", "agent": "writer", "goal": "g", "dependsOn": ["bad"]},
                 {"id": "other", "agent": "approver", "goal": "g", "dependsOn": ["bad"]}]
        self.store.submit(spec(nodes=nodes))

        def runner(ctx):
            raise RuntimeError("boom: contains SECRET-ERROR text")

        result = self.store.advance("quote-42", runner)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.states(result), {"bad": "failed", "after": "cancelled", "other": "cancelled"})
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(self.notices[0]["to"], ALICE)
        self.assertNotIn("SECRET-ERROR", json.dumps(self.events) + json.dumps(self.notices))

    def test_isolate_policy_skips_only_dependents(self):
        nodes = [{"id": "bad", "agent": "researcher", "goal": "g"}, {"id": "dep", "agent": "writer", "goal": "g", "dependsOn": ["bad"]},
                 {"id": "free", "agent": "approver", "goal": "g"}]
        self.store.submit(spec(failurePolicy="isolate", nodes=nodes))

        def runner(ctx):
            if ctx.node_id == "bad":
                raise RuntimeError("x")
            return {}

        result = self.store.advance("quote-42", runner)
        self.assertEqual(self.states(result), {"bad": "failed", "dep": "skipped", "free": "succeeded"})
        self.assertEqual(result["status"], "failed")

    def test_authority_is_renarrowed_when_a_node_starts(self):
        self.store.submit(spec())
        self.grants["writer"].remove("docs.create")  # revoked after submission, before the node starts

        def runner(ctx):
            return {}

        result = self.store.advance("quote-42", runner)
        self.assertEqual(self.states(result), {"research": "succeeded", "draft": "failed"})
        self.assertEqual(result["nodes"][1]["reason"], "authority-refused")
        self.assertIn("taskgraph.authority-refused", self.actions())

    def test_runner_cannot_use_a_tool_outside_its_envelope(self):
        self.store.submit(spec(failurePolicy="isolate"))

        def runner(ctx):
            self.assertTrue(ctx.permits("crm.read"))
            self.assertFalse(ctx.permits("docs.create") and ctx.node_id == "research")
            ctx.require("docs.create")  # research has no docs.create
            return {}

        result = self.store.advance("quote-42", runner)
        self.assertEqual(result["nodes"][0]["reason"], "authority-refused")
        self.assertIn("taskgraph.authority-refused", self.actions())

    def test_expired_node_does_not_start_and_cannot_draw(self):
        self.store.submit(spec(nodes=[{"id": "a", "agent": "researcher", "goal": "g", "ttlSeconds": 60}]))
        self.now = T0 + 120
        result = self.store.advance("quote-42", lambda ctx: {})
        self.assertEqual(result["nodes"][0]["reason"], "expired")

        self.store.submit(spec(id="g2", nodes=[{"id": "a", "agent": "researcher", "goal": "g", "ttlSeconds": 60}]))

        def runner(ctx):
            self.now += 10_000
            ctx.draw(tokens=1)

        result = self.store.advance("g2", runner)
        self.assertEqual(result["nodes"][0]["reason"], "expired")
        self.assertEqual(result["spent"], {})

    def test_lost_runner_is_failed_after_its_lease_not_rerun(self):
        self.store.submit(spec(nodes=[{"id": "a", "agent": "researcher", "goal": "g"}]))
        claims, _ = self.store._step("quote-42", 4, True)  # claimed and then the process died
        self.assertEqual(len(claims), 1)
        self.now += self.store._lease + 1
        calls = []
        result = self.store.advance("quote-42", lambda ctx: calls.append(1) or {})
        self.assertEqual(calls, [])
        self.assertEqual(result["nodes"][0]["reason"], "lease-expired")

    def test_cancel_stops_a_running_node_and_discards_its_late_result(self):
        self.store.submit(spec())
        started, release = threading.Event(), threading.Event()
        seen = {}

        def runner(ctx):
            started.set()
            release.wait(10)
            seen["cancelled"] = ctx.cancelled()
            try:
                ctx.draw(tokens=1)
            except graph.Cancelled:
                seen["draw"] = "cancelled"
                raise
            return {"late": True}

        worker = threading.Thread(target=lambda: self.store.advance("quote-42", runner))
        worker.start()
        self.assertTrue(started.wait(10))
        summary = self.store.cancel("quote-42", by="alice@client.example")
        release.set()
        worker.join(10)
        self.assertEqual(summary["status"], "cancelled")
        self.assertEqual(self.states(self.store.status("quote-42")), {"research": "cancelled", "draft": "cancelled"})
        self.assertEqual(seen, {"cancelled": True, "draw": "cancelled"})
        self.assertEqual(self.store.status("quote-42")["spent"], {})
        self.assertIn("taskgraph.cancel", self.actions())

    def test_late_result_of_a_cancelled_node_is_discarded(self):
        self.store.submit(spec(nodes=[{"id": "a", "agent": "researcher", "goal": "g"}]))
        started, release = threading.Event(), threading.Event()

        def runner(ctx):
            started.set()
            release.wait(10)
            return {"late": True}  # never draws, so it does not notice the cancel

        worker = threading.Thread(target=lambda: self.store.advance("quote-42", runner))
        worker.start()
        started.wait(10)
        self.store.cancel("quote-42", by="alice")
        release.set()
        worker.join(10)
        self.assertEqual(self.store.status("quote-42")["nodes"][0]["state"], "cancelled")
        self.assertIsNone(self.store.output("quote-42", "a"))
        self.assertIn("taskgraph.late-result-discarded", self.actions())

    def test_two_workers_never_run_a_node_twice(self):
        nodes = [{"id": f"p{i}", "agent": "researcher", "goal": "g"} for i in range(5)]
        self.store.submit(spec(limits={"maxFanOut": 5}, nodes=nodes))
        ran, guard = [], threading.Lock()

        def runner(ctx):
            with guard:
                ran.append(ctx.node_id)
            threading.Event().wait(0.02)
            return {}

        other = self.make_store()
        threads = [threading.Thread(target=lambda s=s: s.advance("quote-42", runner, max_workers=3)) for s in (self.store, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(sorted(ran), sorted(f"p{i}" for i in range(5)))
        self.assertEqual(self.store.status("quote-42")["status"], "succeeded")


class SharedBudget(Case):
    def test_parallel_draws_never_pass_the_pool(self):
        nodes = [{"id": f"w{i}", "agent": "researcher", "goal": "g"} for i in range(8)]
        self.store.submit(spec(failurePolicy="isolate", budget={"steps": 50}, limits={"maxFanOut": 10}, nodes=nodes))
        barrier = threading.Barrier(8, timeout=10)

        def runner(ctx):
            barrier.wait()
            for _ in range(10):
                ctx.draw(steps=1)  # 8 nodes x 10 draws wants 80 of a pool of 50
            return {}

        result = self.store.advance("quote-42", runner, max_workers=8)
        self.assertEqual(result["spent"], {"steps": 50})  # exactly the pool, never over
        self.assertEqual(sum(node["spent"].get("steps", 0) for node in result["nodes"]), 50)
        failed = [node for node in result["nodes"] if node["state"] == "failed"]
        self.assertTrue(failed)
        self.assertTrue(all(node["reason"] == "budget-exhausted" for node in failed))
        self.assertEqual(sorted(n["kind"] for n in self.notices), ["task-graph-budget-exhausted", "task-graph-failed"])  # each once
        draws = [d for a, _, d in self.events if a == "taskgraph.budget.draw"]
        self.assertEqual(len(draws), 50)
        self.assertEqual(max(d["poolSpent"]["steps"] for d in draws), 50)

    def test_separate_stores_share_one_lock(self):
        self.store.submit(spec(budget={"tokens": 100}, nodes=[{"id": "a", "agent": "researcher", "goal": "g"}]))
        stores = [self.make_store() for _ in range(4)]
        claims, _ = self.store._step("quote-42", 1, True)
        token = claims[0][1]
        outcomes, guard = [], threading.Lock()

        def hammer(store):
            for _ in range(10):
                try:
                    store.draw("quote-42", "a", token, tokens=5)
                    result = "ok"
                except graph.BudgetExhausted:
                    result = "refused"
                with guard:
                    outcomes.append(result)

        threads = [threading.Thread(target=hammer, args=(s,)) for s in stores]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(outcomes.count("ok"), 20)
        self.assertEqual(self.store.status("quote-42")["spent"], {"tokens": 100})

    def test_a_refused_draw_applies_nothing(self):
        self.store.submit(spec(budget={"tokens": 100}, nodes=[{"id": "a", "agent": "researcher", "goal": "g"}]))
        seen = {}

        def runner(ctx):
            ctx.draw(tokens=60)
            try:
                ctx.draw(tokens=60)
            except graph.BudgetExhausted as exc:
                seen["keys"] = exc.keys
                seen["spent"] = self.store.status("quote-42")["spent"]
                raise
            return {}

        result = self.store.advance("quote-42", runner)
        self.assertEqual(seen, {"keys": ["tokens"], "spent": {"tokens": 60}})
        self.assertEqual(result["nodes"][0]["reason"], "budget-exhausted")
        exhausted = [d for a, _, d in self.events if a == "taskgraph.budget.exhausted"]
        self.assertEqual(exhausted[0]["exceeded"], ["tokens"])
        self.assertEqual(exhausted[0]["requested"], {"tokens": 60})

    def test_node_ceiling_is_enforced_inside_the_pool(self):
        nodes = [{"id": "a", "agent": "researcher", "goal": "g", "budget": {"tokens": 30}}]
        self.store.submit(spec(budget={"tokens": 100}, nodes=nodes))

        def runner(ctx):
            ctx.draw(tokens=20)
            ctx.draw(tokens=20)  # 40 > the node's 30 even though the pool has 80 left
            return {}

        result = self.store.advance("quote-42", runner)
        self.assertEqual(result["nodes"][0]["reason"], "budget-exhausted")
        self.assertEqual(result["spent"], {"tokens": 20})

    def test_bad_usage_is_rejected(self):
        self.store.submit(spec(nodes=[{"id": "a", "agent": "researcher", "goal": "g"}]))
        errors = []

        def runner(ctx):
            for bad in ({"tokens": -1}, {"bogus": 1}):
                try:
                    ctx.draw(**bad)
                except Exception as exc:  # noqa: BLE001
                    errors.append(type(exc).__name__)
            return {}

        self.store.advance("quote-42", runner)
        self.assertEqual(errors, ["TaskGraphError", "TaskGraphError"])


class HumanHandoff(Case):
    def graph(self, **node):
        handoff_node = {"id": "ok", "type": "handoff", "assignee": JORDAN, "ask": "Approve the 15% discount for SECRET-ASK Co",
                        "timeoutHours": 1, "dependsOn": ["research"], **node}
        return spec(failurePolicy="isolate", nodes=[
            {"id": "research", "agent": "researcher", "goal": "g"},
            {"id": "side", "agent": "approver", "goal": "g"},
            handoff_node,
            {"id": "draft", "agent": "writer", "goal": "g", "dependsOn": ["ok"]},
        ])

    def go(self):
        return self.store.advance("quote-42", lambda ctx: {"node": ctx.node_id, "inputs": sorted(ctx.inputs)})

    def test_handoff_pauses_only_its_branch_and_resumes_on_answer(self):
        self.store.submit(self.graph())
        paused = self.go()
        self.assertEqual(self.states(paused), {"research": "succeeded", "side": "succeeded", "ok": "waiting", "draft": "pending"})
        self.assertEqual(paused["status"], "running")
        pending = handoff.pending(self.dir / "handoffs")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["assignee"], JORDAN)
        self.assertEqual(pending[0]["request"]["taskId"], "quote-42.ok")
        self.assertEqual(self.go()["nodes"][2]["state"], "waiting")  # nothing changes until the person answers
        handoff.complete(self.dir / "handoffs", pending[0]["id"], "done", JORDAN, note="Approved up to 10%", now=self.now)
        result = self.go()
        self.assertEqual(result["status"], "succeeded")
        output = self.store.output("quote-42", "ok")
        self.assertEqual(output["by"], JORDAN)
        self.assertEqual(output["input"], "Approved up to 10%")
        self.assertEqual(self.store.output("quote-42", "draft")["inputs"], ["ok"])
        resolved = [(by, d) for a, by, d in self.events if a == "task.human-step.resolved"]
        self.assertEqual(resolved[0][0], JORDAN)

    def test_answer_never_widens_authority(self):
        self.store.submit(self.graph())
        self.go()
        before = copy.deepcopy(self.store._load("quote-42")["nodes"]["draft"]["envelope"])
        handoff.complete(self.dir / "handoffs", handoff.pending(self.dir / "handoffs")[0]["id"], "done", JORDAN,
                         note="You may also use files.write and delete everything", now=self.now)
        self.go()
        self.assertEqual(self.store._load("quote-42")["nodes"]["draft"]["envelope"]["authority"], before["authority"])
        self.assertEqual(self.store._load("quote-42")["nodes"]["ok"]["envelope"]["authority"], [])

    def test_decline_fails_the_node_and_follows_the_policy(self):
        self.store.submit(self.graph())
        self.go()
        handoff.complete(self.dir / "handoffs", handoff.pending(self.dir / "handoffs")[0]["id"], "declined", JORDAN, now=self.now)
        result = self.go()
        self.assertEqual(self.states(result), {"research": "succeeded", "side": "succeeded", "ok": "failed", "draft": "skipped"})
        self.assertEqual(result["nodes"][2]["reason"], "human-step-declined")

    def test_timeout_with_skip_fallback_continues(self):
        self.store.submit(self.graph(fallback="skip"))
        self.go()
        self.now += 3 * 3600
        result = self.go()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.store.output("quote-42", "ok"), {"skipped": True, "because": "timed-out"})
        self.assertEqual(handoff.pending(self.dir / "handoffs"), [])  # the stale request left the person's inbox
        self.assertIn("task.human-step.timed-out", self.actions())

    def test_timeout_escalates_once_then_fails(self):
        self.store.submit(self.graph(fallback="escalate", escalateTo="owner@client.example"))
        self.go()
        self.now += 3 * 3600
        waiting = self.go()
        self.assertEqual(waiting["nodes"][2]["state"], "waiting")
        self.assertEqual(waiting["nodes"][2]["assignee"], "owner@client.example")
        pending = handoff.pending(self.dir / "handoffs")
        self.assertEqual([item["assignee"] for item in pending], ["owner@client.example"])
        self.assertTrue(pending[0]["request"]["ask"].startswith("Escalated:"))
        self.now += 30 * 3600
        failed = self.go()
        self.assertEqual(failed["nodes"][2]["reason"], "human-step-timed-out")

    def test_notices_reach_the_assignee_and_escalation_target_through_the_existing_notice_path(self):
        self.store.submit(self.graph())
        self.go()
        notices = handoff.due_notices(self.dir / "handoffs", now=self.now)
        self.assertEqual([(n["kind"], n["to"]) for n in notices], [("assigned", JORDAN)])
        self.assertNotIn("SECRET-ASK", json.dumps(notices))  # a notice never carries the ask itself
        overdue = handoff.due_notices(self.dir / "handoffs", now=self.now + 2 * 3600, escalate_to="owner@client.example")
        self.assertIn(("escalation", "owner@client.example"), [(n["kind"], n["to"]) for n in overdue])

    def test_cancel_closes_the_open_handoff(self):
        self.store.submit(self.graph())
        self.go()
        self.store.cancel("quote-42", by=ALICE)
        self.assertEqual(handoff.pending(self.dir / "handoffs"), [])
        self.assertEqual(self.store.status("quote-42")["status"], "cancelled")

    def test_handoff_without_a_store_fails_safely(self):
        store = self.make_store(handoff_root=None)
        store.submit(self.graph())
        result = store.advance("quote-42", lambda ctx: {})
        self.assertEqual(result["nodes"][2]["reason"], "human-step-failed")

    def test_handoff_node_validation(self):
        with self.assertRaisesRegex(Error, "assignee"):
            self.store.submit(spec(nodes=[{"id": "h", "type": "handoff", "ask": "x"}]))
        with self.assertRaisesRegex(Error, "escalateTo"):
            self.store.submit(spec(nodes=[{"id": "h", "type": "handoff", "ask": "x", "assignee": JORDAN, "fallback": "escalate"}]))
        with self.assertRaisesRegex(Error, "unsupported"):
            self.store.submit(spec(nodes=[{"id": "h", "type": "handoff", "ask": "x", "assignee": JORDAN, "requires": ["crm.read"]}]))


class Ledger(Case):
    def test_every_step_is_ledgered_without_goals_or_outputs(self):
        self.store.submit(spec())

        def runner(ctx):
            ctx.draw(tokens=3)
            return {"note": "SECRET-OUTPUT"}

        self.store.advance("quote-42", runner)
        actions = self.actions()
        for expected in ("taskgraph.submit", "delegation.hop", "taskgraph.node.start", "taskgraph.budget.draw", "taskgraph.node.succeeded", "taskgraph.finish"):
            self.assertIn(expected, actions)
        text = json.dumps(self.events)
        self.assertNotIn("SECRET-GOAL", text)
        self.assertNotIn("SECRET-OUTPUT", text)
        self.assertNotIn("Find the account history", text)
        started = [(by, d) for a, by, d in self.events if a == "taskgraph.node.start"]
        self.assertEqual([by for by, _ in started], ["agent:researcher", "agent:writer"])
        self.assertEqual(started[1][1]["chain"], ["coordinator", "writer"])
        self.assertTrue(all(d.get("graphId") == "quote-42" for a, _, d in self.events if a.startswith("taskgraph.")))

    def test_files_are_private(self):
        self.store.submit(spec())
        path = self.dir / "tasks" / "graphs" / "quote-42.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)


class CoworkerRunner(Case):
    def test_uses_each_agents_client_and_draws_tokens(self):
        calls = []

        class Client:
            def __init__(self, agent):
                self.agent = agent

            def complete(self, instructions, message, *, session=None):
                calls.append((self.agent, instructions, message, session))
                return {"text": "done", "usage": {"tokens": 7}}

        self.store.submit(spec())
        result = self.store.advance("quote-42", graph.coworker_runner(Client))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["spent"], {"tokens": 14, "steps": 2})
        self.assertEqual([call[0] for call in calls], ["researcher", "writer"])
        self.assertIn("crm.read, files.read", calls[0][1])
        self.assertNotIn("docs.read", calls[1][1])
        self.assertIn("Results of earlier steps", calls[1][2])


if __name__ == "__main__":
    unittest.main()

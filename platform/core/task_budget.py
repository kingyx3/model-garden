"""One shared budget across a delegated task tree: reserve on delegation, charge to the leaf, halt and tell the owner (#96).

``delegation.delegate`` already makes a child's budget no larger than what its parent has left, but on its own two siblings could
each be given everything the parent had left, so a task with a budget of 100 could spend 200. ``TaskBudgetTree`` is the
credential-free bookkeeping a multi-Agent runtime needs to make the budget real across hops. It is pure (state in memory, with
``snapshot``/``restore`` for durability), calls ``delegation`` for every rule it already enforces, and is wired to a team's
governed calls by ``platform/runtime/team_runtime.py`` (#95).

- **Delegating reserves.** The child's budget is set aside from its parent's own: the parent's *available* budget is
  ``budget - spent - reserved``, and a second child can only be given what is still available, so the children of a task can
  never add up to more than it had.
- **Usage is charged where it happens.** A charge lands on the task that did the work and stays within that task's reservation.
  When a child finishes (or halts), its reservation is released to its parent and what it actually used is rolled into the
  parent's accounting, so ``total_spent`` is the real cost of the whole tree and nothing is counted twice.
- **Exhaustion halts the task and everything under it.** Charging past a task's available budget records the overspend, ledgers
  ``task.budget-exhausted`` (task, chain, exceeded keys, budget and spend; never the goal), halts that task and all its
  descendants, and tells the originating principal once per task. A halted task cannot be charged further or delegate. A
  sibling branch keeps running: what a coordinator does about a failed branch is its own policy (#98).
- **Visible per branch.** ``timeline`` lists every task with its agent, parent, state, budget, spend, reserved and remaining, so
  cost and halts show up per branch.
"""
from __future__ import annotations

import copy
import importlib.util
import pathlib
import sys
from collections.abc import Callable, Mapping
from typing import Any

Audit = Callable[[str, str, dict[str, Any]], Any]
Notify = Callable[[dict[str, Any]], Any]


class TaskBudgetError(ValueError):
    """An invalid call: an unknown task, a task that is halted or finished, or a usage record that is malformed."""


class BudgetExhausted(RuntimeError):
    """Raised after a charge passes a task's available budget; the task and its descendants are halted."""

    def __init__(self, task_id: str, keys: list[str]) -> None:
        super().__init__(f"task {task_id} exhausted its budget: {', '.join(keys)}")
        self.task_id, self.keys = task_id, keys


def _delegation():
    name = "model_garden_task_budget_delegation"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name("delegation.py"))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class TaskBudgetTree:
    def __init__(self, root: Mapping[str, Any], *, audit: Audit | None = None, notify: Notify | None = None) -> None:
        if root.get("parentTaskId") is not None or "taskId" not in root:
            raise TaskBudgetError("the tree starts from a root envelope")
        self._audit, self._notify = audit, notify
        self._nodes: dict[str, dict[str, Any]] = {}
        self._add(dict(root))

    # -- plumbing ------------------------------------------------------------------------

    def _add(self, envelope: dict[str, Any]) -> dict[str, Any]:
        node = {"envelope": envelope, "children": [], "open": [], "rolled": {}, "state": "running", "told": False}
        self._nodes[envelope["taskId"]] = node
        return node

    def _node(self, task_id: str) -> dict[str, Any]:
        node = self._nodes.get(task_id)
        if node is None:
            raise TaskBudgetError("no such task in this tree")
        return node

    def _live(self, task_id: str) -> dict[str, Any]:
        node = self._node(task_id)
        if node["state"] != "running":
            raise TaskBudgetError(f"task {task_id} is {node['state']}")
        return node

    def _reserved(self, node: Mapping[str, Any]) -> dict[str, int]:
        """The budgets set aside for the children that have not finished or halted."""
        reserved: dict[str, int] = {}
        for child in node["open"]:
            for key, amount in self._nodes[child]["envelope"]["budget"].items():
                reserved[key] = reserved.get(key, 0) + int(amount)
        return reserved

    def _used(self, node: Mapping[str, Any]) -> dict[str, int]:
        """What this task has used itself plus what finished or halted children used (open children are reserved, not used)."""
        used = {key: int(value) for key, value in node["envelope"].get("spent", {}).items()}
        for key, value in node["rolled"].items():
            used[key] = used.get(key, 0) + int(value)
        return used

    def _total(self, node: Mapping[str, Any]) -> dict[str, int]:
        total = self._used(node)
        for child in node["open"]:
            for key, value in self._total(self._nodes[child]).items():
                total[key] = total.get(key, 0) + value
        return total

    def _available(self, node: Mapping[str, Any]) -> dict[str, int]:
        used, reserved = self._used(node), self._reserved(node)
        return {key: int(amount) - used.get(key, 0) - reserved.get(key, 0) for key, amount in node["envelope"]["budget"].items()}

    def _record(self, action: str, by: str, details: dict[str, Any]) -> None:
        if self._audit is not None:
            self._audit(action, by, details)

    def _descendants(self, task_id: str) -> list[str]:
        found: list[str] = []
        for child in self._nodes[task_id]["children"]:
            found.append(child)
            found.extend(self._descendants(child))
        return found

    # -- the tree ------------------------------------------------------------------------

    def delegate(self, parent_id: str, **arguments: Any) -> dict[str, Any]:
        """Delegate under ``parent_id`` (the keyword arguments of ``delegation.delegate``); the child's budget is reserved."""
        parent = self._live(parent_id)
        delegation = _delegation()
        envelope = parent["envelope"]
        used, reserved = self._used(parent), self._reserved(parent)
        view = {**envelope, "spent": {key: used.get(key, 0) + reserved.get(key, 0) for key in envelope["budget"]}}
        try:
            child = delegation.delegate(view, children_so_far=len(parent["children"]), **arguments)
        except PermissionError as exc:
            raise TaskBudgetError(str(exc)) from exc
        if child["taskId"] in self._nodes:
            raise TaskBudgetError("that task id is already in the tree")
        parent["children"].append(child["taskId"])
        parent["open"].append(child["taskId"])
        self._add(child)
        self._record("delegation.hop", f"agent:{envelope['agent']}", delegation.hop_details(view, child))
        return copy.deepcopy(child)

    def charge(self, task_id: str, **used: int) -> dict[str, int]:
        """Record usage on ``task_id``; returns what it has available. Raises ``BudgetExhausted`` once a bound is passed."""
        node = self._live(task_id)
        delegation = _delegation()
        try:
            node["envelope"] = delegation.charge(node["envelope"], **used)
        except PermissionError as exc:
            raise TaskBudgetError(str(exc)) from exc
        available = self._available(node)
        over = sorted(key for key, amount in available.items() if amount < 0)
        if over:
            self._halt(task_id, over)
            raise BudgetExhausted(task_id, over)
        return available

    def finish(self, task_id: str) -> dict[str, int]:
        """Complete a task: its reservation is released to its parent and what it used is rolled into the parent's accounting."""
        node = self._live(task_id)
        if any(self._nodes[child]["state"] == "running" for child in node["open"]):
            raise TaskBudgetError("a task cannot finish while a delegated task is still running")
        node["state"] = "finished"
        self._roll_up(task_id)
        return self._total(node)

    def _roll_up(self, task_id: str) -> str | None:
        """Release a closed task's reservation to its parent; returns the parent's id when it was still open there."""
        node = self._nodes[task_id]
        parent_id = node["envelope"].get("parentTaskId")
        if parent_id is None or task_id not in self._nodes[parent_id]["open"]:
            return None
        parent = self._nodes[parent_id]
        parent["open"].remove(task_id)
        for key, value in self._total(node).items():
            parent["rolled"][key] = int(parent["rolled"].get(key, 0)) + value
        return parent_id

    def _halt(self, task_id: str, keys: list[str]) -> None:
        node = self._nodes[task_id]
        envelope = node["envelope"]
        for descendant in [task_id, *self._descendants(task_id)]:
            if self._nodes[descendant]["state"] == "running":
                self._nodes[descendant]["state"] = "halted"
        self._record("task.budget-exhausted", f"agent:{envelope['agent']}", {
            "taskId": task_id, "chain": list(envelope["delegationChain"]), "exceeded": keys, "budget": dict(envelope["budget"]),
            "spent": self._used(node), "reserved": self._reserved(node),
        })
        if self._notify is not None and not node["told"]:
            node["told"] = True
            try:
                self._notify({
                    "key": f"budget-exhausted:{task_id}", "kind": "task-budget-exhausted", "to": envelope["originatingPrincipal"],
                    "taskId": task_id, "exceeded": keys,
                    "text": f"A task ({task_id}) stopped because it used up its budget ({', '.join(keys)}). Nothing further was done for it.",
                })
            except Exception:  # noqa: BLE001 - telling the owner failing must not undo the halt
                pass
        # Its real use counts against its parent now; if that leaves the parent over, the parent has overspent too.
        parent_id = self._roll_up(task_id)
        if parent_id is not None and self._nodes[parent_id]["state"] == "running":
            over = sorted(key for key, amount in self._available(self._nodes[parent_id]).items() if amount < 0)
            if over:
                self._halt(parent_id, over)

    # -- views ---------------------------------------------------------------------------

    def state(self, task_id: str) -> str:
        return self._node(task_id)["state"]

    def timeline(self) -> list[dict[str, Any]]:
        rows = []
        for task_id, node in self._nodes.items():
            envelope = node["envelope"]
            rows.append({
                "taskId": task_id, "parentTaskId": envelope.get("parentTaskId"), "agent": envelope["agent"], "depth": envelope["depth"],
                "state": node["state"], "budget": dict(envelope["budget"]), "spent": self._used(node),
                "reserved": self._reserved(node), "available": self._available(node),
            })
        return rows

    def total_spent(self) -> dict[str, int]:
        """What the whole tree has really used so far, every task counted once."""
        root = next(node for node in self._nodes.values() if node["envelope"].get("parentTaskId") is None)
        return self._total(root)

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy({"nodes": self._nodes})

    @classmethod
    def restore(cls, snapshot: Mapping[str, Any], *, audit: Audit | None = None, notify: Notify | None = None) -> "TaskBudgetTree":
        nodes = copy.deepcopy(dict(snapshot.get("nodes", {})))
        roots = [node for node in nodes.values() if node["envelope"].get("parentTaskId") is None]
        if len(roots) != 1:
            raise TaskBudgetError("a snapshot has exactly one root task")
        tree = cls(roots[0]["envelope"], audit=audit, notify=notify)
        tree._nodes = nodes
        return tree

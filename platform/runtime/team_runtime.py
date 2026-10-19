"""A team of Agents in one environment: one governed runtime per Agent, isolated state, delegation that cannot escalate (#95, #96, #129).

The governed sidecar serves one compiled Agent. A team keeps that property and adds several of them side by side:

- **The Agent is the runtime instance, never something the model says.** Each Agent gets its own ``GovernedToolRuntime`` built from
  its own compiled desired state, so a call can only be authorised against the Tools that Agent holds. The model of one Agent
  reaches only that Agent's runtime (``launch_plan`` gives each Agent its own process and port); no tool argument names another.
- **Isolated state, one shared ledger.** Approvals, hand-offs, memory and artifacts live under ``agents/<name>/``, so one Agent's
  pending approval, hand-off or file is never visible to another. All Agents append to one hash-chained ledger whose events name
  the Agent, so the per-Agent usage counts and the operator's ``audit`` filters keep working. Policy and knowledge deployments are
  ledgered per Agent.
- **Own provider access, fail closed.** More than one Agent only starts when ``platform/core/team.py`` finds that every Agent
  needing an external connector family holds a usable grant of its own (never a shared login); otherwise ``TeamError`` names
  each blocker. One Agent starts exactly as before.
- **Delegation under the envelope rules.** ``start_task`` and ``delegate`` apply ``platform/core/delegation.py`` (authority is the
  delegate's own Tools, the delegator's authority for the task and the originator's permissions; bounded depth and fan-out; loops
  refused; budget and deadline only tighten). Every hop, and every refused hop, is ledgered. A governed call made under
  ``acting(agent, envelope)`` carries ``taskId``, ``originatingPrincipal`` and ``delegationChain`` and is denied and ledgered when
  the Tool is outside the task's authority, the deadline has passed, the budget is spent, or the envelope belongs to another Agent.
- **One budget per task tree, charged as calls happen.** A task tree is a ``TaskBudgetTree`` (``platform/core/task_budget.py``),
  saved under ``<base>/tasks/`` after every change so a restart neither resets spending nor fan-out. Delegating reserves the child's
  budget from its parent; every governed call under a task is charged one ``steps`` before it runs (``charge`` adds tokens or cost the
  host measured); past the budget the call is refused, ``task.budget-exhausted`` is ledgered, the branch and its descendants halt and
  the originating principal is told once through the host's ``notify``.
- **A person as a task step.** ``open_human_step`` and ``human_step`` put a step to a named person on the Agent's own Tier 4
  hand-offs (``platform/core/human_step.py``): the pending hand-off is the durable pause, a timeout resolves to the configured
  fallback and never resumes silently, and the opening, the person's answer (under their name) and a timeout are each ledgered once
  against the task, so they appear in the task timeline.

- **Parallel branches under one budget.** ``fan_out`` delegates several independent sub-tasks of one task at once (each reserves its
  share of the parent's budget, so branches together never spend more than the parent had), runs them on a bounded pool, and joins:
  every branch ends ``succeeded``, ``failed``, ``budget-exhausted``, ``refused`` (the hop itself was refused) or ``cancelled``
  (``fail-fast`` stops starting branches after the first failure; ``best-effort`` runs them all). Concurrency is capped per call and
  per environment (``max_parallel``), the fan-out, each branch's outcome and spend, and the join are each ledgered against the
  task, so the task timeline shows the cost and actions of every branch. Error text from a branch is never recorded: its type only.

Nothing here holds a credential or reaches the network. The task state lives in one host process: sidecars launched from
``launch_plan`` do not consult it. Not covered: a model-callable delegation Tool, the runtime-native handoff/A2A decision,
Docker/Hermes deployment of several Agents, and any live evidence.
"""
from __future__ import annotations

import contextlib
import contextvars
import copy
import importlib.util
import json
import os
import pathlib
import re
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
AGENT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
DEFAULT_PORT = 9120
DEFAULT_MAX_PARALLEL = 4
MAX_PARALLEL = 32
FAN_OUT_POLICIES = ("fail-fast", "best-effort")


def _load(name: str, relative: str):
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, ROOT / relative)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


GOVERN = _load("model_garden_team_runtime_govern", "scripts/govern-action.py")
TEAM = _load("model_garden_team_runtime_team", "platform/core/team.py")
DELEGATION = _load("model_garden_team_runtime_delegation", "platform/core/delegation.py")
CAPABILITIES = _load("model_garden_team_runtime_capabilities", "platform/core/capabilities.py")
TASK_BUDGET = _load("model_garden_team_runtime_task_budget", "platform/core/task_budget.py")
HUMAN_STEP = _load("model_garden_team_runtime_human_step", "platform/core/human_step.py")
TeamError = TEAM.TeamError

# The task a call is running under, set by ``acting`` for the duration of one call (thread- and task-local).
_TASK: contextvars.ContextVar[tuple[str, Mapping[str, Any]] | None] = contextvars.ContextVar("model_garden_team_task", default=None)


def _governed():
    return _load("model_garden_team_runtime_governed_mcp", "platform/runtime/governed_mcp.py")


def _write_private(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class TeamRuntime:
    """Several compiled Agents, each behind its own governed runtime, sharing one ledger and nothing else."""

    def __init__(
        self,
        states: Iterable[Mapping[str, Any]],
        base: pathlib.Path,
        *,
        environment_binding: Mapping[str, Any] | None = None,
        registry: Any | None = None,
        environ: Mapping[str, str] | None = None,
        now: Callable[[], float] = time.time,
        max_depth: int = DELEGATION.DEFAULT_MAX_DEPTH,
        max_fan_out: int = DELEGATION.DEFAULT_MAX_FAN_OUT,
        runtime_kwargs: Mapping[str, Any] | None = None,
        notify: Callable[[dict[str, Any]], Any] | None = None,
        max_parallel: int = DEFAULT_MAX_PARALLEL,
    ) -> None:
        if not isinstance(max_parallel, int) or isinstance(max_parallel, bool) or not 1 <= max_parallel <= MAX_PARALLEL:
            raise TeamError(f"max_parallel must be 1 to {MAX_PARALLEL}")
        copies = [copy.deepcopy(dict(state)) for state in states]
        # Raises TeamError for no Agents or two Agents with one name (the name identifies approvals, attribution and ledger records).
        report = TEAM.plan(copies, environment_binding=environment_binding, registry=registry, now=int(now()))
        for state in copies:
            name = TEAM.agent_name(state)
            if not AGENT_NAME.fullmatch(name):
                raise TeamError(f"Agent name {name!r} must be a lowercase DNS-like name (it becomes a directory and a process identity)")
            attributed = (state.get("source") or {}).get("name")
            if attributed != name:  # the governed boundary attributes every request and ledger event to source.name
                raise TeamError(f"Agent {name!r} would be recorded in the ledger as {attributed!r}: source.name and the Agent's name must be the same")
        if report["mode"] == "multi-agent" and not report["ready"]:
            raise TeamError("the team is not ready to start: " + "; ".join(TEAM.blocked_reasons(report)))
        self.report = report
        self.base = pathlib.Path(base)
        self.audit_path = self.base / "audit.jsonl"
        self.now = now
        self.max_depth, self.max_fan_out = max_depth, max_fan_out
        self._binding = environment_binding
        self._environ = dict(os.environ if environ is None else environ)
        self._runtime_kwargs = dict(runtime_kwargs or {})
        self._states = {TEAM.agent_name(state): state for state in copies}
        self._runtimes: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._notify = notify
        self.max_parallel = max_parallel
        self._parallel = threading.BoundedSemaphore(max_parallel)  # the environment-wide limit, shared by every fan-out
        self._task_lock = threading.RLock()
        self._trees: dict[str, Any] = {}
        self._root_of: dict[str, str] = {}
        for name, state in self._states.items():
            self._prepare(name, state)
        self._restore_tasks()

    # -- layout ----------------------------------------------------------------------------

    def agents(self) -> list[str]:
        return sorted(self._states)

    def agent_dir(self, agent: str) -> pathlib.Path:
        self._state(agent)
        return self.base / "agents" / agent

    def _state(self, agent: str) -> Mapping[str, Any]:
        state = self._states.get(agent) if isinstance(agent, str) else None
        if state is None:
            raise TeamError(f"{agent!r} is not an Agent in this environment")
        return state

    def _prepare(self, agent: str, state: Mapping[str, Any]) -> None:
        directory = self.base / "agents" / agent
        _write_private(directory / "desired-state.json", state)
        if self._binding is not None:
            _write_private(directory / "capabilities.json", CAPABILITIES.build_manifest(state, self._binding))

    def _agent_environ(self, agent: str) -> dict[str, str]:
        environ = dict(self._environ)
        manifest = self.base / "agents" / agent / "capabilities.json"
        if manifest.is_file():
            environ["MODEL_GARDEN_CAPABILITY_MANIFEST"] = str(manifest)
        else:  # never inherit another Agent's manifest from the environment
            environ.pop("MODEL_GARDEN_CAPABILITY_MANIFEST", None)
        return environ

    def runtime(self, agent: str):
        """This Agent's own governed runtime (built once). Its Tools, approvals, hand-offs, memory and files are this Agent's alone."""
        with self._lock:
            runtime = self._runtimes.get(agent)
            if runtime is None:
                directory = self.agent_dir(agent)
                runtime = _governed().GovernedToolRuntime(
                    directory / "desired-state.json", self.audit_path, directory / "approvals",
                    environ=self._agent_environ(agent), task_resolver=lambda name=agent: self._task_for(name), **self._runtime_kwargs,
                )
                self._runtimes[agent] = runtime
            return runtime

    def launch_plan(self, *, base_port: int = DEFAULT_PORT, host: str = "0.0.0.0") -> list[dict[str, Any]]:
        """How to run each Agent as its own governed sidecar process: its own state, approval root and port, one shared ledger."""
        if not isinstance(base_port, int) or isinstance(base_port, bool) or not 1 <= base_port <= 65535 - len(self._states):
            raise TeamError("base_port must leave one port per Agent")
        entries = []
        for offset, agent in enumerate(self.agents()):
            directory = self.agent_dir(agent)
            argv = [
                "python3", "platform/runtime/governed_mcp.py", "--desired-state", str(directory / "desired-state.json"),
                "--audit-path", str(self.audit_path), "--approval-root", str(directory / "approvals"),
                "--host", host, "--port", str(base_port + offset),
            ]
            env = {}
            if (directory / "capabilities.json").is_file():
                env["MODEL_GARDEN_CAPABILITY_MANIFEST"] = str(directory / "capabilities.json")
            entries.append({"agent": agent, "port": base_port + offset, "argv": argv, "env": env})
        return entries

    def ledger_deployments(self, changed_by: str) -> dict[str, dict[str, Any]]:
        """Ledger each Agent's policy and knowledge changes since it was last applied, naming the Agent and who deployed it."""
        governed = _governed()
        results: dict[str, dict[str, Any]] = {}
        for agent in self.agents():
            state, directory = self._state(agent), self.agent_dir(agent)
            policy = governed.POLICY_DEPLOY.record_deployment(
                state.get("policies"), directory, changed_by=changed_by,
                record=lambda before, after, by, name=agent: GOVERN.record_policy_change(self.audit_path, before, after, changed_by=by, agent=name),
            )

            def knowledge_event(before, after, by, name=agent):
                summary = governed.KNOWLEDGE_DEPLOY.diff(before, after)
                if not governed.KNOWLEDGE_DEPLOY.has_change(summary):
                    return None
                return GOVERN.record_operator_action(self.audit_path, governed.KNOWLEDGE_DEPLOY.ACTION, by, {**summary, "agent": name})

            knowledge = governed.KNOWLEDGE_DEPLOY.record_deployment(
                governed.KNOWLEDGE_DEPLOY.fingerprint(state), directory, changed_by=changed_by, record=knowledge_event,
            )
            results[agent] = {"policy": policy, "knowledge": knowledge}
        return results

    # -- tasks, delegation and budget ---------------------------------------------------------

    def _ledger_action(self, action: str, by: str, details: dict[str, Any]) -> None:
        GOVERN.record_operator_action(self.audit_path, action, by, details)

    def _tasks_dir(self) -> pathlib.Path:
        return self.base / "tasks"

    def _restore_tasks(self) -> None:
        directory = self._tasks_dir()
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("*.json")):
            tree = TASK_BUDGET.TaskBudgetTree.restore(json.loads(path.read_text(encoding="utf-8")), audit=self._ledger_action, notify=self._notify)
            root = path.stem
            self._trees[root] = tree
            for row in tree.timeline():
                self._root_of[row["taskId"]] = root

    def _persist(self, root_id: str) -> None:
        _write_private(self._tasks_dir() / f"{root_id}.json", self._trees[root_id].snapshot())

    def _tree(self, task_id: Any) -> tuple[str, Any]:
        root = self._root_of.get(task_id) if isinstance(task_id, str) else None
        if root is None:
            raise TeamError("that task was not started by this team")
        return root, self._trees[root]

    def start_task(
        self,
        agent: str,
        *,
        task_id: str,
        originating_principal: str,
        originator_permissions: Iterable[str],
        goal: str,
        required_capabilities: Iterable[str] | None = None,
        budget: Mapping[str, int] | None = None,
        deadline: int | None = None,
        constraints: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """The root envelope: what the requester may ask for, narrowed to the Tools ``agent`` itself holds."""
        state = self._state(agent)
        envelope = DELEGATION.root_envelope(
            task_id=task_id, agent=agent, originating_principal=originating_principal, goal=goal,
            agent_grants=TEAM.tool_names(state), originator_permissions=originator_permissions,
            budget=budget, deadline=deadline, constraints=constraints, required_capabilities=required_capabilities,
        )
        with self._task_lock:
            if envelope["taskId"] in self._root_of:
                raise TeamError("that task id is already in use")
            tree = TASK_BUDGET.TaskBudgetTree(envelope, audit=self._ledger_action, notify=self._notify)
            self._trees[envelope["taskId"]] = tree
            self._root_of[envelope["taskId"]] = envelope["taskId"]
            self._persist(envelope["taskId"])
        return envelope

    def delegate(
        self,
        parent: Mapping[str, Any],
        *,
        to_agent: str,
        child_task_id: str,
        goal: str,
        required_capabilities: Iterable[str] | None = None,
        budget: Mapping[str, int] | None = None,
        deadline: int | None = None,
        constraints: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Hand part of ``parent``'s task to another Agent of this team, ledgering the hop (or the refusal).

        The delegate's authority is its own compiled Tools, the delegator's authority for this task and the originator's permissions
        (which the parent's authority already narrows), so a hop can only shrink what may be done. The child's budget is reserved
        from the parent's, so siblings cannot together spend more than the parent had.
        """
        delegator = parent.get("agent")
        self._check_envelope(delegator, parent)
        delegate_state = self._state(to_agent)
        with self._task_lock:
            root, tree = self._tree(parent["taskId"])
            try:
                child = tree.delegate(
                    parent["taskId"], to_agent=to_agent, child_task_id=child_task_id, goal=goal,
                    delegate_grants=TEAM.tool_names(delegate_state), originator_permissions=parent["authority"],
                    required_capabilities=required_capabilities, budget=budget, deadline=deadline, constraints=constraints,
                    max_depth=self.max_depth, max_fan_out=self.max_fan_out, now=int(self.now()),
                )
            except TASK_BUDGET.TaskBudgetError as exc:
                self._ledger_action("delegation.refused", f"agent:{delegator}",
                                    {"fromTask": parent["taskId"], "toAgent": to_agent, "reason": str(exc)[:300]})
                raise DELEGATION.DelegationError(str(exc)) from exc
            self._root_of[child["taskId"]] = root
            self._persist(root)
        return child

    def fan_out(
        self,
        parent: Mapping[str, Any],
        branches: Iterable[Mapping[str, Any]],
        *,
        runner: Callable[[str, dict[str, Any], Any], Any],
        policy: str = "best-effort",
        max_concurrency: int | None = None,
    ) -> dict[str, Any]:
        """Run independent sub-tasks of ``parent`` in parallel under its budget and join them.

        Each branch is a mapping with ``agent``, ``taskId`` and ``goal`` (optionally ``requiredCapabilities``, ``budget``, ``deadline``,
        ``constraints``). ``runner(agent, envelope, runtime)`` does the branch's work on that Agent's governed runtime, under its
        envelope, so every call it makes is attributed, charged and bounded as for any delegated task. The join returns each branch's
        outcome, value (what the runner returned, never ledgered), error type and spend. ``fail-fast`` starts no further branch once one
        has failed or been refused (a branch already running finishes); ``best-effort`` runs every branch it could delegate.
        """
        if policy not in FAN_OUT_POLICIES:
            raise TeamError(f"policy must be one of {', '.join(FAN_OUT_POLICIES)}")
        plan = [dict(branch) for branch in branches]
        if not plan:
            raise TeamError("a fan-out needs at least one branch")
        concurrency = min(self.max_parallel, max_concurrency if max_concurrency is not None else self.max_parallel)
        if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency < 1:
            raise TeamError("max_concurrency must be at least 1")
        self._check_envelope(parent.get("agent"), parent)
        parent_id = parent["taskId"]
        by = f"agent:{parent['agent']}"
        self._ledger_action("task.fan-out", by, {"taskId": parent_id, "branches": len(plan), "policy": policy, "concurrency": concurrency})
        stop = threading.Event()
        results: list[dict[str, Any]] = [{"taskId": item.get("taskId"), "agent": item.get("agent"), "outcome": "cancelled"} for item in plan]
        children: dict[int, dict[str, Any]] = {}
        for index, item in enumerate(plan):
            if stop.is_set():
                break
            try:
                children[index] = self.delegate(
                    parent, to_agent=item.get("agent"), child_task_id=item.get("taskId"), goal=item.get("goal", ""),
                    required_capabilities=item.get("requiredCapabilities"), budget=item.get("budget"), deadline=item.get("deadline"),
                    constraints=item.get("constraints"),
                )
            except (DELEGATION.DelegationError, TeamError, ValueError) as exc:
                results[index].update(outcome="refused", error=type(exc).__name__)
                if policy == "fail-fast":
                    stop.set()

        def run(index: int) -> None:
            child, agent = children[index], children[index]["agent"]
            with self._parallel:  # the environment-wide cap, whatever else is fanning out
                if stop.is_set():
                    return
                try:
                    with self.acting(agent, child) as runtime:
                        results[index]["value"] = runner(agent, child, runtime)
                    results[index]["outcome"] = "succeeded"
                except Exception as exc:  # noqa: BLE001 - a branch failing is a join outcome; only the type is kept
                    halted = self._tree(child["taskId"])[1].state(child["taskId"]) == "halted"
                    results[index].update(outcome="budget-exhausted" if halted else "failed", error=type(exc).__name__)
                    if policy == "fail-fast":
                        stop.set()
                    return
                if self._tree(child["taskId"])[1].state(child["taskId"]) == "halted":  # a refused call is not an exception to the runner
                    results[index]["outcome"] = "budget-exhausted"
                    if policy == "fail-fast":
                        stop.set()

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for future in [pool.submit(run, index) for index in children]:
                future.result()
        counts = {"succeeded": 0, "failed": 0, "cancelled": 0, "refused": 0, "budgetExhausted": 0}
        for index, item in enumerate(plan):
            result = results[index]
            usage: dict[str, int] = {}
            if index in children:
                task_id = children[index]["taskId"]
                with self._task_lock:
                    root, tree = self._tree(task_id)
                    row = next(row for row in tree.timeline() if row["taskId"] == task_id)
                    if row["state"] == "running":
                        tree.finish(task_id)  # the unused reservation goes back to the parent
                        self._persist(root)
                        row = next(row for row in tree.timeline() if row["taskId"] == task_id)
                usage = {key: int(row["spent"].get(key, 0)) for key in ("tokens", "costCents", "steps")}
            result["usage"] = usage
            counts["budgetExhausted" if result["outcome"] == "budget-exhausted" else result["outcome"]] += 1
            self._ledger_action("task.branch.finished", by, {
                "taskId": parent_id, "toTask": result["taskId"], "toAgent": result["agent"], "outcome": result["outcome"],
                **({"usage": usage} if usage else {}),
            })
        self._ledger_action("task.join", by, {"taskId": parent_id, "policy": policy, **counts})
        return {"policy": policy, "concurrency": concurrency, "complete": counts["succeeded"] == len(plan), "branches": results}

    def charge(self, envelope: Mapping[str, Any], **used: int) -> dict[str, int]:
        """Charge usage the host measured (``tokens``, ``costCents``, ``steps``) to a task; returns what it has left.

        Raises ``BudgetExhausted`` once a bound is passed: the branch halts, ``task.budget-exhausted`` is ledgered and the owner told.
        """
        with self._task_lock:
            root, tree = self._tree(envelope.get("taskId"))
            try:
                return tree.charge(envelope["taskId"], **used)
            finally:
                self._persist(root)

    def finish_task(self, envelope: Mapping[str, Any]) -> dict[str, int]:
        """Complete a task: its unused reservation goes back to its parent. Calls under a finished task are refused."""
        with self._task_lock:
            root, tree = self._tree(envelope.get("taskId"))
            try:
                return tree.finish(envelope["taskId"])
            finally:
                self._persist(root)

    def task_status(self, task_id: str) -> list[dict[str, Any]]:
        """Every task in this task's tree with its Agent, state, budget, spend, reservation and what is left."""
        return self._tree(task_id)[1].timeline()

    def _check_envelope(self, agent: Any, envelope: Mapping[str, Any]) -> None:
        state = self._state(agent)
        chain = envelope.get("delegationChain")
        if envelope.get("agent") != agent or not isinstance(chain, list) or not chain or chain[-1] != agent:
            raise TeamError(f"the task envelope does not belong to {agent}")
        outside = sorted(set(envelope.get("authority", [])) - set(TEAM.tool_names(state)))
        if outside:  # a forged or stale envelope can never grant a Tool the Agent was not compiled with
            raise TeamError(f"the task envelope names Tools {agent} does not hold: {', '.join(outside)}")
        _, tree = self._tree(envelope.get("taskId"))
        known = tree.snapshot()["nodes"][envelope["taskId"]]["envelope"]  # what this team started; the caller's copy is never trusted
        if known["agent"] != agent or sorted(known["authority"]) != sorted(envelope.get("authority", [])) \
                or known["originatingPrincipal"] != envelope.get("originatingPrincipal") or known["delegationChain"] != chain:
            raise TeamError("the task envelope does not match the task this team started")

    @contextlib.contextmanager
    def acting(self, agent: str, envelope: Mapping[str, Any]) -> Iterator[Any]:
        """Run governed calls on ``agent``'s runtime under this task: they carry its identifiers and cannot exceed its authority."""
        self._check_envelope(agent, envelope)
        runtime = self.runtime(agent)
        token = _TASK.set((agent, copy.deepcopy(dict(envelope))))
        try:
            yield runtime
        finally:
            _TASK.reset(token)

    def _task_for(self, agent: str) -> dict[str, Any] | None:
        """Resolver handed to each runtime: the attested task for a call, or ``None`` outside any task."""
        current = _TASK.get()
        if current is None:
            return None
        bound_to, envelope = current
        context = DELEGATION.governance_context(envelope)
        if bound_to != agent:  # the task belongs to another Agent's runtime: refuse and ledger rather than borrow its authority
            return {"governance": context, "authority": [], "halt": "task-bound-to-another-agent"}
        halt = None
        deadline = envelope.get("deadline")
        with self._task_lock:
            state = self._tree(envelope["taskId"])[1].state(envelope["taskId"])
        if deadline is not None and int(self.now()) >= deadline:
            halt = "task-deadline-passed"
        elif state == "halted":
            halt = "task-budget-exhausted"
        elif state == "finished":
            halt = "task-finished"
        task_id = envelope["taskId"]

        def charge_step() -> None:  # one step per governed call, taken before the call runs
            try:
                self.charge({"taskId": task_id}, steps=1)
            except TASK_BUDGET.BudgetExhausted as exc:
                raise PermissionError("task-budget-exhausted") from exc

        return {"governance": context, "authority": list(envelope.get("authority", [])), "halt": halt, "charge": charge_step}

    # -- a person as a task step --------------------------------------------------------------

    def _handoff_root(self, envelope: Mapping[str, Any]) -> pathlib.Path:
        self._check_envelope(envelope.get("agent"), envelope)
        return self.agent_dir(envelope["agent"]) / "handoffs"

    def _already_ledgered(self, action: str, handoff_id: str, outcome: str | None = None) -> bool:
        try:
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return False
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            details = event.get("details") or {}
            if event.get("action") == action and details.get("handoffId") == handoff_id and (outcome is None or details.get("outcome") == outcome):
                return True
        return False

    def open_human_step(
        self,
        envelope: Mapping[str, Any],
        *,
        step: str,
        assignee: str,
        ask: str,
        context: str = "",
        timeout_hours: int = 24,
        fallback: str = "fail",
        escalate_to: str | None = None,
    ) -> dict[str, Any]:
        """Pause the task on a named person: a Tier 4 hand-off on this Agent's own store, ledgered once against the task."""
        root = self._handoff_root(envelope)
        with self._task_lock:
            document = HUMAN_STEP.open_step(
                root, envelope, step=step, assignee=assignee, ask=ask, context=context, timeout_hours=timeout_hours,
                fallback=fallback, escalate_to=escalate_to, now=int(self.now()),
            )
            if not self._already_ledgered("task.human-step.opened", document["id"]):
                HUMAN_STEP.ledger_opened(GOVERN, self.audit_path, envelope, step, document)
        return document

    def human_step(self, envelope: Mapping[str, Any], step: str) -> dict[str, Any]:
        """Where the step stands (waiting, resumed, declined, timed-out). The person's answer and a timeout are ledgered once each."""
        root = self._handoff_root(envelope)
        with self._task_lock:
            result = HUMAN_STEP.resolution(root, envelope, step, now=int(self.now()))
            handoff_id = result.get("handoffId")
            if result["state"] in ("resumed", "declined") and not self._already_ledgered("task.human-step.resolved", handoff_id):
                HUMAN_STEP.ledger_resolved(GOVERN, self.audit_path, envelope, step, HUMAN_STEP._handoff().find(root, handoff_id))
            elif result["state"] == "timed-out" and not self._already_ledgered("task.human-step.timed-out", handoff_id):
                HUMAN_STEP.ledger_timed_out(GOVERN, self.audit_path, envelope, step, HUMAN_STEP._handoff().find(root, handoff_id))
        return result


def load_states(path: pathlib.Path) -> list[dict[str, Any]]:
    """Compiled desired states from a ``compile-workspace.py`` output (one Agent or ``{"agents": [...]}``)."""
    document = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if isinstance(document, dict) and isinstance(document.get("agents"), list):
        return [dict(item) for item in document["agents"]]
    if isinstance(document, dict) and document.get("schemaVersion") == 1:
        return [document]
    raise TeamError("expected compiled desired state or a compile-workspace.py multi-Agent output")

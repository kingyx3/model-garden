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

Nothing here holds a credential or reaches the network. Not covered: a model-callable delegation Tool, the runtime-native
handoff/A2A decision, Docker/Hermes deployment of several Agents, and any live evidence.
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
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
AGENT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
DEFAULT_PORT = 9120


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
    ) -> None:
        copies = [copy.deepcopy(dict(state)) for state in states]
        # Raises TeamError for no Agents or two Agents with one name (the name identifies approvals, attribution and ledger records).
        report = TEAM.plan(copies, environment_binding=environment_binding, registry=registry, now=int(now()))
        for state in copies:
            name = TEAM.agent_name(state)
            if not AGENT_NAME.fullmatch(name):
                raise TeamError(f"Agent name {name!r} must be a lowercase DNS-like name (it becomes a directory and a process identity)")
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
        for name, state in self._states.items():
            self._prepare(name, state)

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

    # -- tasks and delegation ---------------------------------------------------------------

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
        return DELEGATION.root_envelope(
            task_id=task_id, agent=agent, originating_principal=originating_principal, goal=goal,
            agent_grants=TEAM.tool_names(state), originator_permissions=originator_permissions,
            budget=budget, deadline=deadline, constraints=constraints, required_capabilities=required_capabilities,
        )

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
        (which the parent's authority already narrows), so a hop can only shrink what may be done.
        """
        delegator = parent.get("agent")
        self._check_envelope(delegator, parent)
        delegate_state = self._state(to_agent)
        with self._lock:  # fan-out is counted from the ledger, so it survives a restart and cannot be raced past
            try:
                child = DELEGATION.delegate(
                    parent, to_agent=to_agent, child_task_id=child_task_id, goal=goal,
                    delegate_grants=TEAM.tool_names(delegate_state), originator_permissions=parent["authority"],
                    required_capabilities=required_capabilities, budget=budget, deadline=deadline, constraints=constraints,
                    children_so_far=self._hops_from(parent["taskId"]), max_depth=self.max_depth, max_fan_out=self.max_fan_out,
                    now=int(self.now()),
                )
            except DELEGATION.DelegationError as exc:
                GOVERN.record_operator_action(
                    self.audit_path, "delegation.refused", f"agent:{delegator}",
                    {"fromTask": parent["taskId"], "toAgent": to_agent, "reason": str(exc)[:300]},
                )
                raise
            DELEGATION.record_hop(GOVERN, self.audit_path, parent, child)
        return child

    def _hops_from(self, task_id: str) -> int:
        count = 0
        try:
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return 0
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("action") == "delegation.hop" and (event.get("details") or {}).get("fromTask") == task_id:
                count += 1
        return count

    def _check_envelope(self, agent: Any, envelope: Mapping[str, Any]) -> None:
        state = self._state(agent)
        chain = envelope.get("delegationChain")
        if envelope.get("agent") != agent or not isinstance(chain, list) or not chain or chain[-1] != agent:
            raise TeamError(f"the task envelope does not belong to {agent}")
        outside = sorted(set(envelope.get("authority", [])) - set(TEAM.tool_names(state)))
        if outside:  # a forged or stale envelope can never grant a Tool the Agent was not compiled with
            raise TeamError(f"the task envelope names Tools {agent} does not hold: {', '.join(outside)}")

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
        if deadline is not None and int(self.now()) >= deadline:
            halt = "task-deadline-passed"
        elif DELEGATION.exhausted(envelope):
            halt = "task-budget-exhausted"
        return {"governance": context, "authority": list(envelope.get("authority", [])), "halt": halt}


def load_states(path: pathlib.Path) -> list[dict[str, Any]]:
    """Compiled desired states from a ``compile-workspace.py`` output (one Agent or ``{"agents": [...]}``)."""
    document = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if isinstance(document, dict) and isinstance(document.get("agents"), list):
        return [dict(item) for item in document["agents"]]
    if isinstance(document, dict) and document.get("schemaVersion") == 1:
        return [document]
    raise TeamError("expected compiled desired state or a compile-workspace.py multi-Agent output")

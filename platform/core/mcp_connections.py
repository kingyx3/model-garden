"""Private, tenant-scoped activation state for reviewed MCP connections (#87, #159).

This is an operator-side data store, not a public Tool, an OAuth implementation or
evidence that an external server is live. Stage/replacement is disabled by default.
Activation needs the named owner's consent, a separate named operator, approved
fingerprints for *every* permitted operation, and an independent usable coworker grant.
The runtime resolves a stored connection ID, never a caller-supplied endpoint.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

SCHEMA_VERSION = 1
MAX_CONNECTIONS = 100
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_AGENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_PERSON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")


class ConnectionRefused(PermissionError):
    """Connection is not operator-approved, disabled, changed, or for a different Agent."""


def _mcp():
    key = "modelgarden_connection_mcp"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, pathlib.Path(__file__).with_name("mcp_passthrough.py"))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


def _gateway():
    key = "modelgarden_connection_gateway"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, pathlib.Path(__file__).with_name("mcp_executor.py"))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


def _actor(value: str, label: str) -> str:
    if not isinstance(value, str) or not _PERSON.fullmatch(value):
        raise ConnectionRefused(f"{label} must be a named person")
    return value


def _digest(declaration: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(declaration, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class McpConnectionStore:
    def __init__(self, root: pathlib.Path, *, tenant: str, audit: Callable[..., Any] | None = None) -> None:
        if not isinstance(tenant, str) or not _TENANT.fullmatch(tenant):
            raise ConnectionRefused("invalid tenant identity")
        self.root = pathlib.Path(root)
        self.tenant = tenant
        self.path = self.root / f"{tenant}.mcp-connections.json"
        self._audit = audit

    def _key(self, agent: str, connection_id: str) -> str:
        if not isinstance(agent, str) or not _AGENT.fullmatch(agent):
            raise ConnectionRefused("invalid coworker identity")
        if not isinstance(connection_id, str) or not _ID.fullmatch(connection_id):
            raise ConnectionRefused("invalid connection id")
        return f"{agent}/{connection_id}"

    def _record(self, action: str, by: str, entry: Mapping[str, Any], **extra: Any) -> None:
        if self._audit is not None:
            self._audit(action, by, {
                "tenant": self.tenant, "agent": entry["agent"], "connectionId": entry["id"],
                "server": entry["declaration"]["name"], "revision": entry["revision"],
                **extra,
            })

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        fd = os.open(self.root / f".{self.tenant}.mcp-connections.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            state: dict[str, Any] = {"schemaVersion": SCHEMA_VERSION, "tenant": self.tenant, "connections": {}}
            if self.path.exists():
                if self.path.is_symlink():
                    raise ConnectionRefused("connection store must not be a symlink")
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if (not isinstance(loaded, dict) or loaded.get("schemaVersion") != SCHEMA_VERSION
                        or loaded.get("tenant") != self.tenant or not isinstance(loaded.get("connections"), dict)):
                    raise ConnectionRefused("invalid tenant-scoped connection store")
                state = loaded
            previous = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != previous:
                handle, tmpname = tempfile.mkstemp(prefix=f".{self.tenant}.", suffix=".tmp", dir=self.root)
                try:
                    with os.fdopen(handle, "w", encoding="utf-8") as stream:
                        os.fchmod(stream.fileno(), 0o600)
                        json.dump(state, stream, sort_keys=True)
                        stream.write("\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(tmpname, self.path)
                finally:
                    if os.path.exists(tmpname):
                        os.unlink(tmpname)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def stage(
        self, connection_id: str, *, agent: str, declaration: Mapping[str, Any],
        by: str, now: int, replace: bool = False,
    ) -> dict[str, Any]:
        """Operator registers/reviews an exact manifest; changes always deactivate it."""
        key = self._key(agent, connection_id)
        who = _actor(by, "operator")
        server = _mcp().validate_server(declaration)
        if not server.get("owner"):
            raise ConnectionRefused("an MCP connection must name a client owner")
        # Every executable operation must name a portable, catalog-classified Tool.
        for tool in server["tools"].values():
            if "capability" not in tool:
                raise ConnectionRefused("every operation needs an explicit portable Tool mapping")
            catalog = _gateway()._capabilities().catalog_tool(tool["tool"])
            if not catalog or catalog["risk"] != tool["risk"]:
                raise ConnectionRefused("operation and portable Tool catalog risks must agree")
        revision = _digest(server)
        entry = {"id": connection_id, "agent": agent, "declaration": copy.deepcopy(dict(declaration)),
                 "revision": revision, "active": False, "stagedBy": who, "stagedAt": now}
        with self._locked() as state:
            old = state["connections"].get(key)
            if old is not None and not replace:
                raise ConnectionRefused("connection already staged; use reviewed replacement")
            if old is None and replace:
                raise ConnectionRefused("no connection to replace")
            if old is None and len(state["connections"]) >= MAX_CONNECTIONS:
                raise ConnectionRefused("too many connections for this tenant")
            state["connections"][key] = entry
        self._record("mcp.connection-stage" if old is None else "mcp.connection-replace", who, entry,
                     deactivated=True, previousRevision=old["revision"] if old else None)
        return {"id": connection_id, "agent": agent, "revision": revision, "active": False}

    def activate(
        self, connection_id: str, *, agent: str, consented_by: str, by: str,
        offered: Sequence[Mapping[str, Any]], approvals: Any, grants: Any, now: int,
    ) -> dict[str, Any]:
        """Prove consent + all schema pins + own grant before activation; never allow-all."""
        owner = _actor(consented_by, "consenting owner")
        reviewer = _actor(by, "operator")
        key = self._key(agent, connection_id)
        with self._locked() as state:
            entry = copy.deepcopy(state["connections"].get(key))
        if entry is None:
            raise ConnectionRefused("no such coworker connection")
        server = _mcp().validate_server(entry["declaration"])
        if owner != server["owner"]:
            raise ConnectionRefused("consent must come from the named client owner")
        if grants.tenant != self.tenant or approvals.tenant != self.tenant:
            raise ConnectionRefused("grants and schema approvals must belong to this tenant")
        if not isinstance(offered, Sequence) or isinstance(offered, (str, bytes)) or len(offered) > _mcp().MAX_TOOLS * 5:
            raise ConnectionRefused("upstream tool listing is not bounded")
        catalog = {t.get("name"): t for t in offered if isinstance(t, Mapping)}
        fingerprints = {}
        for upstream, spec in server["tools"].items():
            tool = catalog.get(upstream)
            if tool is None or sum(1 for item in offered if isinstance(item, Mapping) and item.get("name") == upstream) != 1:
                raise ConnectionRefused("a declared upstream operation is absent or duplicated")
            _mcp().call_gate(entry["declaration"], tool, approvals)
            # prepare enforces matched provider, exact credential reference and independent grant.
            _gateway().prepare(
                entry["declaration"], registry=grants, tenant=self.tenant, attested_agent=agent, upstream=upstream,
                enabled_tools=(spec["tool"],), originator_permissions=(spec["tool"],), now=now,
            )
            fingerprints[upstream] = _mcp().fingerprint(tool)
        with self._locked() as state:
            actual = state["connections"].get(key)
            if actual is None or actual["revision"] != entry["revision"]:
                raise ConnectionRefused("connection changed during review; review the new revision")
            actual.update(active=True, approvedBy=reviewer, consentedBy=owner,
                          activatedAt=now, reviewedFingerprints=fingerprints)
            outcome = copy.deepcopy(actual)
        self._record("mcp.connection-activate", reviewer, outcome,
                     ownerConsent=owner, reviewedTools=sorted(fingerprints))
        return {"id": connection_id, "agent": agent, "revision": outcome["revision"], "active": True}

    def disable(self, connection_id: str, *, agent: str, by: str) -> dict[str, Any]:
        who = _actor(by, "operator")
        key = self._key(agent, connection_id)
        with self._locked() as state:
            item = state["connections"].get(key)
            if item is None:
                raise ConnectionRefused("no such coworker connection")
            item["active"] = False
            snapshot = copy.deepcopy(item)
        self._record("mcp.connection-disable", who, snapshot)
        return {"id": connection_id, "agent": agent, "active": False}

    def resolve(self, connection_id: str, *, agent: str) -> dict[str, Any]:
        """Only the internal runtime can read this secret-reference-bearing record."""
        key = self._key(agent, connection_id)
        with self._locked() as state:
            item = copy.deepcopy(state["connections"].get(key))
        if item is None or item.get("agent") != agent or not item.get("active"):
            raise ConnectionRefused("MCP connection is not active for this coworker")
        if item.get("revision") != _digest(_mcp().validate_server(item["declaration"])) or not item.get("reviewedFingerprints"):
            raise ConnectionRefused("MCP connection requires renewed review")
        return item

    def list_public(self, *, agent: str) -> list[dict[str, Any]]:
        """Operator status without even revealing opaque secret reference names."""
        self._key(agent, "status")
        with self._locked() as state:
            items = [item for item in state["connections"].values() if item.get("agent") == agent]
        return sorted((
            {"id": item["id"], "server": item["declaration"]["name"], "agent": agent,
             "revision": item["revision"], "active": bool(item["active"])}
            for item in items
        ), key=lambda x: x["id"])

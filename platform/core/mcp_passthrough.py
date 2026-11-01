"""Governed MCP passthrough: the declaration and exposure rules for a vendor or client MCP server.

Connecting a system through its official MCP server must not weaken governance (#87). This module is the
credential-free contract for what the governed sidecar may expose from such a server; it contains **no MCP client**
and is not wired into the sidecar. The sidecar would be the only thing that ever talks to the upstream server;
Hermes still sees only the sidecar's own Tools.

- **Public endpoints only.** The URL is also checked by ``endpoint_guard.parse_endpoint``: https on port 443, a public host
  name or address (no localhost, private, link-local, metadata or numeric-trick hosts). Resolution is checked and pinned at
  connect time with ``endpoint_guard.qualify`` / ``assert_pinned``; nothing here makes a network call.
- **Explicit declaration.** A server is declared with a name, an https URL (no embedded credentials), a ``secret://``
  credential *reference* and an allow-list of upstream tool names. Every allowed tool carries an explicit risk class
  (``read``, ``write``, ``privileged``, ``external-communication``) and may also be mapped to a portable capability Tool
  such as ``crm.read``; an unclassified entry is refused when the declaration is validated, so nothing is exposed by default.
- **Unlisted means invisible.** An upstream tool that is not in the allow-list is never exposed, however the server
  describes it. A listed tool that the server no longer offers is reported missing, not exposed.
- **Metadata review.** Before a tool can be approved, exposed or forwarded, ``mcp_tool_review.review`` checks its description and
  schema for hidden characters, instruction-like text (tool poisoning), oversized or remote-referencing schemas; a blocking
  finding refuses approval and hides the tool even if an earlier pin exists, naming only a code and a path.
- **Schema pinning.** An operator approves a tool by fingerprint (a SHA-256 over its name, description and input
  schema). If the upstream definition later changes (a new parameter, or a rewritten description, which is how a
  poisoned tool is introduced) the tool is hidden and calls fail closed until it is approved again.
- **Risk drives approval.** Anything but ``read`` is defined with ``approvalRequired: true``; the client's signed
  policy can relax that for named tools, exactly as for native Tools.
- **A client's own REST API.** A declaration may carry ``api``, a ``custom_api`` definition, served by the sidecar's REST transport
  instead of an MCP client. It must agree with the declaration exactly (name, base URL, credential reference, operations and their
  risk), so everything above (pins, review, grants, activation) applies to it unchanged.
- **Ledger.** ``ledger_fields`` gives ``tier: 2``, the upstream server identity and tool name (never arguments or
  results), for the sidecar's existing ledger context.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import stat
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
RISKS = ("read", "write", "privileged", "external-communication")
TIER = 2
MAX_TOOLS = 100
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_UPSTREAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SECRET_REF = re.compile(r"^secret://[A-Za-z0-9][A-Za-z0-9._/-]{2,199}$")
_CAPABILITY_TOOL = re.compile(r"^[a-z][a-z0-9_-]*(\.[a-z0-9_-]+)+$")
_PERSON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
Audit = Callable[[str, str, dict[str, Any]], Any]


class McpDeclarationError(ValueError):
    """Raised for an invalid server declaration."""


class SchemaDriftError(PermissionError):
    """Raised when an upstream tool is not approved, or its definition changed since it was."""


def _guard():
    name = "model_garden_mcp_endpoint_guard"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name("endpoint_guard.py"))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _review():
    name = "model_garden_mcp_tool_review"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name("mcp_tool_review.py"))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _custom_api():
    name = "model_garden_mcp_custom_api"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name("custom_api.py"))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def fingerprint(tool: Mapping[str, Any]) -> str:
    """A stable digest of what the model would be shown and what the call accepts."""
    material = {"name": tool.get("name"), "description": tool.get("description", ""), "inputSchema": tool.get("inputSchema", {})}
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def validate_server(declaration: Any) -> dict[str, Any]:
    """The normalised declaration, or ``McpDeclarationError``."""
    if not isinstance(declaration, Mapping):
        raise McpDeclarationError("an MCP server declaration is an object")
    unknown = sorted(set(declaration) - {"name", "url", "credentialRef", "tools", "owner", "api"})
    if unknown:
        raise McpDeclarationError("unsupported keys: " + ", ".join(unknown))
    name = declaration.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise McpDeclarationError("server name must be lowercase letters, digits and hyphens")
    url = declaration.get("url")
    parts = urlsplit(url) if isinstance(url, str) else None
    if parts is None or parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise McpDeclarationError("url must be an https address without credentials, query or fragment")
    try:  # the address a customer typed must not be a private, metadata or local destination (resolution is checked at connect time)
        _guard().parse_endpoint(url)
    except ValueError as exc:
        raise McpDeclarationError(f"url is not an acceptable public endpoint: {exc}") from exc
    reference = declaration.get("credentialRef")
    if not isinstance(reference, str) or not _SECRET_REF.match(reference):
        raise McpDeclarationError("the credential must be a secret:// reference, never the value")
    owner = declaration.get("owner")
    if owner is not None and (not isinstance(owner, str) or not _PERSON.match(owner)):
        raise McpDeclarationError("owner must be a named person")
    tools = declaration.get("tools")
    if not isinstance(tools, Mapping) or not tools or len(tools) > MAX_TOOLS:
        raise McpDeclarationError(f"declare 1 to {MAX_TOOLS} allowed upstream tools; there is no allow-all")
    normal: dict[str, dict[str, Any]] = {}
    exposed: dict[str, str] = {}
    for upstream, entry in tools.items():
        if not isinstance(upstream, str) or not _UPSTREAM.match(upstream):
            raise McpDeclarationError(f"upstream tool name {upstream!r} is not a plain identifier")
        if not isinstance(entry, Mapping) or set(entry) - {"capability", "risk"}:
            raise McpDeclarationError(f"{upstream}: give a risk class and optionally a capability Tool, nothing else")
        capability, risk = entry.get("capability"), entry.get("risk")
        if risk is None:
            raise McpDeclarationError(f"{upstream}: an allowed tool needs an explicit risk class (and may also map to a capability Tool)")
        if risk not in RISKS:
            raise McpDeclarationError(f"{upstream}: risk must be one of {', '.join(RISKS)}")
        if capability is not None and (not isinstance(capability, str) or not _CAPABILITY_TOOL.match(capability)):
            raise McpDeclarationError(f"{upstream}: capability must be a portable Tool name such as crm.read")
        exposed_name = capability or f"mcp.{name}.{upstream.lower().replace('_', '-').replace('.', '-')}"
        if not _CAPABILITY_TOOL.match(exposed_name):
            raise McpDeclarationError(f"{upstream}: cannot be exposed as a Tool name")
        if exposed_name in exposed:
            raise McpDeclarationError(f"{upstream} and {exposed[exposed_name]} would both be exposed as {exposed_name}")
        exposed[exposed_name] = upstream
        normal[upstream] = {"tool": exposed_name, "risk": risk, **({"capability": capability} if capability else {})}
    api = _api_definition(declaration, name=name, url=url, reference=reference, tools=normal) if "api" in declaration else None
    return {"name": name, "url": url, "credentialRef": reference, "tools": normal, **({"owner": owner} if owner else {}), **({"api": api} if api else {})}


def _api_definition(declaration: Mapping[str, Any], *, name: str, url: str, reference: str, tools: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """A client's own REST API (``custom_api``) served through this declaration: it must agree with it on every point.

    The definition's name is the server name (so schema approvals are keyed alike), its base URL is the declared URL (so the
    address qualified and pinned is the one requests go to), it authenticates with the declared credential reference, and the
    allow-list is exactly its operations at exactly their risk. Anything else is refused rather than reconciled.
    """
    try:
        api = _custom_api().validate_definition(declaration["api"])
    except ValueError as exc:
        raise McpDeclarationError(f"api: {exc}") from exc
    if api["name"] != name:
        raise McpDeclarationError("api: the API definition's name must be the server name")
    if api["baseUrl"] != url:
        raise McpDeclarationError("api: url must be the API definition's base URL exactly")
    if api["auth"]["type"] == "none" or api["auth"].get("credentialRef") != reference:
        raise McpDeclarationError("api: the API must authenticate with the declared credential reference")
    if set(tools) != set(api["operations"]):
        raise McpDeclarationError("api: allow exactly the API definition's operations")
    for op_id, entry in tools.items():
        if entry["risk"] != api["operations"][op_id]["risk"]:
            raise McpDeclarationError(f"{op_id}: risk must be the API operation's risk ({api['operations'][op_id]['risk']})")
    return api


class SchemaApprovals:
    """Operator-approved fingerprints per upstream tool, one private file per tenant."""

    def __init__(self, root: pathlib.Path, *, tenant: str, audit: Audit | None = None) -> None:
        if not isinstance(tenant, str) or not _TENANT.match(tenant):
            raise McpDeclarationError("tenant must be a lowercase client-environment slug such as acme-prod")
        self.tenant = tenant
        self.root = pathlib.Path(root)
        self.path = self.root / f"{tenant}.mcp-approvals.json"
        self._audit = audit

    @staticmethod
    def _private_file(path: pathlib.Path, *, missing: bool = False) -> os.stat_result | None:
        """Reject aliases and exposed approval/lock files instead of following them."""
        try:
            info = path.lstat()
        except FileNotFoundError:
            if missing:
                return None
            raise McpDeclarationError("unsafe MCP schema approval state is missing") from None
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise McpDeclarationError("unsafe MCP schema approval state path (type, owner, permissions or links)")
        return info

    @staticmethod
    def _checked_open(fd: int, expected: os.stat_result) -> os.stat_result:
        actual = os.fstat(fd)
        if (not stat.S_ISREG(actual.st_mode) or actual.st_uid != os.geteuid()
                or actual.st_mode & 0o077 or actual.st_nlink != 1
                or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)):
            raise McpDeclarationError("unsafe MCP schema approval state changed during open")
        return actual

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root_info = self.root.lstat()
        if (not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.geteuid()
                or root_info.st_mode & 0o077):
            raise McpDeclarationError("unsafe MCP schema approval root (type, owner or permissions)")
        lock_path = self.root / f".{self.tenant}.mcp-approvals.lock"
        try:
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        except OSError as exc:
            raise McpDeclarationError("unsafe MCP schema approval lock could not be opened") from exc
        try:
            self._checked_open(descriptor, self._private_file(lock_path))
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self._checked_open(descriptor, self._private_file(lock_path))
            state: dict[str, Any] = {"schemaVersion": SCHEMA_VERSION, "tenant": self.tenant, "approved": {}}
            original = self._private_file(self.path, missing=True)
            if original is not None:
                try:
                    fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                except OSError as exc:
                    raise McpDeclarationError("unsafe MCP schema approval index could not be opened") from exc
                with os.fdopen(fd, "rb") as stream:
                    observed = self._checked_open(stream.fileno(), original)
                    if observed.st_size > 4 * 1024 * 1024:
                        raise McpDeclarationError("MCP schema approval index exceeds its size limit")
                    raw = stream.read(4 * 1024 * 1024 + 1)
                    if len(raw) > 4 * 1024 * 1024:
                        raise McpDeclarationError("MCP schema approval index exceeds its size limit")
                try:
                    loaded = json.loads(raw.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise McpDeclarationError("invalid MCP schema approval index") from exc
                if (not isinstance(loaded, dict) or loaded.get("schemaVersion") != SCHEMA_VERSION
                        or loaded.get("tenant") != self.tenant):
                    raise McpDeclarationError("approval store has an unsupported format or belongs to another tenant")
                approved = loaded.get("approved")
                if not isinstance(approved, dict) or any(
                    not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", key)
                    or not isinstance(value, dict) or set(value) != {"fingerprint", "approvedBy", "approvedAt"}
                    or not isinstance(value["fingerprint"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["fingerprint"])
                    or not isinstance(value["approvedBy"], str) or not _PERSON.fullmatch(value["approvedBy"])
                    or not isinstance(value["approvedAt"], int) or isinstance(value["approvedAt"], bool)
                    or value["approvedAt"] < 0
                    for key, value in approved.items()
                ):
                    raise McpDeclarationError("invalid MCP schema approval index entries")
                state = loaded
            before = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != before:
                current = self._private_file(self.path, missing=True)
                if (current is None) != (original is None) or (
                    current is not None and original is not None
                    and (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino)
                ):
                    raise McpDeclarationError("unsafe MCP schema approval index changed during update")
                self._checked_open(descriptor, self._private_file(lock_path))
                encoded = (json.dumps(state, sort_keys=True) + "\n").encode("utf-8")
                if len(encoded) > 4 * 1024 * 1024:
                    raise McpDeclarationError("MCP schema approval index exceeds its size limit")
                handle, temporary = tempfile.mkstemp(prefix=f".{self.tenant}.mcp-approval-", dir=self.root)
                try:
                    with os.fdopen(handle, "wb") as stream:
                        stream.write(encoded)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, self.path)
                finally:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(temporary)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def approve(self, server: str, tool: Mapping[str, Any], *, by: str, now: int) -> str:
        """Pin the tool's current definition; approving a changed definition replaces the old pin."""
        if not isinstance(by, str) or not _PERSON.match(by):
            raise McpDeclarationError("approval names the person who reviewed the tool")
        if not isinstance(server, str) or not _NAME.fullmatch(server):
            raise McpDeclarationError("approval needs a valid server name")
        if not isinstance(now, int) or isinstance(now, bool) or now < 0:
            raise McpDeclarationError("approval needs a nonnegative integer timestamp")
        if not isinstance(tool.get("name"), str) or not _UPSTREAM.match(tool["name"]):
            raise McpDeclarationError("the upstream tool needs a plain name")
        unsafe = _review().blocking_codes(_review().review(tool))
        if unsafe:
            raise McpDeclarationError(f"{tool['name']} cannot be approved: its own description or schema looks unsafe ({', '.join(unsafe)}); ask the server's owner to fix it")
        print_ = fingerprint(tool)
        key = f"{server}/{tool['name']}"
        with self._locked() as state:
            previous = state["approved"].get(key)
            state["approved"][key] = {"fingerprint": print_, "approvedBy": by, "approvedAt": now}
        if self._audit is not None:
            self._audit("mcp.schema-approve", by, {"server": server, "tool": tool["name"], "fingerprint": print_,
                                                    "replaced": bool(previous and previous["fingerprint"] != print_), "tier": TIER})
        return print_

    def status(self, server: str, tool: Mapping[str, Any]) -> str:
        """``approved``, ``unapproved`` or ``drifted`` for the upstream tool's current definition."""
        with self._locked() as state:
            pinned = state["approved"].get(f"{server}/{tool.get('name')}")
        if pinned is None:
            return "unapproved"
        return "approved" if pinned["fingerprint"] == fingerprint(tool) else "drifted"

    def check(self, server: str, tool: Mapping[str, Any]) -> None:
        """Fail closed unless the tool's current definition is the approved one."""
        state = self.status(server, tool)
        if state != "approved":
            raise SchemaDriftError(
                f"upstream tool {server}/{tool.get('name')} is {state}; it stays hidden until an operator reviews and approves its current definition"
            )


def exposed_catalog(
    declaration: Mapping[str, Any], upstream_tools: Sequence[Mapping[str, Any]], approvals: SchemaApprovals
) -> dict[str, Any]:
    """What the sidecar may expose now: allow-listed, classified, present upstream and approved at their current fingerprint.

    ``upstream_tools`` is the server's own tool listing (name, description, inputSchema). Everything not exposed is
    named with a reason so an operator can act; unlisted upstream tools appear only as a count.
    """
    server = validate_server(declaration)
    offered = {tool.get("name"): tool for tool in upstream_tools if isinstance(tool, Mapping)}
    exposed, hidden = [], []
    for upstream, entry in sorted(server["tools"].items()):
        tool = offered.get(upstream)
        if tool is None:
            hidden.append({"upstream": upstream, "reason": "missing-upstream"})
            continue
        unsafe = _review().blocking_codes(_review().review(tool))
        if unsafe:  # even an approved pin is hidden if the definition it pinned (or a drifted one) is unsafe
            hidden.append({"upstream": upstream, "reason": "unsafe-metadata", "findings": unsafe})
            continue
        state = approvals.status(server["name"], tool)
        if state != "approved":
            hidden.append({"upstream": upstream, "reason": state})
            continue
        exposed.append({"tool": entry["tool"], "upstream": upstream, "risk": entry["risk"], "approvalRequired": entry["risk"] != "read"})
    unlisted = sorted(name for name in offered if name not in server["tools"])
    return {"server": server["name"], "tier": TIER, "exposed": exposed, "hidden": hidden, "unlistedUpstreamTools": len(unlisted)}


def tool_definitions(declaration: Mapping[str, Any], catalog: Mapping[str, Any], *, owner: str = "ai-platform") -> list[dict[str, Any]]:
    """``modelgarden.ai/v1`` Tool resources for the exposed tools (type ``mcp``), valid against the Tool schema."""
    server = validate_server(declaration)
    definitions = []
    for item in catalog["exposed"]:
        spec: dict[str, Any] = {"type": "mcp", "risk": item["risk"], "approvalRequired": item["approvalRequired"]}
        capability = server["tools"][item["upstream"]].get("capability")
        if capability:
            spec["capability"] = re.sub(r"[^a-z]", "", capability.split(".", 1)[0])
        definitions.append({
            "apiVersion": "modelgarden.ai/v1", "kind": "Tool",
            "metadata": {"name": item["tool"], "owner": owner, "description": f"Upstream MCP tool {item['upstream']} on {server['name']} (governed passthrough)."},
            "spec": spec,
        })
    return definitions


def ledger_fields(declaration: Mapping[str, Any], upstream: str) -> dict[str, Any]:
    """Ledger context for a passthrough call: tier 2, which server and which upstream tool; never arguments or results."""
    server = validate_server(declaration)
    if upstream not in server["tools"]:
        raise SchemaDriftError(f"{upstream} is not on {server['name']}'s allow-list")
    return {"tier": TIER, "provider": f"mcp:{server['name']}", "mcpServer": server["name"], "mcpTool": upstream}


def call_gate(declaration: Mapping[str, Any], upstream_tool: Mapping[str, Any], approvals: SchemaApprovals) -> dict[str, Any]:
    """Everything that must hold before the sidecar forwards a call: allow-listed, and the live definition is the approved one."""
    server = validate_server(declaration)
    name = upstream_tool.get("name")
    entry = server["tools"].get(name) if isinstance(name, str) else None
    if entry is None:
        raise SchemaDriftError(f"{name} is not on {server['name']}'s allow-list")
    unsafe = _review().blocking_codes(_review().review(upstream_tool))
    if unsafe:
        raise SchemaDriftError(f"upstream tool {server['name']}/{name} has unsafe metadata ({', '.join(unsafe)}) and is not forwarded")
    approvals.check(server["name"], upstream_tool)
    return {**ledger_fields(declaration, name), "risk": entry["risk"], "approvalRequired": entry["risk"] != "read", "tool": entry["tool"]}

"""A customer's own REST API as a small set of explicitly declared, reviewed operations (#159, #89).

A client should be able to connect an API Model Garden has no connector for, without code and without the coworker ever holding
an unrestricted HTTP requester. This is the credential-free contract for that. It validates a **definition** (a base URL plus
named operations, each with a method, a path, bounded parameters and an optional request body), classifies every operation's
risk, lets a person approve the exact definition, and builds a checked request for one declared operation. It contains **no
HTTP client** and no secret: ``platform/runtime/upstream_http.RestApiSession`` sends the request it builds, adds the credential
resolved from the ``secret://`` reference and applies the limits below. A definition reaches the governed sidecar as the ``api``
part of an MCP connection declaration (``mcp_passthrough.validate_server``), so staging, owner consent, activation, per-coworker
grants, schema pins and exact-action approval are the same as for an MCP server.

- **Only what is declared.** There is no "call any URL" operation. Each operation is a method and a path under the base URL
  with named parameters; a request for anything else is refused. Path parameters can only be plain identifiers (letters,
  digits, ``. _ ~ -``), so an argument can never climb a path (``..``), add a segment, a query or a host.
- **Address checked like any customer endpoint.** The base URL goes through ``endpoint_guard.parse_endpoint`` (https, public host,
  no user information, port 443). Resolution and pinning are the connector's job (``endpoint_guard.qualify``).
- **Risk comes from the method and can only go up.** GET is ``read``, POST, PUT and PATCH are ``write``, DELETE is ``privileged``;
  a definition may mark an operation ``external-communication`` or ``privileged`` but never lower it. Anything but ``read`` needs
  approval by default, like a native Tool.
- **A person approves the exact definition.** ``operation_tools`` presents each operation to ``mcp_passthrough.SchemaApprovals``
  as a pinned tool (name, summary, schema, method and path), so approving pins its fingerprint and any later change to the
  operation hides it until it is approved again. The summaries and descriptions are also scanned by ``mcp_tool_review`` because
  an imported OpenAPI file is untrusted text.
- **Import proposes; it never activates.** ``import_openapi`` turns an OpenAPI 3 document into a definition for *only the
  operation ids a person selects*, resolving local ``#/`` references to a bounded depth and refusing remote ones, header
  parameters, and non-JSON bodies. The result is a proposal to review.
- **Secrets stay references.** Authentication is ``bearer``, ``header`` (an API-key header with a name that is not a reserved
  one) or ``basic``, each with a ``secret://`` reference; a literal credential anywhere in the definition is refused.
- **Limits for the sender.** ``LIMITS`` (timeout, response size, no redirects, request body size) are for the connector to
  enforce; ``build_request`` already bounds argument and body sizes.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import re
import sys
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlencode

METHOD_RISK = {"GET": "read", "POST": "write", "PUT": "write", "PATCH": "write", "DELETE": "privileged"}
RISK_ORDER = ("read", "write", "external-communication", "privileged")
AUTH_TYPES = ("bearer", "header", "basic", "none")
LIMITS = {"timeoutSeconds": 15, "maxResponseBytes": 1_048_576, "maxRequestBytes": 262_144, "followRedirects": False}
MAX_OPERATIONS = 50
MAX_PARAMETERS = 20
MAX_STRING = 4000
MAX_DEPTH = 5
MAX_REF_DEPTH = 4
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_OP = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
_PARAM = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,40}$")
_SEGMENT = re.compile(r"^(?:[A-Za-z0-9._~-]{1,64}|\{[A-Za-z][A-Za-z0-9_]{0,40}\})$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
_SECRET_REF = re.compile(r"^secret://[A-Za-z0-9][A-Za-z0-9._/-]{2,199}$")
_HEADER = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,40}$")
_RESERVED_HEADERS = {"host", "content-length", "content-type", "cookie", "set-cookie", "authorization", "proxy-authorization", "transfer-encoding", "connection",
                     "upgrade", "te", "trailer", "expect", "forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip", "accept-encoding"}
_PERSON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")
_TYPES = ("string", "integer", "number", "boolean")


class CustomApiError(ValueError):
    """Raised for an invalid definition, import or request; nothing is sent or stored when it is raised."""


def _module(name: str, filename: str):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name(filename))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _guard():
    return _module("model_garden_custom_api_guard", "endpoint_guard.py")


def _review():
    return _module("model_garden_custom_api_review", "mcp_tool_review.py")


# -- schemas ------------------------------------------------------------------------------


def _schema(node: Any, where: str, depth: int = 0) -> dict[str, Any]:
    """A bounded schema subset: scalar types, enum, maxLength, minimum/maximum, objects with properties, arrays with items."""
    if depth > MAX_DEPTH or not isinstance(node, Mapping):
        raise CustomApiError(f"{where}: a schema is an object at most {MAX_DEPTH} levels deep")
    allowed = {"type", "description", "enum", "maxLength", "minimum", "maximum", "properties", "required", "items", "additionalProperties"}
    extra = sorted(set(node) - allowed)
    if extra:
        raise CustomApiError(f"{where}: unsupported schema keys: {', '.join(extra)}")
    kind = node.get("type")
    if kind not in (*_TYPES, "object", "array"):
        raise CustomApiError(f"{where}: type must be string, integer, number, boolean, object or array")
    result: dict[str, Any] = {"type": kind}
    if isinstance(node.get("description"), str):
        result["description"] = node["description"][:500]
    if "enum" in node:
        values = node["enum"]
        if not isinstance(values, list) or not 1 <= len(values) <= 50 or not all(isinstance(item, (str, int, float, bool)) for item in values):
            raise CustomApiError(f"{where}: enum is 1 to 50 plain values")
        result["enum"] = values
    for key in ("maxLength", "minimum", "maximum"):
        if key in node:
            if isinstance(node[key], bool) or not isinstance(node[key], (int, float)):
                raise CustomApiError(f"{where}: {key} must be a number")
            result[key] = node[key]
    if kind == "string":
        result["maxLength"] = min(int(result.get("maxLength", MAX_STRING)), MAX_STRING)
    if kind == "object":
        properties = node.get("properties", {})
        if not isinstance(properties, Mapping) or len(properties) > MAX_PARAMETERS:
            raise CustomApiError(f"{where}: an object has at most {MAX_PARAMETERS} properties")
        result["properties"] = {}
        for name, sub in properties.items():
            if not isinstance(name, str) or not _PARAM.match(name):
                raise CustomApiError(f"{where}: property names are plain identifiers")
            result["properties"][name] = _schema(sub, f"{where}.{name}", depth + 1)
        required = node.get("required", [])
        if not isinstance(required, list) or not all(isinstance(item, str) and item in result["properties"] for item in required):
            raise CustomApiError(f"{where}: required names declared properties")
        result["required"] = sorted(set(required))
        if node.get("additionalProperties") not in (None, False):
            raise CustomApiError(f"{where}: additionalProperties must be false (declare every field)")
    if kind == "array":
        result["items"] = _schema(node.get("items"), f"{where}[]", depth + 1)
    return result


def _check_value(value: Any, schema: Mapping[str, Any], where: str, depth: int = 0) -> None:
    kind = schema["type"]
    if kind == "string" and (not isinstance(value, str) or len(value) > schema["maxLength"]):
        raise CustomApiError(f"{where} must be a string of at most {schema['maxLength']} characters")
    if kind == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        raise CustomApiError(f"{where} must be an integer")
    if kind == "number" and (isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (float("inf"), float("-inf"))):
        raise CustomApiError(f"{where} must be a finite number")
    if kind == "boolean" and not isinstance(value, bool):
        raise CustomApiError(f"{where} must be true or false")
    if kind in ("integer", "number"):
        if "minimum" in schema and value < schema["minimum"] or "maximum" in schema and value > schema["maximum"]:
            raise CustomApiError(f"{where} is outside its allowed range")
    if "enum" in schema and value not in schema["enum"]:
        raise CustomApiError(f"{where} must be one of the declared values")
    if kind == "object":
        if not isinstance(value, Mapping):
            raise CustomApiError(f"{where} must be an object")
        unknown = sorted(set(value) - set(schema["properties"]))
        if unknown:
            raise CustomApiError(f"{where} has undeclared fields: {', '.join(str(item) for item in unknown)}")
        missing = [name for name in schema["required"] if name not in value]
        if missing:
            raise CustomApiError(f"{where} is missing: {', '.join(missing)}")
        for name, item in value.items():
            _check_value(item, schema["properties"][name], f"{where}.{name}", depth + 1)
    if kind == "array":
        if not isinstance(value, list) or len(value) > 100:
            raise CustomApiError(f"{where} must be a list of at most 100 items")
        for index, item in enumerate(value):
            _check_value(item, schema["items"], f"{where}[{index}]", depth + 1)


# -- definitions --------------------------------------------------------------------------


def validate_definition(definition: Any) -> dict[str, Any]:
    """The normalised definition, or ``CustomApiError``. It holds no secret, only a ``secret://`` reference."""
    if not isinstance(definition, Mapping):
        raise CustomApiError("a definition is an object")
    extra = sorted(set(definition) - {"name", "baseUrl", "auth", "operations", "owner"})
    if extra:
        raise CustomApiError("unsupported keys: " + ", ".join(extra))
    name = definition.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise CustomApiError("name must be lowercase letters, digits and hyphens")
    try:
        base = _guard().parse_endpoint(definition.get("baseUrl"))
    except ValueError as exc:
        raise CustomApiError(f"baseUrl is not an acceptable public endpoint: {exc}") from exc
    base_path = base["path"].rstrip("/")
    owner = definition.get("owner")
    if owner is not None and (not isinstance(owner, str) or not _PERSON.match(owner)):
        raise CustomApiError("owner must be a named person")
    auth = definition.get("auth", {"type": "none"})
    if not isinstance(auth, Mapping) or auth.get("type") not in AUTH_TYPES or set(auth) - {"type", "credentialRef", "header"}:
        raise CustomApiError("auth is {type: bearer|header|basic|none, credentialRef: secret://…, header: …}")
    normal_auth: dict[str, Any] = {"type": auth["type"]}
    if auth["type"] == "none":
        if "credentialRef" in auth or "header" in auth:
            raise CustomApiError("auth type none takes no credential")
    else:
        if not isinstance(auth.get("credentialRef"), str) or not _SECRET_REF.match(auth["credentialRef"]):
            raise CustomApiError("the credential must be a secret:// reference, never the value")
        normal_auth["credentialRef"] = auth["credentialRef"]
        if auth["type"] == "header":
            header = auth.get("header")
            if not isinstance(header, str) or not _HEADER.match(header) or header.lower() in _RESERVED_HEADERS or header.lower().startswith(("proxy-", "sec-")):
                raise CustomApiError("an API-key header needs a plain header name that is not a reserved one")
            normal_auth["header"] = header
        elif "header" in auth:
            raise CustomApiError("only the header auth type names a header")
    operations = definition.get("operations")
    if not isinstance(operations, Mapping) or not 1 <= len(operations) <= MAX_OPERATIONS:
        raise CustomApiError(f"declare 1 to {MAX_OPERATIONS} operations; there is no 'any request'")
    normal_ops: dict[str, dict[str, Any]] = {}
    for op_id, entry in operations.items():
        if not isinstance(op_id, str) or not _OP.match(op_id):
            raise CustomApiError(f"operation id {op_id!r} is not a plain lowercase identifier")
        normal_ops[op_id] = _operation(op_id, entry)
    return {"name": name, "baseUrl": f"https://{base['host']}{'' if base['port'] == 443 else ':' + str(base['port'])}{base_path}", "auth": normal_auth,
            "operations": normal_ops, **({"owner": owner} if owner else {})}


def _operation(op_id: str, entry: Any) -> dict[str, Any]:
    if not isinstance(entry, Mapping):
        raise CustomApiError(f"{op_id}: an operation is an object")
    extra = sorted(set(entry) - {"method", "path", "summary", "parameters", "body", "risk"})
    if extra:
        raise CustomApiError(f"{op_id}: unsupported keys: {', '.join(extra)}")
    method = entry.get("method")
    if method not in METHOD_RISK:
        raise CustomApiError(f"{op_id}: method must be one of {', '.join(METHOD_RISK)}")
    path = entry.get("path")
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 300 or "?" in path or "#" in path:
        raise CustomApiError(f"{op_id}: path starts with / and has no query or fragment")
    segments = [segment for segment in path.split("/")[1:] if segment != ""]
    if "" in path.split("/")[1:-1] or not all(_SEGMENT.match(segment) and segment not in (".", "..") for segment in segments):
        raise CustomApiError(f"{op_id}: path segments are plain or {{parameter}} placeholders")
    placeholders = [segment[1:-1] for segment in segments if segment.startswith("{")]
    if len(set(placeholders)) != len(placeholders):
        raise CustomApiError(f"{op_id}: a path parameter appears twice")
    summary = entry.get("summary", "")
    if not isinstance(summary, str) or len(summary) > 300:
        raise CustomApiError(f"{op_id}: summary is at most 300 characters")
    parameters = entry.get("parameters", {})
    if not isinstance(parameters, Mapping) or len(parameters) > MAX_PARAMETERS:
        raise CustomApiError(f"{op_id}: at most {MAX_PARAMETERS} parameters")
    normal_parameters: dict[str, dict[str, Any]] = {}
    for name, spec in parameters.items():
        if not isinstance(name, str) or not _PARAM.match(name) or name == "body" or not isinstance(spec, Mapping) or set(spec) - {"in", "required", "schema"}:
            raise CustomApiError(f"{op_id}: parameter {name!r} is {{in: path|query, required, schema}}")
        location = spec.get("in")
        if location not in ("path", "query"):
            raise CustomApiError(f"{op_id}.{name}: a parameter is in the path or the query (headers and cookies are not accepted)")
        schema = _schema(spec.get("schema"), f"{op_id}.{name}")
        if schema["type"] in ("object", "array"):
            raise CustomApiError(f"{op_id}.{name}: path and query parameters are scalar")
        if location == "path" and (name not in placeholders or spec.get("required") is not True):
            raise CustomApiError(f"{op_id}.{name}: a path parameter appears in the path and is required")
        normal_parameters[name] = {"in": location, "required": bool(spec.get("required", False)), "schema": schema}
    if set(placeholders) - set(normal_parameters):
        raise CustomApiError(f"{op_id}: declare every path placeholder as a parameter")
    body = entry.get("body")
    normal_body = None
    if body is not None:
        if method in ("GET", "DELETE"):
            raise CustomApiError(f"{op_id}: {method} takes no body")
        normal_body = _schema(body, f"{op_id}.body")
        if normal_body["type"] != "object":
            raise CustomApiError(f"{op_id}: a request body is a JSON object")
    floor = METHOD_RISK[method]
    declared = entry.get("risk", floor)
    if declared not in RISK_ORDER or RISK_ORDER.index(declared) < RISK_ORDER.index(floor):
        raise CustomApiError(f"{op_id}: risk cannot be lower than {floor} for {method}")
    return {"method": method, "path": path, "summary": summary, "parameters": normal_parameters, "risk": declared, **({"body": normal_body} if normal_body else {})}


def operation_tools(definition: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each operation as an ``mcp_passthrough`` pinned tool, so approval pins method, path, summary and every schema.

    ``inputSchema`` is a plain JSON Schema of the arguments (each parameter, plus ``body`` when the operation takes one), so the
    governed executor validates a call exactly as it does an MCP tool's; the ``x-`` annotations pin method, path, base URL, risk,
    authentication type and where each parameter goes.
    """
    normal = validate_definition(definition)
    tools = []
    for op_id, operation in sorted(normal["operations"].items()):
        properties = {name: spec["schema"] for name, spec in operation["parameters"].items()}
        required = [name for name, spec in operation["parameters"].items() if spec["required"]]
        if operation.get("body") is not None:
            properties["body"] = operation["body"]
            required.append("body")
        schema = {"type": "object", "properties": properties, "required": sorted(required), "additionalProperties": False,
                  "x-method": operation["method"], "x-path": operation["path"], "x-base": normal["baseUrl"], "x-risk": operation["risk"],
                  "x-auth": normal["auth"]["type"], "x-in": {name: spec["in"] for name, spec in operation["parameters"].items()}}
        tools.append({"name": op_id, "description": operation["summary"], "inputSchema": schema})
    return tools


def review(definition: Mapping[str, Any]) -> dict[str, Any]:
    """Poisoning findings for every operation's summary and schema; ``blocking`` operations must not be approved."""
    results = {tool["name"]: _review().review(tool) for tool in operation_tools(definition)}
    return {"operations": results, "blocking": sorted(name for name, item in results.items() if item["blocking"])}


def exposed(definition: Mapping[str, Any], approvals: Any) -> dict[str, Any]:
    """Operations that may be offered now: approved at their current pin and with no blocking finding."""
    normal = validate_definition(definition)
    visible, hidden = [], []
    for tool in operation_tools(normal):
        op_id = tool["name"]
        findings = _review().blocking_codes(_review().review(tool))
        if findings:
            hidden.append({"operation": op_id, "reason": "unsafe-metadata", "findings": findings})
            continue
        state = approvals.status(normal["name"], tool)
        if state != "approved":
            hidden.append({"operation": op_id, "reason": state})
            continue
        operation = normal["operations"][op_id]
        visible.append({"operation": op_id, "tool": f"api.{normal['name']}.{op_id}", "risk": operation["risk"], "approvalRequired": operation["risk"] != "read"})
    return {"api": normal["name"], "exposed": visible, "hidden": hidden}


def build_request(definition: Mapping[str, Any], op_id: str, arguments: Mapping[str, Any], approvals: Any) -> dict[str, Any]:
    """The checked request for one approved operation: method, URL, query and JSON body. It carries no credential."""
    normal = validate_definition(definition)
    operation = normal["operations"].get(op_id) if isinstance(op_id, str) else None
    if operation is None:
        raise CustomApiError("that operation is not declared")
    tool = next(item for item in operation_tools(normal) if item["name"] == op_id)
    if _review().review(tool)["blocking"]:
        raise CustomApiError("that operation's metadata is unsafe and it is not offered")
    if approvals.status(normal["name"], tool) != "approved":
        raise CustomApiError("that operation is not approved at its current definition; a person must review it")
    if not isinstance(arguments, Mapping):
        raise CustomApiError("arguments are an object")
    arguments = dict(arguments)
    body_value = arguments.pop("body", None)  # "body" is reserved for the request body; no parameter may use the name
    unknown = sorted(set(arguments) - set(operation["parameters"]))
    if unknown:
        raise CustomApiError("undeclared arguments: " + ", ".join(unknown))
    path = operation["path"]
    query: list[tuple[str, str]] = []
    for name, spec in operation["parameters"].items():
        if name not in arguments:
            if spec["required"]:
                raise CustomApiError(f"{name} is required")
            continue
        value = arguments[name]
        _check_value(value, spec["schema"], name)
        text = ("true" if value else "false") if isinstance(value, bool) else str(value)
        if spec["in"] == "path":
            if not _IDENTIFIER.match(text) or text in (".", ".."):
                raise CustomApiError(f"{name} must be a plain identifier (letters, digits, . _ ~ -)")
            path = path.replace("{" + name + "}", quote(text, safe=""))
        else:
            query.append((name, text))
    request: dict[str, Any] = {"method": operation["method"], "url": normal["baseUrl"] + path, "limits": dict(LIMITS), "risk": operation["risk"],
                               "approvalRequired": operation["risk"] != "read", "auth": normal["auth"]["type"]}
    if query:
        request["url"] += "?" + urlencode(sorted(query))
    if operation.get("body") is not None:
        if body_value is None:
            raise CustomApiError("this operation needs a body")
        _check_value(body_value, operation["body"], "body")
        encoded = json.dumps(body_value, separators=(",", ":"), sort_keys=True)
        if len(encoded.encode("utf-8")) > LIMITS["maxRequestBytes"]:
            raise CustomApiError("the request body is too large")
        request["body"] = body_value
    elif body_value is not None:
        raise CustomApiError("this operation takes no body")
    return request


# -- OpenAPI import -----------------------------------------------------------------------


def _resolve(node: Any, root: Mapping[str, Any], depth: int = 0, seen: tuple[str, ...] = ()) -> Any:
    if isinstance(node, Mapping) and "$ref" in node:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/") or depth >= MAX_REF_DEPTH or ref in seen:
            raise CustomApiError("only local, non-circular # references up to %d deep are accepted" % MAX_REF_DEPTH)
        target: Any = root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, Mapping) or part not in target:
                raise CustomApiError("a reference points at nothing")
            target = target[part]
        return _resolve(target, root, depth + 1, (*seen, ref))
    if isinstance(node, Mapping):
        return {key: _resolve(value, root, depth, seen) for key, value in node.items()}
    if isinstance(node, list):
        return [_resolve(value, root, depth, seen) for value in node]
    return node


def _plain_schema(node: Any) -> dict[str, Any]:
    """Keep only the schema subset this module understands (the rest of an OpenAPI schema is dropped, not trusted)."""
    if not isinstance(node, Mapping):
        raise CustomApiError("a schema is an object")
    keep = {key: node[key] for key in ("type", "description", "enum", "maxLength", "minimum", "maximum", "required") if key in node}
    if node.get("type") == "object":
        keep["properties"] = {name: _plain_schema(sub) for name, sub in (node.get("properties") or {}).items()}
    if node.get("type") == "array":
        keep["items"] = _plain_schema(node.get("items"))
    return keep


def import_openapi(document: Any, *, name: str, select: Mapping[str, str], base_url: str | None = None, auth: Mapping[str, Any] | None = None, owner: str | None = None) -> dict[str, Any]:
    """A *proposed* definition for only the selected operations (``{operationId: our-operation-id}``) of an OpenAPI 3 document."""
    if not isinstance(document, Mapping) or not str(document.get("openapi", "")).startswith("3."):
        raise CustomApiError("an OpenAPI 3 document is expected")
    if not isinstance(select, Mapping) or not 1 <= len(select) <= MAX_OPERATIONS:
        raise CustomApiError("select the operation ids to import; nothing is imported by default")
    if base_url is None:
        servers = document.get("servers")
        base_url = servers[0].get("url") if isinstance(servers, list) and servers and isinstance(servers[0], Mapping) else None
    operations: dict[str, Any] = {}
    wanted = dict(select)
    for path, item in (document.get("paths") or {}).items():
        if not isinstance(item, Mapping):
            continue
        shared = item.get("parameters", [])
        for method in ("get", "post", "put", "patch", "delete"):
            operation = item.get(method)
            if not isinstance(operation, Mapping) or operation.get("operationId") not in wanted:
                continue
            our_id = wanted.pop(operation["operationId"])
            parameters: dict[str, Any] = {}
            for raw in [*(shared if isinstance(shared, list) else []), *(operation.get("parameters") or [])]:
                parameter = _resolve(raw, document)
                if not isinstance(parameter, Mapping) or parameter.get("in") not in ("path", "query"):
                    raise CustomApiError(f"{our_id}: only path and query parameters are imported (no headers or cookies)")
                parameters[parameter.get("name")] = {"in": parameter["in"], "required": bool(parameter.get("required", False)), "schema": _plain_schema(_resolve(parameter.get("schema", {"type": "string"}), document))}
            entry: dict[str, Any] = {"method": method.upper(), "path": path, "summary": str(operation.get("summary") or operation.get("description") or "")[:300], "parameters": parameters}
            request_body = _resolve(operation.get("requestBody"), document) if operation.get("requestBody") else None
            if request_body:
                content = request_body.get("content", {}) if isinstance(request_body, Mapping) else {}
                if set(content) - {"application/json"} or "application/json" not in content:
                    raise CustomApiError(f"{our_id}: only application/json request bodies are imported")
                schema = _plain_schema(_resolve(content["application/json"].get("schema", {}), document))
                schema.setdefault("type", "object")
                entry["body"] = schema
            operations[our_id] = entry
    if wanted:
        raise CustomApiError("these operation ids were not found: " + ", ".join(sorted(wanted)))
    proposal = {"name": name, "baseUrl": base_url, "auth": dict(auth) if auth else {"type": "none"}, "operations": operations, **({"owner": owner} if owner else {})}
    # Validate so a proposal is always well formed; the plain schema subset above is re-checked strictly here.
    return validate_definition(copy.deepcopy(_with_object_defaults(proposal)))


def _with_object_defaults(proposal: dict[str, Any]) -> dict[str, Any]:
    def fix(schema: dict[str, Any]) -> None:
        if schema.get("type") == "object":
            schema.setdefault("properties", {})
            schema.setdefault("required", [item for item in schema.get("required", []) if item in schema["properties"]])
            for sub in schema["properties"].values():
                fix(sub)
        if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
            fix(schema["items"])

    for operation in proposal["operations"].values():
        for spec in operation.get("parameters", {}).values():
            fix(spec["schema"])
        if "body" in operation:
            fix(operation["body"])
    return proposal

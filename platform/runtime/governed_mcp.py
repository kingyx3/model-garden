"""MCP bridge from Hermes to Model Garden governed business Tools.

The server is intentionally narrow. Hermes discovers this service through its standard
MCP client; every call is still authorized against the compiled single-Agent desired
state before a connector can execute. Connector credentials are injected only into this
service, never into the Hermes/model process.

MVP supported Tools:
- calendar.availability: Google Calendar free/busy read
- calendar.book: Google Calendar event creation, with exact-action approval when required
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import re
from typing import Any, Mapping

from mcp.server.fastmcp import FastMCP

ROOT = pathlib.Path(__file__).resolve().parents[2]
REQUEST_ID = re.compile(r"^[0-9a-f]{64}$")


def _load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        raise RuntimeError(f"cannot load Model Garden runtime module {path}")
    spec.loader.exec_module(module)
    return module


GOVERN = _load_module("model_garden_govern_action", ROOT / "scripts" / "govern-action.py")
CALENDAR = _load_module("model_garden_google_calendar", ROOT / "platform" / "connectors" / "google_calendar.py")


def _load_json_object(path: pathlib.Path, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _atomic_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _request_path(approval_root: pathlib.Path, state: str, request_id: str) -> pathlib.Path:
    if not REQUEST_ID.fullmatch(request_id):
        raise ValueError("invalid governed-action request id")
    return approval_root / state / f"{request_id}.json"


class GovernedToolRuntime:
    """Execute the small supported Tool set through Model Garden governance."""

    def __init__(
        self,
        desired_state_path: pathlib.Path,
        audit_path: pathlib.Path,
        approval_root: pathlib.Path,
        *,
        environ: Mapping[str, str] | None = None,
        calendar_transport=None,
    ) -> None:
        self.desired_state = _load_json_object(desired_state_path, "compiled desired state")
        if self.desired_state.get("schemaVersion") != 1:
            raise ValueError("compiled desired state schemaVersion must be 1")
        self.audit_path = audit_path
        self.approval_root = approval_root
        self.environ = dict(os.environ if environ is None else environ)
        self.calendar_transport = calendar_transport

    def _identity(self) -> str:
        identity = self.environ.get("MODEL_GARDEN_INITIATING_IDENTITY", "runtime:hermes").strip()
        return identity or "runtime:hermes"

    def _calendar(self):
        token = self.environ.get("MODEL_GARDEN_GOOGLE_CALENDAR_TOKEN")
        if not token:
            raise RuntimeError("Google Calendar credential is not configured for the governed Tool service")
        calendar_id = self.environ.get("MODEL_GARDEN_GOOGLE_CALENDAR_ID", "primary")
        return CALENDAR.GoogleCalendarClient(
            token,
            calendar_id=calendar_id,
            transport=self.calendar_transport,
        )

    def _approval(self, request_id: str) -> dict[str, Any] | None:
        path = _request_path(self.approval_root, "decisions", request_id)
        if not path.is_file():
            return None
        decision = _load_json_object(path, "approval decision")
        if decision.get("requestId") != request_id:
            raise ValueError("approval decision requestId does not match its filename")
        return decision

    def _pending(self, request: dict[str, Any]) -> None:
        _atomic_json(_request_path(self.approval_root, "pending", request["requestId"]), request)

    def _clear_pending(self, request_id: str) -> None:
        path = _request_path(self.approval_root, "pending", request_id)
        if path.exists():
            path.unlink()

    def _execute(self, tool_name: str, arguments: dict[str, Any], executor) -> dict[str, Any]:
        request = GOVERN.build_action_request(
            self.desired_state,
            tool_name,
            arguments,
            initiating_user=self._identity(),
        )
        approval = self._approval(request["requestId"])
        try:
            result = GOVERN.execute_action(
                self.desired_state,
                tool_name,
                arguments,
                executor,
                approval=approval,
                initiating_user=self._identity(),
                audit_path=self.audit_path,
            )
        except PermissionError:
            if approval is not None:
                self._clear_pending(request["requestId"])
            raise
        if result.get("status") == "pending-approval":
            self._pending(result["request"])
        else:
            self._clear_pending(request["requestId"])
        return result

    def calendar_availability(self, *, start: str, end: str, timezone: str | None = None) -> dict[str, Any]:
        arguments: dict[str, Any] = {"start": start, "end": end}
        if timezone is not None:
            arguments["timezone"] = timezone
        client = self._calendar()
        return self._execute(
            "calendar.availability",
            arguments,
            lambda exact: client.check_availability(
                start=exact["start"],
                end=exact["end"],
                timezone=exact.get("timezone"),
            ),
        )

    def calendar_book(
        self,
        *,
        start: str,
        end: str,
        summary: str = "Appointment",
        timezone: str | None = None,
        description: str | None = None,
        attendees: list[str] | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {"start": start, "end": end, "summary": summary}
        if timezone is not None:
            arguments["timezone"] = timezone
        if description is not None:
            arguments["description"] = description
        if attendees is not None:
            arguments["attendees"] = attendees
        client = self._calendar()
        return self._execute("calendar.book", arguments, client.booking_executor())


def build_server(runtime: GovernedToolRuntime, *, host: str = "0.0.0.0", port: int = 9120) -> FastMCP:
    mcp = FastMCP(
        "Model Garden Governed Tools",
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
    )

    @mcp.tool(name="calendar_availability")
    def calendar_availability(start: str, end: str, timezone: str | None = None) -> dict[str, Any]:
        """Check an approved calendar for free/busy state in an exact RFC3339 interval."""
        return runtime.calendar_availability(start=start, end=end, timezone=timezone)

    @mcp.tool(name="calendar_book")
    def calendar_book(
        start: str,
        end: str,
        summary: str = "Appointment",
        timezone: str | None = None,
        description: str | None = None,
        attendees: list[str] | None = None,
    ) -> dict[str, Any]:
        """Request one governed calendar booking; approval-required actions return pending first."""
        return runtime.calendar_book(
            start=start,
            end=end,
            summary=summary,
            timezone=timezone,
            description=description,
            attendees=attendees,
        )

    return mcp


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--desired-state",
        type=pathlib.Path,
        default=pathlib.Path(os.environ.get("MODEL_GARDEN_DESIRED_STATE", "/opt/profile-seed/.modelgarden/desired-state.json")),
    )
    parser.add_argument(
        "--audit-path",
        type=pathlib.Path,
        default=pathlib.Path(os.environ.get("MODEL_GARDEN_AUDIT_PATH", "/opt/data/.modelgarden/audit.jsonl")),
    )
    parser.add_argument(
        "--approval-root",
        type=pathlib.Path,
        default=pathlib.Path(os.environ.get("MODEL_GARDEN_APPROVAL_ROOT", "/opt/data/.modelgarden/approvals")),
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9120)
    args = parser.parse_args()

    runtime = GovernedToolRuntime(args.desired_state, args.audit_path, args.approval_root)
    server = build_server(runtime, host=args.host, port=args.port)
    server.run(transport="streamable-http")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

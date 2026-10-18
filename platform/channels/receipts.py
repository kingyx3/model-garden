"""Action receipts: one message in the thread for every material action the coworker ran, with its ledger reference (#82, #83, #84).

The channel adapters can all ``send_receipt``, but nothing told a person that an approved action had actually run. This
reads the Action Ledger and turns each *execution* of a write, privileged or external-communication Tool into one receipt for
the approver conversations the gateway announces approvals to.

- **From the ledger, not the model.** A receipt is built from an ``execution`` event the governed boundary wrote, so it can only
  describe what really happened (``succeeded`` or ``failed``); nothing the coworker says creates one.
- **Identifiers only.** A receipt names the Agent, the Tool, the outcome, an opaque object reference when the ledger holds one
  and the ledger ``requestId``. It never carries arguments, results, titles or message text.
- **Material actions only.** Reads and Tools of unknown risk produce nothing; so do other Agents' events when several share a
  ledger (``agent`` names the one this feed speaks for).
- **Once each, in order, and retried.** The feed remembers how far into the ledger it has delivered (a byte offset, written
  atomically, starting at the end of the ledger so history is never replayed). An event that could not be delivered anywhere is not
  skipped: the feed stops there and offers it again on the next pass.
- **Bounded.** At most ``MAX_BATCH`` events are read per pass, and a ledger shorter than the saved offset (rotated or replaced)
  restarts from the new end rather than reading garbage.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
from collections.abc import Callable, Mapping
from typing import Any

MATERIAL_RISKS = ("write", "privileged", "external-communication")
MAX_BATCH = 50
_REF = re.compile(r"^[A-Za-z0-9:._/#@-]{1,128}$")
_REQUEST_ID = re.compile(r"^[0-9a-f]{64}$")
SUMMARY = {"succeeded": "{agent} ran {tool}", "failed": "{agent} tried to run {tool} and it did not complete"}


def risks_of(desired_state: Mapping[str, Any]) -> dict[str, str]:
    """Tool name to risk for the Agent's compiled Tools."""
    risks: dict[str, str] = {}
    for tool in desired_state.get("tools", []) if isinstance(desired_state.get("tools"), list) else []:
        name = (tool.get("metadata") or {}).get("name") if isinstance(tool, Mapping) else None
        risk = (tool.get("spec") or {}).get("risk") if isinstance(tool, Mapping) else None
        if isinstance(name, str) and isinstance(risk, str):
            risks[name] = risk
    return risks


class ReceiptFeed:
    def __init__(self, audit_path: pathlib.Path, state_path: pathlib.Path, *, agent: str, risks: Mapping[str, str]) -> None:
        self.audit_path, self.state_path = pathlib.Path(audit_path), pathlib.Path(state_path)
        self.agent, self.risks = agent, dict(risks)

    # -- position ---------------------------------------------------------------------------------

    def _size(self) -> int:
        try:
            return self.audit_path.stat().st_size
        except FileNotFoundError:
            return 0

    def _offset(self) -> int | None:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8")).get("offset")
        except (OSError, ValueError, AttributeError):
            return None
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    def _save(self, offset: int) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.state_path.with_suffix(".tmp")
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"offset": offset}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.state_path)

    def start(self) -> None:
        """Begin at the current end of the ledger unless a position is already saved, so existing history is not replayed."""
        if self._offset() is None:
            self._save(self._size())

    # -- reading ----------------------------------------------------------------------------------

    def _receipt(self, event: Mapping[str, Any]) -> dict[str, str] | None:
        if event.get("event") != "execution" or event.get("agent") != self.agent or event.get("outcome") not in SUMMARY:
            return None
        tool, request_id = event.get("tool"), event.get("requestId")
        if self.risks.get(tool) not in MATERIAL_RISKS or not isinstance(request_id, str) or not _REQUEST_ID.match(request_id):
            return None
        reference = (event.get("ledger") or {}).get("externalObjectRef")
        summary = SUMMARY[event["outcome"]].format(agent=self.agent, tool=tool)
        if event["outcome"] == "succeeded" and isinstance(reference, str) and _REF.match(reference):
            summary += f" ({reference})"
        return {"requestId": request_id, "tool": tool, "status": event["outcome"], "summary": summary}

    def pending(self) -> list[tuple[int, dict[str, str] | None]]:
        """Complete ledger lines after the saved position as ``(offset after the line, receipt or None)``, at most ``MAX_BATCH``."""
        offset = self._offset()
        size = self._size()
        if offset is None or offset > size:
            self._save(size)
            return []
        found: list[tuple[int, dict[str, str] | None]] = []
        with open(self.audit_path, "rb") as handle:
            handle.seek(offset)
            for raw in handle:
                if not raw.endswith(b"\n"):
                    break  # a line still being written
                offset += len(raw)
                try:
                    event = json.loads(raw)
                except ValueError:
                    event = None
                found.append((offset, self._receipt(event) if isinstance(event, Mapping) else None))
                if len(found) >= MAX_BATCH:
                    break
        return found

    def deliver(self, send: Callable[[dict[str, str]], int]) -> int:
        """Offer each new receipt to ``send`` (which returns how many conversations it reached) and move on only once delivered.

        Lines that are not receipts are passed over. Stops at the first receipt that reached nobody so it is offered again next pass.
        Returns the number of receipts delivered.
        """
        delivered = 0
        batch = self.pending()  # may reset a stale position, so read the position after it
        position = self._offset()
        for after, receipt in batch:
            if receipt is not None:
                if send(receipt) < 1:
                    break
                delivered += 1
            position = after
        if position is not None and position != self._offset():
            self._save(position)
        return delivered

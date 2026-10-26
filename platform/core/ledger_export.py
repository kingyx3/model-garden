"""Export the local action-ledger chain to a separately administered sink and verify it.

The governed sidecar's local JSONL chain is tamper-evident but a host administrator can
replace the whole file. This module copies each chained record, plus periodic head hashes,
to a sink that the runtime identity cannot rewrite, and proves later that the export and
the local file agree and that the local file has not been truncated.

Properties:
- records are exported at-least-once; the record ID is the event's own SHA-256 chain hash,
  so a repeated delivery is a harmless duplicate and a different record under one ID is an error;
- the exporter only advances its cursor after the sink acknowledges a batch;
- the verifier recomputes the chain from the exported records alone, reports gaps in the
  exported sequence, compares against the local chain and detects local truncation or divergence;
- sinks never receive anything the local ledger did not already hold (arguments are already
  redacted there).

Sinks provided: ``DirectorySink`` (an append-only layout, intended to be a mount whose write
policy is controlled by a different identity than the runtime) and ``WebhookSink`` (generic
SIEM/HTTP collector; write-only, so it cannot be read back by ``verify``) and ``GcsSink`` (a Google
Cloud Storage bucket, written create-only; the reference target is the retention-locked bucket and the
separate export identity that ``infra/docker-host/gcp`` creates).
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from bisect import bisect_right
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any

EXPORT_SCHEMA_VERSION = 1
CHAINED_SCHEMA_VERSIONS = (2, 3)
DEFAULT_BATCH = 100
_SEQUENCE_WIDTH = 12


class LedgerExportError(RuntimeError):
    """Raised when the local ledger cannot be exported safely."""


class LedgerTruncated(LedgerExportError):
    """The local log is shorter than, or diverges from, what was already exported."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_hash(event: Mapping[str, Any]) -> str:
    """The chain hash the governed boundary stores in ``eventHash`` (SHA-256 of the rest)."""
    material = {key: value for key, value in event.items() if key != "eventHash"}
    return hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()


def read_chained_events(path: pathlib.Path) -> list[dict[str, Any]]:
    """Read and verify the chained (schema v2/v3) events of a local ledger.

    Legacy unchained v1 records may precede the chain and are skipped; they have no record ID.
    Any break in sequence, linkage or hash raises ``LedgerExportError``.
    """
    events: list[dict[str, Any]] = []
    if not path.exists():
        return events
    previous: str | None = None
    records = 0  # sequence numbers count every record, legacy ones included
    last_version = 1
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise LedgerExportError(f"local ledger line {line_number} is not valid JSON") from exc
            if not isinstance(event, dict):
                raise LedgerExportError(f"local ledger line {line_number} is not an object")
            records += 1
            schema_version = event.get("schemaVersion", 1)
            if type(schema_version) is not int or schema_version not in (1, *CHAINED_SCHEMA_VERSIONS):
                raise LedgerExportError(f"local ledger line {line_number}: unsupported schemaVersion {schema_version!r}")
            if schema_version < last_version:
                raise LedgerExportError(f"local ledger line {line_number}: schemaVersion moved backwards")
            last_version = schema_version
            if schema_version == 1:
                previous = hashlib.sha256(_canonical(event).encode("utf-8")).hexdigest()
                continue
            if event.get("sequence") != records:
                raise LedgerExportError(f"local ledger line {line_number}: sequence mismatch")
            if event.get("previousEventHash") != previous:
                raise LedgerExportError(f"local ledger line {line_number}: previous hash mismatch")
            if event.get("eventHash") != event_hash(event):
                raise LedgerExportError(f"local ledger line {line_number}: event hash mismatch")
            previous = event["eventHash"]
            events.append(event)
    return events


def make_record(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "exportSchema": EXPORT_SCHEMA_VERSION,
        "recordId": event["eventHash"],
        "sequence": event["sequence"],
        "event": dict(event),
    }


def make_head(events: list[dict[str, Any]], *, exported_at: int | None = None) -> dict[str, Any]:
    last = events[-1]
    return {
        "exportSchema": EXPORT_SCHEMA_VERSION,
        "kind": "head",
        "sequence": last["sequence"],
        "headHash": last["eventHash"],
        "events": len(events),
        "exportedAt": int(time.time()) if exported_at is None else exported_at,
    }


class DirectorySink:
    """Append-only directory layout: one immutable file per record and per head manifest.

    Files are created exclusively and never rewritten. Re-delivering identical content is a
    no-op; different content under an existing name raises. Point ``root`` at a mount whose
    write policy the runtime and deployment identities cannot change.
    """

    def __init__(self, root: pathlib.Path) -> None:
        self.root = pathlib.Path(root)

    def _write_once(self, path: pathlib.Path, document: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        text = _canonical(document) + "\n"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if path.read_text(encoding="utf-8") != text:
                raise LedgerExportError(f"sink already holds different content at {path.name}") from None
            return
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

    def put_records(self, records: Iterable[Mapping[str, Any]]) -> None:
        for record in records:
            name = f"{record['sequence']:0{_SEQUENCE_WIDTH}d}-{record['recordId'][:16]}.json"
            self._write_once(self.root / "records" / name, record)

    def put_head(self, head: Mapping[str, Any]) -> None:
        name = f"{head['sequence']:0{_SEQUENCE_WIDTH}d}-{head['exportedAt']}-{head['headHash'][:16]}.json"
        self._write_once(self.root / "heads" / name, head)

    def records(self, after: int = 0) -> list[dict[str, Any]]:
        folder = self.root / "records"
        paths = sorted(path for path in folder.glob("*.json") if int(path.name.split("-", 1)[0]) > after) if folder.is_dir() else []
        return [json.loads(path.read_text(encoding="utf-8")) for path in paths]

    def heads(self) -> list[dict[str, Any]]:
        folder = self.root / "heads"
        return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(folder.glob("*.json"))] if folder.is_dir() else []


Transport = Callable[[urllib.request.Request], Any]


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Do not forward audit payloads, bearer tokens or metadata headers to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _private_urlopen(request: urllib.request.Request, *, timeout: int) -> Any:
    """Use verified TLS directly: no environment proxies or automatic redirects.

    The default urllib opener inherits HTTP(S)_PROXY and follows 30x responses.
    Both are inappropriate for an audit export containing sensitive chain records
    and scoped credentials. Callers handle redirect HTTPError as a failed delivery.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectRedirects())
    return opener.open(request, timeout=timeout)


def _urlopen(request: urllib.request.Request) -> Any:
    with _private_urlopen(request, timeout=15) as response:
        return response.status


class WebhookSink:
    """POST batches to an HTTPS collector (SIEM or relay). Write-only: ``verify`` cannot read it back.

    Each batch carries an ``Idempotency-Key`` derived from its record IDs so a retry is safe;
    a non-2xx response or network error raises so the cursor does not advance.
    """

    def __init__(self, url: str, *, token: str | None = None, transport: Transport | None = None) -> None:
        if not url.startswith("https://"):
            raise ValueError("webhook sink requires an https URL")
        self.url = url
        self.token = token
        self.transport = transport or _urlopen

    def _post(self, payload: Mapping[str, Any], key: str) -> None:
        headers = {"Content-Type": "application/json", "Idempotency-Key": key}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.url, data=_canonical(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            status = self.transport(request)
        except (urllib.error.URLError, OSError) as exc:
            raise LedgerExportError(f"webhook delivery failed: {type(exc).__name__}") from exc
        if not isinstance(status, int) or not 200 <= status < 300:
            raise LedgerExportError(f"webhook delivery rejected with status {status!r}")

    def put_records(self, records: Iterable[Mapping[str, Any]]) -> None:
        batch = list(records)
        if batch:
            key = hashlib.sha256("".join(record["recordId"] for record in batch).encode("utf-8")).hexdigest()
            self._post({"kind": "records", "records": batch}, key)

    def put_head(self, head: Mapping[str, Any]) -> None:
        self._post({"kind": "head", "head": dict(head)}, f"head-{head['sequence']}-{head['headHash']}")


HttpCall = Callable[[str, str, Mapping[str, str], bytes | None], tuple[int, bytes]]
_GCS_HOSTS = ("https://storage.googleapis.com/", "https://iamcredentials.googleapis.com/")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$")
_SERVICE_ACCOUNT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$")


def _http(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> tuple[int, bytes]:
    if not url.startswith(_GCS_HOSTS + ("http://metadata.google.internal/",)):
        raise LedgerExportError("the GCS sink only talks to Google Cloud endpoints")
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with _private_urlopen(request, timeout=20) as response:
            return response.status, response.read(16 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def metadata_token(http: HttpCall | None = None) -> Callable[[], str]:
    """Access tokens for the VM's own service account, from the metadata server (nothing is stored)."""
    call = http or _http

    def token() -> str:
        status, raw = call("GET", "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
                           {"Metadata-Flavor": "Google"}, None)
        if status != 200:
            raise LedgerExportError(f"the metadata server refused a token (HTTP {status})")
        return json.loads(raw)["access_token"]

    return token


def impersonated_token(service_account: str, base: Callable[[], str], *, scope: str, http: HttpCall | None = None) -> Callable[[], str]:
    """Access tokens for a separate service account, minted by the caller's identity (it needs Token Creator on that account only)."""
    if not _SERVICE_ACCOUNT.match(service_account):
        raise ValueError("the export identity must be a service account email")
    call = http or _http

    def token() -> str:
        url = f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{service_account}:generateAccessToken"
        status, raw = call("POST", url, {"Authorization": f"Bearer {base()}", "Content-Type": "application/json"},
                           json.dumps({"scope": [scope]}).encode("utf-8"))
        if status != 200:
            raise LedgerExportError(f"the export identity could not be impersonated (HTTP {status})")
        return json.loads(raw)["accessToken"]

    return token


class GcsSink:
    """A Google Cloud Storage bucket as the export target: create-only writes, readable by a separate verifier identity.

    Objects use the directory layout (``records/<seq>-<id>.json``, ``heads/...``) and are written with ``ifGenerationMatch=0``,
    so an existing object is never replaced; an object that already exists counts as delivered, which keeps retries and
    at-least-once delivery harmless. The export identity needs only object-create on the bucket, and a locked retention policy on
    the bucket (see ``infra/docker-host/gcp``) is what stops anyone, including the runtime, from deleting or shortening it. The
    exporter cannot read back, so a record that was already there under a different content is caught by ``verify``, run by the
    verifier identity, not at write time. Tokens come from ``token`` (a callable) and are never stored or logged.
    """

    def __init__(self, bucket: str, token: Callable[[], str], *, prefix: str = "", http: HttpCall | None = None) -> None:
        if not _BUCKET.match(bucket):
            raise ValueError("a GCS bucket name is 3-222 lowercase letters, digits, dots, hyphens or underscores")
        clean = prefix.strip("/")
        if clean and (not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", clean) or any(part in ("", ".", "..") for part in clean.split("/"))):
            raise ValueError("the sink prefix may use letters, digits, dots, hyphens, underscores and slashes only")
        self.bucket, self.prefix, self._token, self._http = bucket, clean, token, http or _http

    def _name(self, kind: str, name: str) -> str:
        return "/".join(part for part in (self.prefix, kind, name) if part)

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}", **(extra or {})}

    def _put(self, object_name: str, document: Mapping[str, Any]) -> None:
        url = (f"https://storage.googleapis.com/upload/storage/v1/b/{self.bucket}/o?uploadType=media&ifGenerationMatch=0&name="
               + urllib.parse.quote(object_name, safe=""))
        try:
            status, _ = self._http("POST", url, self._headers({"Content-Type": "application/json"}), (_canonical(document) + "\n").encode("utf-8"))
        except (urllib.error.URLError, OSError) as exc:
            raise LedgerExportError(f"GCS delivery failed: {type(exc).__name__}") from exc
        if status == 412:  # already stored: a retry, or a duplicate delivery
            return
        if not 200 <= status < 300:
            raise LedgerExportError(f"GCS delivery rejected with status {status}")

    def put_records(self, records: Iterable[Mapping[str, Any]]) -> None:
        for record in records:
            self._put(self._name("records", f"{record['sequence']:0{_SEQUENCE_WIDTH}d}-{record['recordId'][:16]}.json"), record)

    def put_head(self, head: Mapping[str, Any]) -> None:
        self._put(self._name("heads", f"{head['sequence']:0{_SEQUENCE_WIDTH}d}-{head['exportedAt']}-{head['headHash'][:16]}.json"), head)

    def _list(self, kind: str, start: str = "") -> list[str]:
        names: list[str] = []
        token = ""
        # A GCS prefix is not a directory: "records" also matches "records-shadow".
        # The slash must be part of the server-side prefix and start offset.
        folder = self._name(kind, "") + "/"
        while True:
            query = {"prefix": folder}
            if start:
                query["startOffset"] = folder + start
            if token:
                query["pageToken"] = token
            url = f"https://storage.googleapis.com/storage/v1/b/{self.bucket}/o?" + urllib.parse.urlencode(query)
            status, raw = self._http("GET", url, self._headers(), None)
            if status != 200:
                raise LedgerExportError(f"GCS listing refused with status {status}")
            page = json.loads(raw)
            for item in page.get("items", []):
                name = item.get("name") if isinstance(item, dict) else None
                if not isinstance(name, str) or not name.startswith(folder):
                    raise LedgerExportError("GCS listing returned an object outside the requested ledger namespace")
                names.append(name)
            token = page.get("nextPageToken", "")
            if not token:
                return sorted(names)

    def _read(self, kind: str, start: str = "") -> list[dict[str, Any]]:
        documents = []
        for name in self._list(kind, start):
            url = f"https://storage.googleapis.com/storage/v1/b/{self.bucket}/o/{urllib.parse.quote(name, safe='')}?alt=media"
            status, raw = self._http("GET", url, self._headers(), None)
            if status != 200:
                raise LedgerExportError(f"GCS read refused with status {status}")
            documents.append(json.loads(raw))
        return documents

    def records(self, after: int = 0) -> list[dict[str, Any]]:
        """Exported records in sequence order; ``after`` lists only those past that sequence (the billing sync's cursor)."""
        return self._read("records", f"{after + 1:0{_SEQUENCE_WIDTH}d}" if after else "")

    def heads(self) -> list[dict[str, Any]]:
        return self._read("heads")


def _load_state(path: pathlib.Path) -> dict[str, Any]:
    if not path.is_file():
        return {"sequence": 0, "hash": None}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or not isinstance(state.get("sequence"), int):
        raise LedgerExportError("export state file is invalid")
    return state


def _save_state(path: pathlib.Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(_canonical(state) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def export_ledger(
    ledger_path: pathlib.Path,
    sink: Any,
    state_path: pathlib.Path,
    *,
    batch_size: int = DEFAULT_BATCH,
    exported_at: int | None = None,
) -> dict[str, Any]:
    """Export new chained events and a head manifest; return a summary.

    Raises ``LedgerTruncated`` if the local log is shorter than what was already exported, or
    no longer contains the event the cursor remembers, instead of silently re-exporting.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    events = read_chained_events(ledger_path)
    state = _load_state(state_path)
    cursor = state["sequence"]  # sequence number of the last acknowledged record
    head = events[-1]["sequence"] if events else 0
    if cursor > head:
        raise LedgerTruncated(f"local ledger ends at sequence {head} but sequence {cursor} was already exported")
    if cursor:
        remembered = next((event for event in events if event["sequence"] == cursor), None)
        if remembered is None or remembered["eventHash"] != state.get("hash"):
            raise LedgerTruncated(f"local ledger diverges from the export at sequence {cursor}")
    pending = [event for event in events if event["sequence"] > cursor]
    sent = 0
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        sink.put_records([make_record(event) for event in batch])
        # Advance only after the sink acknowledged the batch (at-least-once on crash).
        _save_state(state_path, {"sequence": batch[-1]["sequence"], "hash": batch[-1]["eventHash"]})
        sent += len(batch)
    if events:
        sink.put_head(make_head(events, exported_at=exported_at))
    return {"exported": sent, "totalChainedEvents": len(events), "headSequence": events[-1]["sequence"] if events else 0}


def _exported_chain_problems(records: list[dict[str, Any]], first: int) -> tuple[list[str], list[int]]:
    problems: list[str] = []
    gaps: list[int] = []
    expected = first
    previous: str | None = None
    seen: set[int] = set()
    for record in records:
        sequence = record.get("sequence")
        event = record.get("event", {})
        if not isinstance(sequence, int) or sequence in seen:
            problems.append(f"exported record has an invalid or duplicate sequence {sequence!r}")
            continue
        seen.add(sequence)
        if sequence > expected:
            gaps.extend(range(expected, sequence))
            expected = sequence
            previous = None  # linkage across a gap cannot be proven
        if record.get("recordId") != event.get("eventHash") or event.get("eventHash") != event_hash(event):
            problems.append(f"exported record {sequence} fails its hash check")
        elif previous is not None and event.get("previousEventHash") != previous:
            problems.append(f"exported record {sequence} does not link to {sequence - 1}")
        previous = event.get("eventHash")
        expected = sequence + 1
    return problems, gaps


def verify_export(
    ledger_path: pathlib.Path,
    sink: DirectorySink,
    *,
    max_lag_events: int | None = None,
    max_head_age_seconds: int | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Compare a readable sink with the local ledger.

    ``ok`` is true only when the exported chain is intact and contiguous, matches the local
    chain over their common range, the local log is not shorter than the exported head, and
    every head manifest agrees with its exported record and the newest head covers the
    latest exported record (otherwise a failed head upload must be retried).

    Two optional delivery alarms catch an exporter that has quietly stopped: ``max_lag_events``
    flags a local ledger that is more than that many events ahead of the export, and
    ``max_head_age_seconds`` flags a newest head manifest older than that while the ledger holds
    events the export does not. Both are off unless set, and neither fires when the export is
    simply current, so an idle runtime does not alarm.
    """
    for label, value in (("max_lag_events", max_lag_events), ("max_head_age_seconds", max_head_age_seconds)):
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise ValueError(f"{label} must be a non-negative integer")
    problems: list[str] = []
    try:
        local = read_chained_events(ledger_path)
    except LedgerExportError as exc:
        local = []
        problems.append(f"local ledger invalid: {exc}")
    exported = sorted(sink.records(), key=lambda record: record.get("sequence", 0))
    by_sequence = {record["sequence"]: record for record in exported if isinstance(record.get("sequence"), int)}
    exported_head = max(by_sequence, default=0)
    local_head = local[-1]["sequence"] if local else 0
    if local and not exported:
        # No baseline means there is no independent chain to verify. Optional lag
        # thresholds apply only after at least one record has been exported.
        problems.append("no exported records for nonempty local ledger")
    # The chain starts at the first chained record (legacy records may precede it).
    first = local[0]["sequence"] if local else min(by_sequence, default=1)
    chain_problems, gaps = _exported_chain_problems(exported, first)
    problems.extend(chain_problems)
    if gaps:
        problems.append(f"export gap: missing sequence(s) {gaps[:20]}{'...' if len(gaps) > 20 else ''}")
    truncated = exported_head > local_head
    if truncated:
        problems.append(f"local ledger truncated: it ends at sequence {local_head} but the export holds {exported_head}")
    diverged_at = next(
        (event["sequence"] for event in local if event["sequence"] in by_sequence
         and by_sequence[event["sequence"]]["recordId"] != event["eventHash"]),
        None,
    )
    if diverged_at is not None:
        problems.append(f"local ledger differs from the export at sequence {diverged_at}")
    heads = sink.heads()
    exported_sequences = [record["sequence"] for record in exported if type(record.get("sequence")) is int]
    newest: dict[str, Any] | None = None
    for head in heads:
        if not isinstance(head, dict):
            problems.append("head manifest has invalid structure")
            continue
        sequence = head.get("sequence")
        exported_at = head.get("exportedAt")
        count = head.get("events")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            problems.append("head manifest has invalid sequence")
            continue
        if (head.get("kind") != "head" or head.get("exportSchema") != EXPORT_SCHEMA_VERSION
                or not isinstance(count, int) or isinstance(count, bool)
                or count != bisect_right(exported_sequences, sequence)):
            problems.append(f"head manifest at sequence {sequence} has invalid metadata")
        if not isinstance(exported_at, int) or isinstance(exported_at, bool) or exported_at < 0:
            problems.append(f"head manifest at sequence {sequence} has invalid timestamp")
        else:
            if newest is None or (sequence, exported_at) > (newest["sequence"], newest["exportedAt"]):
                newest = head
        record = by_sequence.get(sequence)
        if record is None or record["recordId"] != head.get("headHash"):
            problems.append(f"head manifest at sequence {sequence} does not match the exported record")
    if exported and not heads:
        problems.append("no head manifest has been exported")
    if exported and newest is not None and newest["sequence"] < exported_head:
        problems.append(
            f"head manifest is behind exported records: latest manifest sequence {newest['sequence']}, "
            f"latest exported record sequence {exported_head}"
        )
    lag = max(0, local_head - exported_head) if not truncated else 0
    if max_lag_events is not None and lag > max_lag_events:
        problems.append(f"export lag: the local ledger is {lag} event(s) ahead of the export (limit {max_lag_events})")
    head_age: int | None = None
    if newest is not None and isinstance(newest.get("exportedAt"), int):
        head_age = max(0, (int(time.time()) if now is None else now) - newest["exportedAt"])
    if max_head_age_seconds is not None and lag > 0:
        if head_age is None:
            problems.append("export stale: events are waiting and no head manifest has ever been exported")
        elif head_age > max_head_age_seconds:
            problems.append(
                f"export stale: events are waiting and the newest head manifest is {head_age} seconds old "
                f"(limit {max_head_age_seconds})"
            )
    return {
        "ok": not problems,
        "localChainedEvents": len(local),
        "exportedRecords": len(exported),
        "exportedHeadSequence": exported_head,
        "gaps": gaps,
        "truncated": truncated,
        "lagEvents": lag,
        "headAgeSeconds": head_age,
        "problems": problems,
    }


def iter_gap_sequences(sequences: Iterable[int]) -> Iterator[int]:
    """Yield missing sequence numbers between 1 and the highest present (helper for alerting)."""
    present = set(sequences)
    for number in range(1, max(present, default=0) + 1):
        if number not in present:
            yield number

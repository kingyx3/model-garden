"""Tier 0 artifact store: finished files for a client with no file system connected.

When a coworker produces a report or spreadsheet and the client has no Drive or SharePoint bound, the file has to
land somewhere a person can fetch it, and the work has to be accountable (#77, #101). This is that place. It is a
credential-free store with tests. ``docs.create`` is served from it on the governed sidecar when ``docs`` is bound to the
built-in provider (``create_document``, below); ``files.*`` is not, and it does not deliver a link into a channel.

- **One tenant per directory.** Files and the index live under ``<root>/<tenant>/`` (mode 0700, files 0600). An
  index for another tenant is refused. Names are display labels only: the file on disk is named by the artifact
  id, so a hostile name cannot walk the filesystem.
- **Bounded and typed.** Only DOCX, XLSX, PDF, PPTX, CSV, Markdown and plain text, at most 10 MB each, 2,000 per
  tenant. ``restricted`` data is refused unless the deployment's policy allows it.
- **Digest-verified.** Each artifact records a SHA-256; a read recomputes it and refuses a file that changed on disk.
- **Accountable.** Every artifact records who asked, which Agent produced it, a bounded list of source references
  (identifiers such as ``kb:rules-of-engagement#hours``, never content) and where it is stored. Creating, reading,
  deleting and expiring call the audit hook with identifiers, size, digest and sources, never the content or the name.
- **Retention.** Each artifact expires after the client's retention period (default 365 days, 1 to 3650);
  ``purge_expired`` deletes the files and records it. A person may delete what they asked for; an administrator any.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import re
import secrets
import stat
import sys
import importlib.util
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

SCHEMA_VERSION = 1
PROVIDER_ID = "built-in-artifacts"
MAX_DOCUMENT_CHARS = 200_000
MAX_BYTES = 10 * 1024 * 1024
MAX_ARTIFACTS = 2000
MAX_INDEX_BYTES = 8 * 1024 * 1024
MAX_SOURCES = 20
DEFAULT_RETENTION_DAYS = 365
MAX_RETENTION_DAYS = 3650
CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")
MEDIA_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/pdf": ".pdf",
    "text/csv": ".csv",
    "text/markdown": ".md",
    "text/plain": ".txt",
}
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_PRINCIPAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")
_AGENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/#@-]{0,199}$")
_ID = re.compile(r"^a-[0-9a-f]{12}$")
Audit = Callable[[str, str, dict[str, Any]], Any]


class ArtifactError(ValueError):
    """Raised for an invalid artifact or an operation outside the caller's rights; never carries content."""


def retention_from_environ(environ: Mapping[str, str]) -> int:
    """The reviewed client retention setting, shared by worker, gateway and governed sidecar."""
    raw = environ.get("MODEL_GARDEN_ARTIFACT_RETENTION_DAYS")
    if raw is None:
        return DEFAULT_RETENTION_DAYS
    if not isinstance(raw, str) or not re.fullmatch(r"[1-9][0-9]{0,3}", raw):
        raise ArtifactError("MODEL_GARDEN_ARTIFACT_RETENTION_DAYS must be 1 to 3650")
    days = int(raw)
    if days > MAX_RETENTION_DAYS:
        raise ArtifactError("MODEL_GARDEN_ARTIFACT_RETENTION_DAYS must be 1 to 3650")
    return days


def _principal(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _PRINCIPAL.match(value):
        raise ArtifactError(f"{label} must be a short identity")
    return value


def _display_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ArtifactError("an artifact needs a name")
    cleaned = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f/\\]+", " ", value)).strip(" .")
    if not cleaned or len(cleaned) > 120:
        raise ArtifactError("an artifact name is 1 to 120 characters")
    return cleaned


class ArtifactStore:
    def __init__(
        self,
        root: pathlib.Path,
        *,
        tenant: str,
        audit: Audit | None = None,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        allow_restricted: bool = False,
    ) -> None:
        if not isinstance(tenant, str) or not _TENANT.match(tenant):
            raise ArtifactError("tenant must be a lowercase client-environment slug such as acme-prod")
        if not isinstance(retention_days, int) or isinstance(retention_days, bool) or not 1 <= retention_days <= MAX_RETENTION_DAYS:
            raise ArtifactError(f"retention must be 1 to {MAX_RETENTION_DAYS} days")
        self.tenant = tenant
        self.directory = pathlib.Path(root) / tenant
        self.index = self.directory / "index.json"
        self._audit = audit
        self._retention_seconds = retention_days * 86400
        self._allow_restricted = bool(allow_restricted)

    @staticmethod
    def _private_path(path: pathlib.Path, *, directory: bool = False, missing: bool = False) -> os.stat_result | None:
        """Refuse linked, foreign-owned or exposed state before opening any tenant data."""
        try:
            info = path.lstat()
        except FileNotFoundError:
            if missing:
                return None
            raise ArtifactError("the artifact's file is missing") from None
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if (not expected(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
                or (not directory and info.st_nlink != 1)):
            raise ArtifactError("unsafe artifact storage path (type, owner, permissions or links)")
        return info

    @staticmethod
    def _checked_fd(descriptor: int, expected: os.stat_result) -> None:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid()
                or opened.st_mode & 0o077 or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)):
            raise ArtifactError("unsafe artifact storage path changed during open")

    def _read_payload(self, path: pathlib.Path, *, limit: int = MAX_BYTES) -> bytes:
        observed = self._private_path(path)
        assert observed is not None
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as exc:
            raise ArtifactError("unsafe artifact storage path could not be opened") from exc
        with os.fdopen(descriptor, "rb") as stream:
            self._checked_fd(stream.fileno(), observed)
            if observed.st_size > limit:
                raise ArtifactError("unsafe artifact storage file exceeds its size limit")
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise ArtifactError("unsafe artifact storage file exceeds its size limit")
            return data

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._private_path(self.directory.parent, directory=True)
        self.directory.mkdir(exist_ok=True, mode=0o700)
        self._private_path(self.directory, directory=True)
        lock_path = self.directory / ".lock"
        try:
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        except OSError as exc:
            raise ArtifactError("unsafe artifact storage lock could not be opened") from exc
        try:
            self._checked_fd(descriptor, self._private_path(lock_path))
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            state: dict[str, Any] = {"schemaVersion": SCHEMA_VERSION, "tenant": self.tenant, "artifacts": {}}
            original = self._private_path(self.index, missing=True)
            if original is not None:
                loaded = json.loads(self._read_payload(self.index, limit=MAX_INDEX_BYTES).decode("utf-8"))
                if not isinstance(loaded, dict) or loaded.get("schemaVersion") != SCHEMA_VERSION or loaded.get("tenant") != self.tenant:
                    raise ArtifactError("artifact index has an unsupported format or belongs to another tenant")
                if not isinstance(loaded.get("artifacts"), dict):
                    raise ArtifactError("artifact index has invalid entries")
                state = loaded
            before = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != before:
                current = self._private_path(self.index, missing=True)
                if (current is None) != (original is None) or (current is not None and original is not None
                        and (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino)):
                    raise ArtifactError("unsafe artifact index changed during update")
                temporary = self.directory / (".index-" + secrets.token_hex(12) + ".tmp")
                handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    with os.fdopen(handle, "w", encoding="utf-8") as stream:
                        stream.write(json.dumps(state, sort_keys=True) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, self.index)
                finally:
                    with contextlib.suppress(FileNotFoundError):
                        temporary.unlink()
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _record(self, action: str, by: str, details: dict[str, Any]) -> None:
        if self._audit is not None:
            self._audit(action, by, details)

    @staticmethod
    def _details(entry: dict[str, Any]) -> dict[str, Any]:
        return {"artifactId": entry["id"], "mediaType": entry["mediaType"], "bytes": entry["bytes"], "sha256": entry["sha256"],
                "location": entry["location"], "agent": entry["agent"], "sources": list(entry["sources"])}

    def _expires_at(self, entry: Mapping[str, Any]) -> int:
        # A shorter reviewed policy also applies to files created before the change;
        # a longer policy never extends their original expiry.
        return min(entry["expiresAt"], entry["createdAt"] + self._retention_seconds)

    def _file(self, artifact_id: str) -> pathlib.Path:
        if not _ID.match(artifact_id):
            raise ArtifactError("artifact id must look like a-0123456789ab")
        return self.directory / artifact_id

    # -- writes -------------------------------------------------------------------------

    def put(
        self,
        *,
        name: str,
        content: bytes,
        media_type: str,
        agent: str,
        requested_by: str,
        sources: Sequence[str] = (),
        classification: str = "internal",
        now: int | None = None,
    ) -> dict[str, Any]:
        """Store a finished file; the same bytes for the same requester and name return the existing artifact."""
        if media_type not in MEDIA_TYPES:
            raise ArtifactError("unsupported media type; supported: " + ", ".join(sorted(MEDIA_TYPES.values())))
        if not isinstance(content, (bytes, bytearray)) or not content:
            raise ArtifactError("an artifact needs content")
        if len(content) > MAX_BYTES:
            raise ArtifactError(f"an artifact is at most {MAX_BYTES // (1024 * 1024)} MB")
        if classification not in CLASSIFICATIONS:
            raise ArtifactError(f"classification must be one of {', '.join(CLASSIFICATIONS)}")
        if classification == "restricted" and not self._allow_restricted:
            raise ArtifactError("restricted data is not stored unless this deployment's policy allows it")
        label = _display_name(name)
        if not isinstance(agent, str) or not _AGENT.match(agent):
            raise ArtifactError("agent must be a short Agent name")
        who = _principal(requested_by, "requested_by")
        listed = list(sources)
        if len(listed) > MAX_SOURCES or not all(isinstance(item, str) and _SOURCE.match(item) for item in listed):
            raise ArtifactError(f"sources are at most {MAX_SOURCES} identifiers such as kb:rules-of-engagement#hours")
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
        digest = hashlib.sha256(bytes(content)).hexdigest()
        with self._locked() as state:
            artifacts = state["artifacts"]
            existing = next((entry for entry in artifacts.values()
                             if entry["sha256"] == digest and entry["requestedBy"] == who and entry["name"] == label
                             and entry["agent"] == agent and entry["mediaType"] == media_type
                             and entry["classification"] == classification and entry["sources"] == listed
                             and self._expires_at(entry) > stamp), None)
            if existing is not None:
                # Idempotent success must not bless an unsafe or altered payload.
                existing_data = self._read_payload(self._file(existing["id"]))
                if hashlib.sha256(existing_data).hexdigest() != existing["sha256"]:
                    raise ArtifactError("the artifact's file no longer matches its recorded digest")
                return {**existing, "duplicate": True}
            if len(artifacts) >= MAX_ARTIFACTS:
                raise ArtifactError("the artifact store is full; expired or unneeded artifacts must be deleted")
            artifact_id = "a-" + secrets.token_hex(6)
            descriptor = os.open(self._file(artifact_id), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(bytes(content))
                stream.flush()
                os.fsync(stream.fileno())
            entry = {
                "id": artifact_id, "tenant": self.tenant, "name": label, "mediaType": media_type, "bytes": len(content),
                "sha256": digest, "agent": agent, "requestedBy": who, "sources": listed, "classification": classification,
                "location": f"tier0:{self.tenant}/{artifact_id}", "createdAt": stamp, "expiresAt": stamp + self._retention_seconds,
            }
            artifacts[artifact_id] = entry
        self._record("artifact.create", f"agent:{agent}", {**self._details(entry), "requestedBy": who, "classification": classification})
        return {**entry, "duplicate": False}

    # -- reads --------------------------------------------------------------------------

    def list(self, *, requested_by: str | None = None, now: int | None = None) -> list[dict[str, Any]]:
        """Return only unexpired metadata; retention applies before manual cleanup runs."""
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
        with self._locked() as state:
            entries = [dict(entry) for entry in state["artifacts"].values()
                       if self._expires_at(entry) > stamp]
        if requested_by is not None:
            entries = [entry for entry in entries if entry["requestedBy"] == requested_by]
        return sorted(entries, key=lambda entry: (entry["createdAt"], entry["id"]))

    def get(self, artifact_id: str, *, requested_by: str, admin: bool = False, now: int | None = None) -> tuple[dict[str, Any], bytes]:
        """The artifact and its bytes, for the person who asked for it or an administrator; verified against its digest."""
        who = _principal(requested_by, "requested_by")
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
        path = self._file(artifact_id)
        with self._locked() as state:
            entry = state["artifacts"].get(artifact_id)
            if entry is None:
                raise ArtifactError("no such artifact")
            if not admin and entry["requestedBy"] != who:
                raise ArtifactError("only the person who asked for an artifact or an administrator may read it")
            if self._expires_at(entry) <= stamp:
                raise ArtifactError("this artifact has expired under the client's retention period")
            data = self._read_payload(path)
            snapshot = dict(entry)
        if hashlib.sha256(data).hexdigest() != snapshot["sha256"]:
            raise ArtifactError("the artifact's file no longer matches its recorded digest")
        self._record("artifact.read", who, {"artifactId": artifact_id, "sha256": snapshot["sha256"], "admin": bool(admin)})
        return snapshot, data

    # -- deletion and retention ---------------------------------------------------------

    def _remove(self, state: dict[str, Any], artifact_id: str) -> dict[str, Any]:
        path = self._file(artifact_id)
        if self._private_path(path, missing=True) is not None:
            path.unlink()
        return state["artifacts"].pop(artifact_id)

    def delete(self, artifact_id: str, *, requested_by: str, admin: bool = False) -> dict[str, Any]:
        who = _principal(requested_by, "requested_by")
        self._file(artifact_id)
        with self._locked() as state:
            entry = state["artifacts"].get(artifact_id)
            if entry is None:
                raise ArtifactError("no such artifact")
            if not admin and entry["requestedBy"] != who:
                raise ArtifactError("only the person who asked for an artifact or an administrator may delete it")
            self._remove(state, artifact_id)
        self._record("artifact.delete", who, {"artifactId": artifact_id, "sha256": entry["sha256"], "admin": bool(admin)})
        return {"deleted": artifact_id}

    def purge_expired(self, *, now: int | None = None) -> dict[str, int]:
        """Delete every artifact past its retention period; each removal is recorded."""
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else now
        with self._locked() as state:
            expired = [self._remove(state, key) for key in [key for key, entry in state["artifacts"].items() if self._expires_at(entry) <= stamp]]
        for entry in expired:
            self._record("artifact.expire", "runtime:retention", {"artifactId": entry["id"], "sha256": entry["sha256"], "expiresAt": entry["expiresAt"]})
        return {"expired": len(expired)}


DOCUMENT_FORMATS = ("markdown", "docx", "xlsx", "pptx", "pdf", "csv")
_STRUCTURE_KEY = {"docx": "sections", "pdf": "sections", "pptx": "slides", "xlsx": "sheets", "csv": "rows"}


def _deliverables():
    name = "model_garden_tier0_deliverables"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name("deliverables.py"))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def create_document(
    store: "ArtifactStore", *, title: str, content: str | None = None, agent: str, requested_by: str, sources: Sequence[str] = (),
    format: str = "markdown", structure: Any = None,
) -> dict[str, Any]:
    """The Tier 0 body of the portable ``docs.create`` Tool: a finished document stored for the requester.

    ``documentId`` is the artifact's ``tier0:<tenant>/<id>`` location, which is what a person (or a channel reply) uses to find
    it; the same title and content from the same requester return the same document. The default ``markdown`` format takes
    ``content``. ``docx``, ``pdf``, ``pptx``, ``xlsx`` and ``csv`` take ``structure`` (``sections``, ``slides``, ``sheets`` or
    ``rows``; see ``deliverables.py``) and are built by it, so a coworker produces a real file and not only text. Passive content only (no
    macros, links or formulas), size-bounded before anything is written. Only identifiers, the format and the source
    identifiers go to the ledger (the store records them), never the text.
    """
    if not isinstance(title, str) or not title.strip():
        raise ArtifactError("a document needs a title")
    if format not in DOCUMENT_FORMATS:
        raise ArtifactError("format must be one of " + ", ".join(DOCUMENT_FORMATS))
    if format == "markdown":
        if structure is not None:
            raise ArtifactError("a markdown document takes content, not structure")
        if not isinstance(content, str) or not content.strip():
            raise ArtifactError("a document needs content")
        if len(content) > MAX_DOCUMENT_CHARS:
            raise ArtifactError(f"a document is at most {MAX_DOCUMENT_CHARS} characters")
        label = _display_name(title.strip() if title.strip().lower().endswith(".md") else title.strip() + ".md")
        entry = store.put(
            name=label, content=content.encode("utf-8"), media_type="text/markdown", agent=agent, requested_by=requested_by,
            sources=sources,
        )
    else:
        key = _STRUCTURE_KEY[format]
        if content not in (None, ""):
            raise ArtifactError(f"a {format} document takes structure ({key}), not content")
        if not isinstance(structure, dict) or set(structure) - {key} or key not in structure:
            raise ArtifactError(f"a {format} document needs structure with exactly {key}")
        module = _deliverables()
        built = {key: structure[key], **({"title": title.strip()} if format in ("docx", "pdf", "pptx") else {})}
        try:
            entry = module.deliver(
                store, kind=format, name=_display_name(title.strip()), content=built, agent=agent, requested_by=requested_by,
                sources=sources,
            )
        except module.DeliverableError as exc:
            raise ArtifactError(str(exc)) from exc
    return {
        "documentId": entry["location"], "artifactId": entry["id"], "name": entry["name"], "bytes": entry["bytes"],
        "sha256": entry["sha256"], "expiresAt": entry["expiresAt"], "duplicate": bool(entry.get("duplicate")),
        "format": format, "mediaType": entry["mediaType"],
    }

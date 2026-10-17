"""Tier 0 backing for ``files.*`` and ``docs.*``: the artifact store as the client's file system when none is bound.

A client with no Drive or SharePoint still needs ``docs.create`` to land somewhere and the coworker to find what it made
(#91, #77). This maps the portable ``files.search``, ``files.read``, ``files.write``, ``docs.create`` and ``docs.read``
Tools onto the Tier 0 artifact store (``artifacts.py``). It is credential-free and served by the governed sidecar.

- **The acting person is bound by the caller.** Executors are built for one verified requester, taken from the governed
  boundary's principal context, never from a model argument. A requester searches, reads and writes only their own
  artifacts, which is the Tier 0 form of "respects the source permissions of the acting principal".
- **No folders.** There is no folder tree at Tier 0, so a ``folderId`` is refused instead of silently ignored.
- **Text in, text out.** ``docs.read`` and ``files.write`` handle Markdown or plain text; ``docs.create`` also builds a real
  DOCX, XLSX, PPTX, PDF or CSV from ``format`` and ``structure`` (``deliverables.py``). Binary files appear in ``files.search`` and ``files.read`` as metadata only; their bytes are
  fetched through the artifact store, not handed to the model.
- **Ledger.** Creation is ledgered by the store with the location (``tier0:<tenant>/<id>``), size and digest, never the
  content or title; every result carries that location as ``externalObjectRef``.
"""
from __future__ import annotations

import pathlib
import re
import sys
import importlib.util
from collections.abc import Callable, Mapping
from typing import Any

PROVIDER_ID = "built-in-files"
MAX_TEXT = 200_000
MAX_READ_TEXT = 20_000
MAX_QUERY = 100
MAX_RESULTS = 25
TEXT_TYPES = {"text/markdown": ".md", "text/plain": ".txt"}
_ID = re.compile(r"^a-[0-9a-f]{12}$")


def _artifacts():
    name = "model_garden_tier0_artifacts"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name("artifacts.py"))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class FilesError(ValueError):
    """Raised for an invalid request or one the requester may not make; never carries file contents."""


def _only(arguments: Any, allowed: set[str], tool: str) -> Mapping[str, Any]:
    if not isinstance(arguments, Mapping):
        raise FilesError(f"{tool} arguments must be a JSON object")
    extra = sorted(set(arguments) - allowed)
    if extra:
        raise FilesError(f"{tool} does not accept: {', '.join(extra)}")
    return arguments


def _text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise FilesError(f"{label} must be a non-empty string of at most {limit} characters")
    return value


class Tier0Files:
    def __init__(self, store: Any, *, agent: str, requested_by: str) -> None:
        self._store = store
        self._agent = agent
        self._by = requested_by
        self._module = _artifacts()

    def _shape(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        return {"fileId": entry["id"], "name": entry["name"], "mediaType": entry["mediaType"], "bytes": entry["bytes"],
                "createdAt": entry["createdAt"], "externalObjectRef": entry["location"]}

    def _write(self, name: str, content: str, *, now: int | None) -> dict[str, Any]:
        text = _text(content, "content", MAX_TEXT)
        suffix = ".md"
        label = name if re.search(r"\.(md|txt)$", name, re.IGNORECASE) else name + suffix
        media = "text/plain" if label.lower().endswith(".txt") else "text/markdown"
        try:
            return self._store.put(name=label, content=text.encode("utf-8"), media_type=media, agent=self._agent,
                                   requested_by=self._by, now=now)
        except self._module.ArtifactError as exc:
            raise FilesError(str(exc)) from exc

    # -- portable Tools -----------------------------------------------------------------

    def docs_create(self, arguments: dict[str, Any], *, now: int | None = None) -> dict[str, Any]:
        args = _only(arguments, {"title", "content", "format", "structure", "sources"}, "docs.create")
        title = _text(args.get("title"), "title", 120)
        if args.get("format", "markdown") == "markdown" and args.get("structure") is None and not args.get("sources"):
            entry = self._write(title, args.get("content"), now=now)
        else:
            try:
                created = self._module.create_document(
                    self._store, title=title, content=args.get("content"), agent=self._agent, requested_by=self._by,
                    sources=args.get("sources") or (), format=args.get("format", "markdown"), structure=args.get("structure"),
                )
            except self._module.ArtifactError as exc:
                raise FilesError(str(exc)) from exc
            entry = {"id": created["artifactId"], "location": created["documentId"], "duplicate": created["duplicate"]}
        return {"provider": "built-in-artifacts", "documentId": entry["id"], "externalObjectRef": entry["location"], "duplicate": entry["duplicate"]}

    def files_write(self, arguments: dict[str, Any], *, now: int | None = None) -> dict[str, Any]:
        args = _only(arguments, {"name", "content", "folderId"}, "files.write")
        if args.get("folderId") is not None:
            raise FilesError("the Tier 0 store has no folders; omit folderId")
        entry = self._write(_text(args.get("name"), "name", 120), args.get("content"), now=now)
        return {"provider": PROVIDER_ID, "fileId": entry["id"], "externalObjectRef": entry["location"], "duplicate": entry["duplicate"]}

    def files_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        args = _only(arguments, {"query"}, "files.search")
        query = _text(args.get("query"), "query", MAX_QUERY).strip().lower()
        found = [entry for entry in self._store.list(requested_by=self._by) if query in entry["name"].lower()]
        found.sort(key=lambda entry: (entry["createdAt"], entry["id"]), reverse=True)
        return {"provider": PROVIDER_ID, "files": [self._shape(entry) for entry in found[:MAX_RESULTS]]}

    def files_read(self, arguments: dict[str, Any], *, now: int | None = None) -> dict[str, Any]:
        args = _only(arguments, {"fileId"}, "files.read")
        return {"provider": PROVIDER_ID, "file": self._read(args.get("fileId"), now)}

    def docs_read(self, arguments: dict[str, Any], *, now: int | None = None) -> dict[str, Any]:
        args = _only(arguments, {"documentId"}, "docs.read")
        return {"provider": "built-in-artifacts", "document": self._read(args.get("documentId"), now)}

    def _read(self, identifier: Any, now: int | None) -> dict[str, Any]:
        if not isinstance(identifier, str) or not _ID.match(identifier):
            raise FilesError("the id must be one returned by files.search or docs.create")
        try:
            entry, data = self._store.get(identifier, requested_by=self._by, now=now)
        except self._module.ArtifactError as exc:
            raise FilesError(str(exc)) from exc
        shaped = self._shape(entry)
        if entry["mediaType"] in TEXT_TYPES:
            shaped["text"] = data.decode("utf-8", errors="replace")[:MAX_READ_TEXT]
            shaped["truncated"] = len(data) > MAX_READ_TEXT
        else:
            shaped["text"] = None
            shaped["note"] = "binary file: metadata only; the file itself is fetched from the artifact store"
        return shaped

    def executors(self, *, now: Callable[[], int] | None = None) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        """Executor per portable Tool name for this requester, in the shape ``scripts/govern-action.py`` expects."""
        clock = now or (lambda: None)  # type: ignore[return-value]
        return {
            "docs.create": lambda args: self.docs_create(args, now=clock()),
            "docs.read": lambda args: self.docs_read(args, now=clock()),
            "files.write": lambda args: self.files_write(args, now=clock()),
            "files.search": self.files_search,
            "files.read": lambda args: self.files_read(args, now=clock()),
        }

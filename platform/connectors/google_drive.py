"""Google Drive connector for the portable ``files.*`` Tools, limited to configured folders.

Implements ``files.search``, ``files.read`` and ``files.write`` against the Drive v3 API behind an injected transport, with the
same result shapes as the Tier 0 artifact store (``builtin_files.py``) so one Agent and one Skill work with either. It is built
to sit behind the governed boundary:

- **Configured folders only.** The connection is configured with the folder ids it may touch (``folders``; there is no
  "whole Drive"). ``files.search`` only asks for direct children of those folders and ignores anything else Drive returns;
  ``files.read`` refuses a file whose parents are not among them; ``files.write`` writes only into one of them (the first when
  none is named). Drive has no recursive parent query, so subfolders must be listed to be covered.
- **Documents are data.** Text is returned only for Google Docs (exported as plain text) and plain text, Markdown, CSV and JSON
  files, bounded to 20,000 characters, and the result carries ``untrusted: true``: file contents are information for the
  coworker, never instructions or authority. Any other type returns metadata only.
- **Duplicate-safe writes.** A write is stamped with a content-derived ``appProperties`` marker and searched for before
  creating, so a re-delivered write returns the existing file (``duplicate: true``) instead of making a second copy.
- **No sharing.** ``files.share`` (a privileged Tool) is not implemented here.
- **Errors.** Failures raise ``DriveError`` with a ``code`` (``rate-limited``, ``outage``, ``unauthorized`` or ``rejected``);
  Drive's error text is never echoed (it can name files), only its reason code.
- **Ledger references.** Every result carries ``externalObjectRef`` (``google-drive:file/<id>``), so the ledger records a
  reference, never file content.

Authentication (an access token for the coworker's own identity, #129), consent, scopes granted and secret storage are outside
this module: the caller injects a token at runtime. ``drive.file`` only covers files the app created or the user opened with
it, so writing into an *existing* folder may need that folder shared with the integration identity or a broader scope; that and
read access to files the owner already has are to be confirmed in a live test. Status: credential-free tests only (a fake
transport); the sidecar does not serve these Tools for Google Drive and nothing here has had a live proof.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import re
import sys
import urllib.parse
from collections.abc import Callable, Sequence
from typing import Any

API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
SCOPES = {
    "files.search": ["https://www.googleapis.com/auth/drive.readonly"],
    "files.read": ["https://www.googleapis.com/auth/drive.readonly"],
    "files.write": ["https://www.googleapis.com/auth/drive.file"],
}
GOOGLE_DOC = "application/vnd.google-apps.document"
TEXT_TYPES = ("text/plain", "text/markdown", "text/csv", "application/json")
MAX_RESULTS = 10
MAX_QUERY = 200
MAX_READ_TEXT = 20_000
MAX_CONTENT_BYTES = 5 * 1024 * 1024
MAX_WRITE_CHARS = 200_000
MAX_NAME = 120
MAX_FOLDERS = 20
_FOLDER = re.compile(r"^[A-Za-z0-9_-]{10,100}$")
_FILE = re.compile(r"^[A-Za-z0-9_-]{10,100}$")
_NAME = re.compile(r"^[^\x00-\x1f/\\]{1,120}$")
_REASON = re.compile(r"^[A-Za-z]{3,60}$")
Transport = Callable[[str, str, dict[str, str], Any], tuple[int, Any]]


class DriveError(RuntimeError):
    """A Drive failure; ``code`` is one of ``rate-limited``, ``outage``, ``unauthorized`` or ``rejected``."""

    def __init__(self, message: str, code: str, status: int) -> None:
        super().__init__(message)
        self.code, self.status = code, status


def _default_transport() -> Transport:
    name = "model_garden_google_drive_http"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).resolve().parent / "http_common.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name].make_transport("Google Drive", re.compile(r"^www\.googleapis\.com$"))


def _quote(text: str) -> str:
    """A Drive query string literal: backslash and single quote escaped."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


class GoogleDriveClient:
    def __init__(self, access_token: str, *, folders: Sequence[str], transport: Transport | None = None) -> None:
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("Google Drive access token must be supplied at runtime")
        listed = list(folders)
        if not listed or len(listed) > MAX_FOLDERS or not all(isinstance(item, str) and _FOLDER.match(item) for item in listed):
            raise ValueError(f"list 1 to {MAX_FOLDERS} Drive folder ids this connection may use; there is no whole-Drive access")
        self._folders = list(dict.fromkeys(listed))
        self._allowed = frozenset(self._folders)
        self._token = access_token
        self._transport = transport or _default_transport()

    # -- plumbing -----------------------------------------------------------------------

    def _request(self, method: str, url: str, payload: Any = None) -> Any:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        status, body = self._transport(method, url, headers, payload)
        if 200 <= status < 300:
            return body if body is not None else {}
        reason = ""
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict) and isinstance(error.get("errors"), list) and error["errors"] and isinstance(error["errors"][0], dict):
            reason = str(error["errors"][0].get("reason", ""))
        reason = reason if _REASON.match(reason) else ""
        if status == 429 or reason in ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"):
            code = "rate-limited"
        elif status in (401, 403):
            code = "unauthorized"
        elif status >= 500:
            code = "outage"
        else:
            code = "rejected"
        raise DriveError(f"Google Drive request failed ({status}): {code}" + (f" [{reason}]" if reason else ""), code, status)

    @staticmethod
    def _id(value: Any, label: str) -> str:
        if not isinstance(value, str) or not _FILE.match(value):
            raise ValueError(f"{label} must be a Drive id returned by files.search or files.write")
        return value

    def _in_scope(self, parents: Any) -> bool:
        return isinstance(parents, list) and any(parent in self._allowed for parent in parents)

    @staticmethod
    def _only(arguments: Any, allowed: set[str], tool: str) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ValueError(f"{tool} arguments must be a JSON object")
        extra = sorted(set(arguments) - allowed)
        if extra:
            raise ValueError(f"{tool} supports only {', '.join(sorted(allowed))}: {', '.join(extra)}")
        return arguments

    @staticmethod
    def _shape(item: dict[str, Any]) -> dict[str, Any]:
        file_id = GoogleDriveClient._id(str(item.get("id", "")), "file id")
        size = item.get("size")
        return {
            "fileId": file_id, "name": str(item.get("name", ""))[:MAX_NAME * 2], "mediaType": str(item.get("mimeType", ""))[:100],
            "bytes": int(size) if isinstance(size, str) and size.isdigit() else None,
            "modifiedAt": item.get("modifiedTime") if isinstance(item.get("modifiedTime"), str) else None,
            "externalObjectRef": f"google-drive:file/{file_id}",
        }

    # -- portable Tools -----------------------------------------------------------------

    def search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        args = self._only(arguments, {"query"}, "files.search")
        query = args.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY or re.search(r"[\x00-\x1f]", query):
            raise ValueError(f"query must be a non-empty one-line string of at most {MAX_QUERY} characters")
        parents = " or ".join(f"{_quote(folder)} in parents" for folder in self._folders)
        q = f"trashed = false and fullText contains {_quote(query.strip())} and ({parents})"
        url = f"{API}/files?q={urllib.parse.quote(q, safe='')}&pageSize={MAX_RESULTS}&fields=" + urllib.parse.quote(
            "files(id,name,mimeType,size,modifiedTime,parents)", safe="")
        found = self._request("GET", url)
        items = found.get("files") if isinstance(found, dict) else None
        files = [self._shape(item) for item in (items or []) if isinstance(item, dict) and self._in_scope(item.get("parents"))]
        return {"provider": "google-drive", "files": files[:MAX_RESULTS]}

    def read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        args = self._only(arguments, {"fileId"}, "files.read")
        file_id = self._id(args.get("fileId"), "fileId")
        meta = self._request("GET", f"{API}/files/{file_id}?fields=" + urllib.parse.quote("id,name,mimeType,size,modifiedTime,parents", safe=""))
        if not isinstance(meta, dict) or not self._in_scope(meta.get("parents")):
            raise ValueError("that file is outside the folders this connection is configured for")
        shaped = self._shape(meta)
        media = shaped["mediaType"]
        size = shaped["bytes"]
        if media == GOOGLE_DOC:
            url = f"{API}/files/{file_id}/export?mimeType=text%2Fplain"
        elif media in TEXT_TYPES and (size is None or size <= MAX_CONTENT_BYTES):
            url = f"{API}/files/{file_id}?alt=media"
        else:
            return {"provider": "google-drive", "file": {**shaped, "text": None, "untrusted": True,
                                                       "note": "this type or size is not read as text: metadata only"}}
        body = self._request("GET", url)
        text = body.get("_text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            text = json.dumps(body) if isinstance(body, dict) and media == "application/json" else ""
        return {"provider": "google-drive", "file": {**shaped, "text": text[:MAX_READ_TEXT], "truncated": len(text) > MAX_READ_TEXT, "untrusted": True}}

    def write(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Create a text file in a configured folder; a re-delivered write returns the existing file."""
        args = self._only(arguments, {"name", "content", "folderId"}, "files.write")
        name, content = args.get("name"), args.get("content")
        if not isinstance(name, str) or not _NAME.match(name.strip()):
            raise ValueError(f"name must be one line of at most {MAX_NAME} characters with no slash")
        if not isinstance(content, str) or not content.strip() or len(content) > MAX_WRITE_CHARS:
            raise ValueError(f"content must be a non-empty string of at most {MAX_WRITE_CHARS} characters")
        folder = args.get("folderId", self._folders[0])
        if folder not in self._allowed:
            raise ValueError("folderId must be one of the folders this connection is configured for")
        name = name.strip()
        media = "text/plain" if name.lower().endswith(".txt") else "text/markdown"
        label = name if re.search(r"\.(md|txt)$", name, re.IGNORECASE) else name + ".md"
        marker = hashlib.sha256(json.dumps([folder, label, content], separators=(",", ":")).encode("utf-8")).hexdigest()[:32]
        q = f"appProperties has {{ key='mgkey' and value='{marker}' }} and {_quote(folder)} in parents and trashed = false"
        existing = self._request("GET", f"{API}/files?q={urllib.parse.quote(q, safe='')}&pageSize=1&fields=" + urllib.parse.quote("files(id,parents)", safe=""))
        found = existing.get("files") if isinstance(existing, dict) else None
        if found and isinstance(found[0], dict) and self._in_scope(found[0].get("parents")):
            file_id = self._id(str(found[0].get("id", "")), "file id")
            return {"provider": "google-drive", "fileId": file_id, "externalObjectRef": f"google-drive:file/{file_id}", "duplicate": True}
        created = self._request(
            "POST", f"{UPLOAD}/files?uploadType=multipart&fields=id",
            {"_multipart": {"metadata": {"name": label, "parents": [folder], "mimeType": media, "appProperties": {"mgkey": marker}},
                            "content": content, "contentType": media}},
        )
        file_id = self._id(str(created.get("id", "")) if isinstance(created, dict) else "", "file id")
        return {"provider": "google-drive", "fileId": file_id, "externalObjectRef": f"google-drive:file/{file_id}", "duplicate": False}

    def executors(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return {"files.search": self.search, "files.read": self.read, "files.write": self.write}

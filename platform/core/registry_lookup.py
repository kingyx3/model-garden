"""One-shot, read-only public MCP Registry discovery (#157).

No crawler, scheduled sync, OAuth token, installation, grant, or tool execution.
The only network destination is the official registry. A source adapter can be
injected for an explicitly reviewed vendor-maintained registry using the same
response contract; callers must not expose an arbitrary URL as a model Tool.
Successful bounded reads become *candidates*, never live integrations.
"""
from __future__ import annotations

import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

OFFICIAL = "https://registry.modelcontextprotocol.io/v0.1/servers"
MAX_BYTES = 1024 * 1024
MAX_PAGES = 4
PAGE_LIMIT = 50
TIMEOUT_SECONDS = 8


class RegistryLookupError(ValueError):
    """An untrusted or unavailable listing; prior local candidates are retained."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _get(url: str) -> Mapping[str, Any]:
    """GET only the fixed registry origin, with no redirect/SSRF target change."""
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme, parsed.hostname, parsed.port, parsed.username, parsed.password) != (
        "https", "registry.modelcontextprotocol.io", None, None, None
    ) or parsed.path != "/v0.1/servers":
        raise RegistryLookupError("registry URL is not on the approved origin")
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "ModelGarden/registry-discovery"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise RegistryLookupError("registry response is not successful")
            raw = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise RegistryLookupError("rate-limited" if exc.code == 429 else "http-error") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RegistryLookupError("network-error") from None
    if len(raw) > MAX_BYTES:
        raise RegistryLookupError("response-too-large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RegistryLookupError("invalid-json") from exc
    if not isinstance(data, dict):
        raise RegistryLookupError("invalid-feed")
    return data


def _timestamp(raw: Any) -> int | None:
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        try:
            return int(dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        except (ValueError, OverflowError):
            pass
    return None


def _entry(item: Any) -> dict[str, Any]:
    """Translate the official `{server, _meta}` envelope without trusting metadata."""
    if not isinstance(item, Mapping) or not isinstance(item.get("server"), Mapping):
        raise RegistryLookupError("malformed-server")
    server = item["server"]
    metadata = item.get("_meta", {})
    official = metadata.get("io.modelcontextprotocol.registry/official", {}) if isinstance(metadata, Mapping) else {}
    if not isinstance(official, Mapping):
        official = {}
    if official.get("status") in ("deleted", "deprecated"):
        raise RegistryLookupError("inactive-server")
    return {
        "name": server.get("name"),
        "version": server.get("version"),
        "description": server.get("description", ""),
        "remotes": server.get("remotes", []),
        "packages": server.get("packages", []),
        # Registry listings often lack licence/scope/tool attestations. Missing
        # evidence intentionally blocks review in connector_candidates.normalise.
        "license": server.get("license"),
        "scopes": server.get("scopes", []),
        "tools": server.get("tools", []),
        "updatedAt": _timestamp(official.get("updatedAt") or official.get("publishedAt")),
    }


def discover(
    query: str,
    *,
    fetch_page: Callable[[str], Mapping[str, Any]] = _get,
    base_url: str = OFFICIAL,
    max_pages: int = MAX_PAGES,
    now: int | None = None,
) -> dict[str, Any]:
    """Read up to max_pages; reject malformed pagination and fail atomically.

    The caller passes the returned entries to CandidateBook.ingest *only* on
    success, so a failed page cannot erase or partially update prior evidence.
    `fetch_page` is injectable for offline tests and reviewed vendor adapters;
    production CLI fixes the URL to the official registry.
    """
    if not isinstance(query, str) or not 2 <= len(query.strip()) <= 80 or any(ord(c) < 32 for c in query):
        raise RegistryLookupError("search must be 2-80 printable characters")
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= MAX_PAGES:
        raise RegistryLookupError("page limit out of bounds")
    seen: set[str] = set()
    entries: list[dict[str, Any]] = []
    skipped = 0
    cursor: str | None = None
    fetched = int(time.time()) if now is None else now
    truncated = False
    for _ in range(max_pages):
        params = {"limit": str(PAGE_LIMIT), "search": query.strip(), "version": "latest"}
        if cursor:
            params["cursor"] = cursor
        data = fetch_page(base_url + "?" + urllib.parse.urlencode(params))
        if not isinstance(data, Mapping) or not isinstance(data.get("servers"), list):
            raise RegistryLookupError("invalid-feed")
        if len(data["servers"]) > PAGE_LIMIT:
            raise RegistryLookupError("oversized-page")
        for item in data["servers"]:
            try:
                entries.append(_entry(item))
            except RegistryLookupError:
                skipped += 1
        meta = data.get("metadata", {})
        if not isinstance(meta, Mapping):
            raise RegistryLookupError("invalid-pagination")
        new_cursor = meta.get("nextCursor")
        if not new_cursor:
            break
        if not isinstance(new_cursor, str) or len(new_cursor) > 1024 or new_cursor in seen:
            raise RegistryLookupError("invalid-pagination")
        seen.add(new_cursor)
        cursor = new_cursor
    else:
        truncated = cursor is not None
    return {"entries": entries, "skipped": skipped, "pages": len(seen) + (0 if truncated else 1),
            "truncated": truncated, "source": "mcp-registry", "fetchedAt": fetched}

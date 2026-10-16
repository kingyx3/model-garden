"""Public-registry connector candidates: discovered, vetted, never trusted by being listed (#157).

Instead of writing an adapter per SaaS vendor, Model Garden can learn what exists from the official MCP Registry and from
vendor-maintained catalogs. A listing is only *information*. This is the credential-free core of treating it that way: it turns a
registry entry into a candidate with provenance and findings, walks it through explicit gates, and shows operators the candidate next
to the evidence-backed ``contracts/v1/integrations.json``, which stays the only authority for "ready". It fetches nothing (a feed is
passed in), installs nothing, grants nothing and executes nothing, and it is not wired to a directory command or the sidecar.

- **Gates, one at a time.** ``discovered → reviewed → contract-tested → approved → coworker-authorized → tenant-live-proven``. This module
  advances a candidate only as far as ``contract-tested`` (a named reviewer, then a contract-test digest). ``approved`` and beyond are
  *read from* the registry, the per-coworker grants and the evidence manifest; they cannot be set here, so there is no second status table.
- **Vetting findings, by code and place.** Unsafe remote endpoints (``endpoint_guard``), unpinned or ``latest`` versions, packages
  without a digest, a licence outside the approved list, a poisoned description (``mcp_tool_review``), poisoned sample tools, excessive
  or wildcard scopes, a stale listing, and a name that is the same as another entry's at the same version with different content
  (``version-conflict``). Blocking findings stop a candidate being reviewed. Findings never repeat the metadata text.
- **Exact operations only.** ``map_operations`` maps an upstream tool to a portable capability Tool only when the owner's mapping names it
  *and* the capability catalog has that Tool; everything else is counted as unmapped and not exposed, and any non-read Tool is marked
  as needing approval.
- **Change resets trust.** A new version or different content for a known name drops it back to ``discovered`` (re-review); a name on the
  deny-list is ``denied`` and stays so, including on later feeds.
- **Feeds fail soft.** A failed or rate-limited feed records only the error type and leaves the last good snapshot in place, which is
  reported ``stale`` once it is older than a week. Nothing is deleted because a feed was down.
- **Public and private are different books.** The public book holds feed candidates; a tenant's own custom connectors live in that
  tenant's private book (``scope="tenant:<slug>"``). A private entry is refused by the public book, the other way round too, and a
  tenant book is a separate file, so nothing is published or shared by accident.
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
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any

SCHEMA_VERSION = 1
GATES = ("discovered", "reviewed", "contract-tested", "approved", "coworker-authorized", "tenant-live-proven")
LOCAL_GATES = GATES[:3]
ALLOWED_LICENSES = frozenset({"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "MPL-2.0"})
STALE_LISTING_SECONDS = 180 * 86400
STALE_FEED_SECONDS = 7 * 86400
MAX_CANDIDATES = 5000
MAX_SCOPES = 10
BUILT = ("live", "built-credential-free")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{2,127}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.]+)?$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PERSON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$")
_SCOPE_WILDCARD = re.compile(r"(?:^|[:/._-])(?:\*|all|admin|root|owner|full[-_]?access)(?:$|[:/._-])", re.IGNORECASE)
_SOURCE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_REASON = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
Audit = Callable[[str, str, dict[str, Any]], Any]


class CandidateError(ValueError):
    """Raised for an invalid candidate or a transition this module does not allow; nothing is changed when it is raised."""


def _module(name: str, filename: str):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name(filename))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.match(r"^(\d+)\.(\d+)\.(\d+)", version).groups()) if _VERSION.match(version or "") else (-1,)


def _finding(code: str, severity: str, where: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "where": where}


# -- turning a listing into a candidate ---------------------------------------------------------


def normalise(entry: Any, *, source: str, fetched_at: int, private: bool = False) -> dict[str, Any]:
    """A candidate from one registry entry (``name``, ``version``, ``description``, ``remotes``, ``packages``, ``license``, ``scopes``, ``updatedAt``, ``tools``)."""
    if not isinstance(entry, Mapping):
        raise CandidateError("a registry entry is an object")
    if not isinstance(source, str) or not _SOURCE.match(source):
        raise CandidateError("source names the feed, for example mcp-registry")
    name = entry.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise CandidateError("a registry entry needs a plain name such as io.github.vendor/server")
    findings: list[dict[str, str]] = []
    version = entry.get("version")
    if not isinstance(version, str) or not _VERSION.match(version):
        findings.append(_finding("unpinned-version", "blocking", "version"))
        version = "0.0.0"
    guard, review = _module("model_garden_candidates_guard", "endpoint_guard.py"), _module("model_garden_candidates_review", "mcp_tool_review.py")
    remotes: list[dict[str, str]] = []
    for index, remote in enumerate(entry.get("remotes") or []):
        try:
            parts = guard.parse_endpoint(remote.get("url") if isinstance(remote, Mapping) else None)
        except ValueError:
            findings.append(_finding("unsafe-remote", "blocking", f"remotes/{index}"))
            continue
        remotes.append({"type": str(remote.get("type", ""))[:40], "url": f"https://{parts['host']}{parts['path']}"})
    packages: list[dict[str, str]] = []
    for index, package in enumerate(entry.get("packages") or []):
        if not isinstance(package, Mapping) or not isinstance(package.get("identifier"), str):
            findings.append(_finding("bad-package", "blocking", f"packages/{index}"))
            continue
        pinned = isinstance(package.get("version"), str) and _VERSION.match(package["version"])
        if not pinned:
            findings.append(_finding("unpinned-version", "blocking", f"packages/{index}"))
        digest = package.get("fileSha256")
        if not (isinstance(digest, str) and _DIGEST.match(digest)):
            findings.append(_finding("package-unpinned", "warning", f"packages/{index}"))
        packages.append({"registryType": str(package.get("registryType", ""))[:40], "identifier": package["identifier"][:200], "version": str(package.get("version", ""))[:40]})
    if not remotes and not packages:
        findings.append(_finding("no-install-path", "blocking", "remotes"))
    licence = entry.get("license")
    if licence not in ALLOWED_LICENSES:
        findings.append(_finding("license-not-approved", "blocking", "license"))
    description = entry.get("description", "")
    if isinstance(description, str) and description.strip():
        if review.review({"name": "listing", "description": description[:4000], "inputSchema": {}})["blocking"]:
            findings.append(_finding("poisoned-description", "blocking", "description"))
    for index, tool in enumerate(entry.get("tools") or []):
        if not isinstance(tool, Mapping) or review.review(tool)["blocking"]:
            findings.append(_finding("poisoned-tool", "blocking", f"tools/{index}"))
    scopes = entry.get("scopes") or []
    if not isinstance(scopes, list) or not all(isinstance(item, str) for item in scopes) or len(scopes) > MAX_SCOPES or any(_SCOPE_WILDCARD.search(item) for item in scopes):
        findings.append(_finding("excessive-scopes", "blocking", "scopes"))
    updated = entry.get("updatedAt")
    if isinstance(updated, int) and not isinstance(updated, bool) and fetched_at - updated > STALE_LISTING_SECONDS:
        findings.append(_finding("stale-listing", "warning", "updatedAt"))
    unique = {(item["code"], item["where"]): item for item in findings}
    ordered = sorted(unique.values(), key=lambda item: (item["severity"] != "blocking", item["code"], item["where"]))
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:63]
    return {
        "id": slug, "name": name, "version": version, "publisher": name.split("/", 1)[0][:80], "license": licence if isinstance(licence, str) else None,
        "remotes": remotes, "packages": packages, "toolCount": len(entry.get("tools") or []), "updatedAt": updated if isinstance(updated, int) else None,
        "provenance": {"source": source, "fetchedAt": fetched_at, "sourceDigest": hashlib.sha256(_canonical(entry).encode("utf-8")).hexdigest()},
        "findings": ordered, "blocking": any(item["severity"] == "blocking" for item in ordered), "gate": "discovered", "private": bool(private),
    }


def dedupe(candidates: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One candidate per name (the highest version); the same name and version with different content is flagged ``version-conflict`` on both."""
    by_name: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        by_name.setdefault(candidate["name"], []).append(dict(candidate))
    chosen: list[dict[str, Any]] = []
    for name, group in sorted(by_name.items()):
        top = max(_version_key(item["version"]) for item in group)
        same = [item for item in group if _version_key(item["version"]) == top]
        if len({item["provenance"]["sourceDigest"] for item in same}) > 1:
            for item in same:
                item["findings"] = [*item["findings"], _finding("version-conflict", "blocking", "version")]
                item["blocking"] = True
            chosen.extend(same)
        else:
            chosen.append(same[0])
    return chosen


def map_operations(upstream_tools: Iterable[str], mapping: Mapping[str, str], catalog: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Which upstream tools may be exposed: only those the owner's mapping names that exist in the portable capability catalog."""
    mapped, unmapped = [], 0
    for upstream in sorted({item for item in upstream_tools if isinstance(item, str)}):
        tool = mapping.get(upstream)
        entry = catalog.get(tool) if isinstance(tool, str) else None
        if entry is None:
            unmapped += 1
            continue
        risk = entry.get("risk", "write")
        mapped.append({"upstream": upstream, "tool": tool, "risk": risk, "approvalRequired": risk != "read"})
    return {"mapped": mapped, "unmappedCount": unmapped}


# -- the book -----------------------------------------------------------------------------------


class CandidateBook:
    def __init__(self, root: pathlib.Path, *, scope: str = "public", audit: Audit | None = None) -> None:
        if scope != "public" and not (isinstance(scope, str) and scope.startswith("tenant:") and _TENANT.match(scope[7:])):
            raise CandidateError('scope is "public" or "tenant:<slug>"')
        self.scope = scope
        self.root = pathlib.Path(root)
        self.path = self.root / (("public" if scope == "public" else "tenant-" + scope[7:]) + ".candidates.json")
        self._audit = audit

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        name = "public" if self.scope == "public" else "tenant-" + self.scope[7:]
        descriptor = os.open(self.root / f".{name}.candidates.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            state: dict[str, Any] = {"schemaVersion": SCHEMA_VERSION, "scope": self.scope, "candidates": {}, "feeds": {}, "denied": {}, "links": {}}
            if self.path.is_file():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict) or loaded.get("schemaVersion") != SCHEMA_VERSION or loaded.get("scope") != self.scope:
                    raise CandidateError("candidate book has an unsupported format or belongs to another scope")
                state = loaded
            before = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != before:
                temporary = self.path.with_suffix(".tmp")
                handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write(json.dumps(state, sort_keys=True) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _record(self, action: str, by: str, details: dict[str, Any]) -> None:
        if self._audit is not None:
            self._audit(action, by, details)

    # -- feeds ---------------------------------------------------------------------------------

    def ingest(self, candidates: Iterable[Mapping[str, Any]], *, source: str, now: int) -> dict[str, list[str]]:
        """Add or refresh candidates from one feed. A changed version or content resets a candidate to ``discovered``."""
        private = self.scope != "public"
        report: dict[str, list[str]] = {"added": [], "refreshed": [], "reset": [], "denied": []}
        incoming = dedupe(candidates)
        if any(bool(item.get("private")) != private for item in incoming):
            raise CandidateError("private custom connectors and public feed candidates are kept in separate books")
        with self._locked() as state:
            for candidate in incoming:
                key = candidate["id"]
                if candidate["name"] in state["denied"]:
                    report["denied"].append(key)
                    continue
                existing = state["candidates"].get(key)
                if existing is None:
                    if len(state["candidates"]) >= MAX_CANDIDATES:
                        raise CandidateError("the candidate book is full")
                    state["candidates"][key] = candidate
                    report["added"].append(key)
                elif (existing["version"], existing["provenance"]["sourceDigest"]) != (candidate["version"], candidate["provenance"]["sourceDigest"]):
                    state["candidates"][key] = {**candidate, "previousVersion": existing["version"], "linkedProvider": existing.get("linkedProvider")}
                    report["reset"].append(key)
                else:
                    existing["provenance"]["fetchedAt"] = now
                    report["refreshed"].append(key)
            state["feeds"][source] = {"lastGood": now, "count": len(incoming)}
        self._record("candidates.ingest", f"feed:{source}", {"scope": self.scope, **{key: len(value) for key, value in report.items()}})
        return report

    def feed_failed(self, source: str, error_type: str, *, now: int) -> None:
        """Remember that a feed failed (the error type only); the last good snapshot stays in place."""
        if not isinstance(source, str) or not _SOURCE.match(source) or not isinstance(error_type, str) or not _REASON.match(error_type.lower()):
            raise CandidateError("name the feed and an error type such as rate-limited")
        with self._locked() as state:
            feed = state["feeds"].setdefault(source, {"lastGood": None, "count": 0})
            feed["lastFailure"], feed["lastFailureAt"] = error_type.lower(), now
        self._record("candidates.feed-failed", f"feed:{source}", {"scope": self.scope, "error": error_type.lower()})

    def feed_state(self, source: str, *, now: int) -> dict[str, Any]:
        with self._locked() as state:
            feed = dict(state["feeds"].get(source) or {})
        if not feed or feed.get("lastGood") is None:
            return {"state": "never-fetched", **feed}
        return {"state": "stale" if now - feed["lastGood"] > STALE_FEED_SECONDS else "fresh", **feed}

    # -- gates ---------------------------------------------------------------------------------

    def advance(self, candidate_id: str, to_gate: str, *, by: str, now: int, contract_test_digest: str | None = None) -> dict[str, Any]:
        """Move one step forward to ``reviewed`` or ``contract-tested``. Later gates come from other evidence and cannot be set here."""
        if not isinstance(by, str) or not _PERSON.match(by):
            raise CandidateError("a named person advances a candidate")
        if to_gate not in GATES:
            raise CandidateError("unknown gate")
        if to_gate not in LOCAL_GATES[1:]:
            raise CandidateError("approved, coworker-authorized and tenant-live-proven come from the integration registry, the grants and the evidence manifest, not from this book")
        with self._locked() as state:
            candidate = state["candidates"].get(candidate_id)
            if candidate is None:
                raise CandidateError("no such candidate")
            if candidate["gate"] == "denied" or candidate["name"] in state["denied"]:
                raise CandidateError("a denied candidate cannot advance")
            if GATES.index(to_gate) != GATES.index(candidate["gate"]) + 1:
                raise CandidateError(f"a candidate at {candidate['gate']} moves to {GATES[GATES.index(candidate['gate']) + 1]} next, in order")
            if candidate["blocking"]:
                raise CandidateError("blocking findings must be resolved upstream and the listing re-fetched before review")
            if to_gate == "contract-tested":
                if not (isinstance(contract_test_digest, str) and _DIGEST.match(contract_test_digest)):
                    raise CandidateError("a contract-tested candidate needs the digest of its passing contract-test run")
                candidate["contractTestDigest"] = contract_test_digest
            candidate["gate"] = to_gate
            candidate.setdefault("history", []).append({"gate": to_gate, "by": by, "at": now})
            snapshot = {"candidateId": candidate_id, "gate": to_gate, "version": candidate["version"]}
        self._record("candidates.advance", by, snapshot)
        return snapshot

    def deny(self, name: str, reason: str, *, by: str, now: int) -> int:
        """Deny-list a name: every candidate with it becomes ``denied`` and later feeds cannot bring it back."""
        if not isinstance(by, str) or not _PERSON.match(by) or not isinstance(reason, str) or not _REASON.match(reason):
            raise CandidateError("deny needs a named person and a short reason code such as malicious or abandoned")
        count = 0
        with self._locked() as state:
            state["denied"][name] = {"reason": reason, "by": by, "at": now}
            for candidate in state["candidates"].values():
                if candidate["name"] == name:
                    candidate["gate"] = "denied"
                    count += 1
        self._record("candidates.deny", by, {"name": name, "reason": reason, "candidates": count})
        return count

    def link(self, candidate_id: str, provider_id: str, *, by: str) -> None:
        """Say which registry provider entry this candidate corresponds to; it grants nothing."""
        if not isinstance(by, str) or not _PERSON.match(by) or not isinstance(provider_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", provider_id):
            raise CandidateError("link needs a named person and a registry provider id")
        with self._locked() as state:
            candidate = state["candidates"].get(candidate_id)
            if candidate is None:
                raise CandidateError("no such candidate")
            candidate["linkedProvider"] = provider_id
        self._record("candidates.link", by, {"candidateId": candidate_id, "provider": provider_id})

    # -- reading -------------------------------------------------------------------------------

    def directory(self, registry: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Candidates next to the evidence-backed registry. ``providerReady`` is true only when the linked registry entry is built or live."""
        statuses = {item["id"]: item.get("status") for item in registry.get("providers", []) if isinstance(item, Mapping)}
        with self._locked() as state:
            candidates = [dict(item) for item in state["candidates"].values()]
        rows = []
        for item in sorted(candidates, key=lambda entry: entry["id"]):
            provider = item.get("linkedProvider")
            registry_status = statuses.get(provider) if provider else None
            rows.append({
                "id": item["id"], "name": item["name"], "version": item["version"], "gate": item["gate"], "blocking": item["blocking"],
                "findings": [finding["code"] for finding in item["findings"]], "linkedProvider": provider, "registryStatus": registry_status,
                "providerReady": registry_status in BUILT, "private": item.get("private", False),
            })
        return rows

    def get(self, candidate_id: str) -> dict[str, Any] | None:
        with self._locked() as state:
            item = state["candidates"].get(candidate_id)
            return dict(item) if item else None

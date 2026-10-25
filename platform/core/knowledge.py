"""Tier 0 business-rules knowledge: parse a client's `.txt` or `.md` into citable sections and search it.

A small business can satisfy `kb` with one uploaded file of rules of engagement (hours, services,
prices it may quote, booking and escalation rules). This module is the credential-free core:

- `parse_sections` splits the file into stable, citable sections (Markdown headings, or
  recognisable plain-text headings, or paragraph chunks when there are none);
- `check_source` is the upload parse/test, reporting sections, digest and warnings;
- `search` ranks sections for a query and returns `{"documents": [...]}` in the `kb.search`
  contract, every document carrying its section citation, source digest and version;
- only content that the client owner has marked `approved`, that is effective today and that
  names this Agent (when audiences are set) is searchable; anything else returns no documents and
  a reason, so the caller clarifies or escalates instead of inventing a rule;
- the owner's `classification` limits who is answered from a source: `public` and `internal` ground any
  answer (a business's quoted prices are "internal" until it says otherwise), `confidential` grounds
  only a verified, mapped team member (never a public or unattributed caller such as a customer's
  email or call), and `restricted` is not served by the built-in store at all, because it has no
  document-level permissions; it needs a provider that does. A `locale` label lets a caller ask for
  one language, and each returned section carries freshness facts (effective date, review-by date and
  days left, with `reviewSoon` inside two weeks) so an answer can say how current its basis is;

Retrieved text is untrusted reference data: results are flagged `dataOnly` and never carry
instructions. Text that reads like an instruction to the reader ("ignore your instructions") is
flagged `instruction-like-text` on its document, and two approved sections with the same topic but
different prices, amounts or times are flagged as a conflict (see `conflicts`) so the coworker
says the rules disagree and escalates instead of choosing one. Each client/environment searches only the content compiled into its own desired
state. The governed runtime exposes it as `kb_search` and the registry lists `text-knowledge` as
`built-credential-free`; there is no live acceptance evidence yet.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
import pathlib
import re
from collections.abc import Mapping
from typing import Any

SUFFIXES = (".txt", ".md")
STATUSES = ("draft", "approved", "retired")
CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")
REVIEW_SOON_DAYS = 14
LOCALE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}$")
MAX_BYTES = 256 * 1024
MAX_SECTIONS = 500
MAX_RESULTS = 10
CHUNK_CHARS = 800
_STOPWORDS = frozenset(
    "a an and are as at be by can do does for from has have how i in is it its me my of on or our so that the "
    "their there they this to us was we what when where which who will with you your".split()
)
_WORD = re.compile(r"[a-z0-9][a-z0-9'\-]*")
# A fact worth contradicting: a money amount or a clock time. Words are not compared, so wording differences never conflict.
_FACT = re.compile(r"(?<![\w.])(?:S\$|[$£€])\s?\d[\d,]*(?:\.\d+)?|\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b|\b\d{1,2}:\d{2}\b", re.IGNORECASE)
_INSTRUCTION_LIKE = (
    re.compile(r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}\b(?:previous|prior|above|earlier|all|your|the|these)\b[^.\n]{0,40}\b(?:instructions?|rules?|prompts?|polic(?:y|ies))\b", re.IGNORECASE),
    re.compile(r"\b(?:system|developer)\s+(?:prompt|message|instructions?)\b", re.IGNORECASE),
    re.compile(r"\bnew\s+instructions?\s*:", re.IGNORECASE),
    re.compile(r"\b(?:reveal|print|show|send|email|forward)\b[^.\n]{0,60}\b(?:instructions|prompt|credentials?|passwords?|api[- ]?keys?|secrets?)\b", re.IGNORECASE),
)
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_NUMBERED = re.compile(r"^\d+[.)]\s+\S")
_UNDERLINE = re.compile(r"^(=+|-+)\s*$")


class KnowledgeError(ValueError):
    """Raised when a knowledge file cannot be used safely."""


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "section"


def _stem(word: str) -> str:
    """Fold simple plurals so 'smells' finds 'smell' and 'prices' finds 'price'."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _tokens(text: str) -> list[str]:
    return [_stem(word) for word in _WORD.findall(text.lower()) if word not in _STOPWORDS and len(word) > 1]


def _is_plain_heading(lines: list[str], index: int) -> bool:
    line = lines[index].strip()
    if not line or len(line) > 80 or line[-1] in ".!?;,":
        return False
    previous_blank = index == 0 or not lines[index - 1].strip()
    following = lines[index + 1].strip() if index + 1 < len(lines) else ""
    if not previous_blank or not following:
        return False
    if _UNDERLINE.fullmatch(following) and len(following) >= 3:
        return True
    return bool(line.endswith(":") or (line.isupper() and any(c.isalpha() for c in line)) or _NUMBERED.match(line))


def _chunks(text: str) -> list[tuple[str, str]]:
    """Paragraph chunks (about 800 characters) for a file with no headings."""
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", text) if item.strip()]
    chunks: list[str] = []
    for paragraph in paragraphs:
        if chunks and len(chunks[-1]) + len(paragraph) < CHUNK_CHARS:
            chunks[-1] += "\n\n" + paragraph
        else:
            chunks.append(paragraph)
    return [(f"Part {number}", chunk) for number, chunk in enumerate(chunks, start=1)]


def parse_sections(text: str, *, markdown: bool) -> list[dict[str, Any]]:
    """Split into sections with stable IDs (`NNN-title-slug`), each with its own content digest."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    raw: list[tuple[str, list[str]]] = []
    current_title: str | None = None
    current: list[str] = []
    found_heading = False

    def flush() -> None:
        body = [line for line in current]
        if any(line.strip() for line in body):
            raw.append((current_title or "Overview", body))

    index = 0
    while index < len(lines):
        line = lines[index]
        heading: str | None = None
        skip = 0
        match = _MD_HEADING.match(line) if markdown else None
        if match:
            heading = match.group(2).strip()
        elif not markdown and _is_plain_heading(lines, index):
            heading = line.strip().rstrip(":").strip()
            if index + 1 < len(lines) and _UNDERLINE.fullmatch(lines[index + 1].strip() or "x"):
                skip = 1
        if heading:
            flush()
            current_title, current = heading, []
            found_heading = True
            index += 1 + skip
            continue
        current.append(line)
        index += 1
    flush()
    if not found_heading:
        sections = _chunks("\n".join(lines))
    else:
        sections = [(title, "\n".join(body).strip()) for title, body in raw]
    if len(sections) > MAX_SECTIONS:
        raise KnowledgeError(f"knowledge file has more than {MAX_SECTIONS} sections; split it or shorten it")
    return [
        {"id": f"{number:03d}-{_slug(title)}", "title": title, "text": body, "digest": digest(body)}
        for number, (title, body) in enumerate(sections, start=1)
        if body.strip() or found_heading
    ]


def read_text_file(path: pathlib.Path, *, root: pathlib.Path | None = None) -> str:
    """Read a bounded UTF-8 `.txt`/`.md` file, optionally confined to ``root``."""
    path = pathlib.Path(path)
    if path.suffix.lower() not in SUFFIXES:
        raise KnowledgeError(f"knowledge file must be {' or '.join(SUFFIXES)}, not {path.suffix or 'extensionless'}")
    resolved = path.resolve()
    if root is not None:
        try:
            resolved.relative_to(pathlib.Path(root).resolve())
        except ValueError as exc:
            raise KnowledgeError(f"knowledge file escapes its allowed root: {path}") from exc
    if not resolved.is_file():
        raise KnowledgeError(f"knowledge file does not exist: {path}")
    if resolved.stat().st_size > MAX_BYTES:
        raise KnowledgeError(f"knowledge file is larger than {MAX_BYTES // 1024} KiB")
    try:
        return resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise KnowledgeError("knowledge file must be UTF-8 text") from exc


def check_source(text: str, *, filename: str, classification: str | None = None) -> dict[str, Any]:
    """The upload parse/test: what would be searchable, and anything the owner should fix."""
    if not text.strip():
        raise KnowledgeError("knowledge file is empty")
    markdown = filename.lower().endswith(".md")
    sections = parse_sections(text, markdown=markdown)
    if not sections:
        raise KnowledgeError("knowledge file has no usable text")
    warnings: list[str] = []
    if len(sections) == 1 and sections[0]["title"].startswith("Part"):
        warnings.append("no headings found: answers can only cite generic parts; add headings per topic (hours, prices, escalation)")
    if len({section["title"] for section in sections}) != len(sections):
        warnings.append("some section titles repeat; citations use the numbered section ID")
    for conflict in find_conflicts([{"title": section["title"], "content": section["text"], "citation": section["id"]} for section in sections]):
        warnings.append(f"conflicting values under \"{conflict['topic']}\" ({', '.join(conflict['differences'])}): the owner must say which is right before it is approved")
    if any(instruction_warnings(section["text"]) for section in sections):
        warnings.append("some text reads like instructions to the coworker; knowledge is reference data only and will be treated as such")
    if classification == "confidential":
        warnings.append("classified confidential: only a verified team member is answered from it; customers and callers are not, so a public-facing Skill needs another source")
    elif classification == "restricted":
        warnings.append("classified restricted: the built-in store does not serve it at all (it has no document-level permissions); connect a provider that does, or lower the classification after review")
    lowered = text.lower()
    for topic, hints in (("hours", ("hours", "open")), ("escalation", ("escalat", "hand off", "human")), ("prices", ("price", "fee", "cost", "$"))):
        if not any(hint in lowered for hint in hints):
            warnings.append(f"no mention of {topic}: the coworker will have to escalate those questions")
    return {
        "sections": [{"id": s["id"], "title": s["title"], "characters": len(s["text"])} for s in sections],
        "sectionCount": len(sections),
        "sha256": digest(text),
        "warnings": warnings,
    }


def build_index(name: str, text: str, *, filename: str, spec: Mapping[str, Any] | None = None) -> dict[str, Any]:
    spec = spec or {}
    sections = parse_sections(text, markdown=filename.lower().endswith(".md"))
    document_frequency: dict[str, int] = {}
    for section in sections:
        for token in set(_tokens(section["title"] + " " + section["text"])):
            document_frequency[token] = document_frequency.get(token, 0) + 1
    return {
        "name": name,
        "sha256": digest(text),
        "version": spec.get("version"),
        "status": spec.get("status", "draft"),
        "effectiveDate": spec.get("effectiveDate"),
        "reviewBy": spec.get("reviewBy"),
        "classification": spec.get("classification", "internal"),
        "locale": spec.get("locale"),
        "audiences": list(spec.get("audiences", [])),
        "sections": sections,
        "df": document_frequency,
    }


def availability(
    index: Mapping[str, Any], *, agent: str | None = None, today: dt.date | None = None, access: str = "public"
) -> str | None:
    """None when the source may ground answers, else the reason it may not.

    ``access`` is the asker's standing as the runtime verified it: ``member`` for a mapped team member, anything else (a public
    caller, or no attributed person at all) is treated as ``public``, so an unknown asker never sees more.
    """
    if index["status"] != "approved":
        return f"source-{index['status']}"
    effective = index.get("effectiveDate")
    if effective:
        try:
            starts = dt.date.fromisoformat(str(effective))
        except ValueError:
            return "source-effective-date-invalid"
        if starts > (today or dt.date.today()):
            return "source-not-yet-effective"
    review_by = index.get("reviewBy")
    if review_by:
        try:
            last_day = dt.date.fromisoformat(str(review_by))
        except ValueError:
            return "source-review-date-invalid"
        if last_day < (today or dt.date.today()):
            return "source-stale"
    audiences = index.get("audiences") or []
    if audiences and agent not in audiences:
        return "source-not-for-this-agent"
    classification = index.get("classification", "internal")
    if classification == "restricted":
        return "source-restricted"
    if classification == "confidential" and access != "member":
        return "source-members-only"
    return None


def source_state(spec: Mapping[str, Any], *, agent: str | None = None, today: dt.date | None = None,
                 access: str = "member") -> str:
    """Whether one KnowledgeSource declaration may ground answers for an Agent, without reading its file.

    ``usable`` or the reason it is not: ``draft``, ``retired``, ``not-yet-effective``,
    ``effective-date-invalid``, ``stale`` (past its ``reviewBy`` date), ``review-date-invalid``,
    ``not-for-this-agent``, ``restricted`` (classified so the built-in store may not serve it) or ``not-indexed`` (a source
    type that has no governed, retrievable content here). A ``confidential`` source is ``usable``: it answers team members.
    """
    if spec.get("type") != "text-file":
        return "not-indexed"
    reason = availability(
        {"status": spec.get("status", "draft"), "effectiveDate": spec.get("effectiveDate"),
         "reviewBy": spec.get("reviewBy"), "audiences": spec.get("audiences", []),
         "classification": spec.get("classification", "internal")},
        agent=agent, today=today, access=access,
    )
    return "usable" if reason is None else reason.removeprefix("source-")


def _facts(text: str) -> frozenset[str]:
    return frozenset(re.sub(r"\s+", "", match.group(0).lower()) for match in _FACT.finditer(text))


def _topic(title: str) -> str:
    return " ".join(sorted({_stem(word) for word in _tokens(title)}))


def instruction_warnings(text: str) -> list[str]:
    """``["instruction-like-text"]`` when the text reads like an instruction to its reader; it is flagged, never obeyed."""
    return ["instruction-like-text"] if any(pattern.search(text) for pattern in _INSTRUCTION_LIKE) else []


def find_conflicts(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sections on the same topic whose prices, amounts or times disagree; marks each such document ``conflict: true``.

    Two documents conflict when their titles name the same topic and both state facts, but not the same facts. The
    difference is reported as the values themselves. Sections with different wording and the same facts do not conflict,
    and a section with no amount or time never does.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for document in documents:
        topic = _topic(document["title"])
        if topic and _facts(document["content"]):
            groups.setdefault(topic, []).append(document)
    conflicts: list[dict[str, Any]] = []
    for topic, members in sorted(groups.items()):
        distinct = {_facts(member["content"]) for member in members}
        if len(distinct) < 2:
            continue
        values = sorted(set().union(*distinct) - frozenset.intersection(*distinct))
        for member in members:
            member["conflict"] = True
        conflicts.append({"topic": members[0]["title"], "citations": [member["citation"] for member in members], "differences": values})
    return conflicts


def _conflict_guidance(result: dict[str, Any], conflicts: list[dict[str, Any]]) -> None:
    if conflicts:
        result["conflicts"] = conflicts
        result["guidance"] = ("approved sections disagree on " + "; ".join(item["topic"] for item in conflicts)
                              + ": do not choose one; tell the customer you will confirm and escalate to the owner")


def freshness(index: Mapping[str, Any], *, today: dt.date | None = None) -> dict[str, Any]:
    """How current the basis of an answer is: the declared effective date, the review-by date, the days left and whether a review is due soon."""
    facts: dict[str, Any] = {}
    if index.get("effectiveDate"):
        facts["effectiveDate"] = str(index["effectiveDate"])
    if index.get("reviewBy"):
        facts["reviewBy"] = str(index["reviewBy"])
        try:
            left = (dt.date.fromisoformat(str(index["reviewBy"])) - (today or dt.date.today())).days
        except ValueError:
            return facts
        facts["reviewInDays"] = left
        if left <= REVIEW_SOON_DAYS:
            facts["reviewSoon"] = True
    return facts


def search(
    index: Mapping[str, Any],
    query: str,
    *,
    limit: int = 3,
    agent: str | None = None,
    today: dt.date | None = None,
    access: str = "public",
) -> dict[str, Any]:
    """Rank sections for ``query``; returns the `kb.search` result shape plus availability facts."""
    limit = max(1, min(int(limit), MAX_RESULTS))
    reason = availability(index, agent=agent, today=today, access=access)
    if reason is not None:
        return {"documents": [], "reason": reason, "dataOnly": True}
    terms = _tokens(query)
    total = max(1, len(index["sections"]))
    scored = []
    for position, section in enumerate(index["sections"]):
        body = _tokens(section["text"])
        title = set(_tokens(section["title"]))
        score = 0.0
        for term in set(terms):
            idf = math.log(1 + total / (1 + index["df"].get(term, 0)))
            score += idf * (body.count(term) + (3 if term in title else 0))
        if score > 0:
            scored.append((-score, position, section))
    scored.sort(key=lambda item: (item[0], item[1]))
    documents = [
        {
            "id": section["id"],
            "title": section["title"],
            "content": section["text"],
            "citation": f"knowledge:{index['name']}#{section['id']}",
            "sourceSha256": index["sha256"],
            "sourceVersion": index.get("version"),
            "score": round(-negative, 4),
        }
        for negative, _, section in scored[:limit]
    ]
    facts = freshness(index, today=today)
    for document in documents:
        warnings = instruction_warnings(document["content"])
        if warnings:
            document["contentWarnings"] = warnings
        if facts:
            document["freshness"] = dict(facts)
        if index.get("locale"):
            document["locale"] = index["locale"]
    result: dict[str, Any] = {"documents": documents, "dataOnly": True}
    _conflict_guidance(result, find_conflicts(documents))
    if not documents:
        result["reason"] = "no-matching-section"
    return result


def search_selected(
    desired_state: Mapping[str, Any],
    query: str,
    *,
    source: str | None = None,
    limit: int = 3,
    today: dt.date | None = None,
    access: str = "public",
    locale: str | None = None,
    require_integrity: bool = False,
) -> dict[str, Any]:
    """`kb.search` over the Tier 0 sources compiled into this client's own desired state.

    ``access`` is the asker's verified standing (``member`` or, for anyone else, ``public``); ``locale`` asks for one language, and
    a source with no locale label matches every request.
    """
    if locale is not None and not (isinstance(locale, str) and LOCALE.match(locale)):
        raise KnowledgeError("locale must be a language tag such as en or en-SG")
    contents = desired_state.get("knowledgeContent", {})
    agent = desired_state.get("source", {}).get("name")
    selected = {
        item.get("metadata", {}).get("name"): item
        for item in desired_state.get("knowledge", [])
        if isinstance(item, dict) and item.get("spec", {}).get("type") == "text-file"
    }
    names = [source] if source else sorted(selected)
    if source and source not in selected:
        return {"documents": [], "reason": "source-not-selected", "dataOnly": True}
    documents: list[dict[str, Any]] = []
    reasons: dict[str, str] = {}
    for name in names:
        record = contents.get(name)
        if record is None:
            reasons[name] = "source-content-missing"
            continue
        # The governed runtime requires the compiler's content digest and
        # section count on every read. A changed or incomplete desired-state
        # payload must not be treated as owner-approved knowledge. The default
        # remains compatible with metadata-only synthetic evaluation fixtures.
        if require_integrity:
            if not isinstance(record, Mapping):
                reasons[name] = "source-content-integrity"
                continue
            text, filename = record.get("content"), record.get("source")
            if not isinstance(text, str) or not isinstance(filename, str) or not filename.lower().endswith(SUFFIXES):
                reasons[name] = "source-content-integrity"
                continue
            if not text.strip() or len(text.encode("utf-8")) > MAX_BYTES or record.get("sha256") != digest(text):
                reasons[name] = "source-content-integrity"
                continue
            try:
                section_count = len(parse_sections(text, markdown=filename.lower().endswith(".md")))
            except KnowledgeError:
                reasons[name] = "source-content-integrity"
                continue
            if type(record.get("sectionCount")) is not int or section_count < 1 or record["sectionCount"] != section_count:
                reasons[name] = "source-content-integrity"
                continue
        declared = selected[name]["spec"].get("locale")
        if locale and declared and declared.split("-")[0].lower() != locale.split("-")[0].lower():
            reasons[name] = "source-other-locale"
            continue
        index = build_index(name, record["content"], filename=record["source"], spec=selected[name]["spec"])
        outcome = search(index, query, limit=limit, agent=agent, today=today, access=access)
        documents.extend(outcome["documents"])
        if outcome.get("reason") and not outcome["documents"]:
            reasons[name] = outcome["reason"]
    documents.sort(key=lambda doc: -doc["score"])
    for document in documents:  # conflicts that only show up across sources: approved sources must not disagree silently
        document.pop("conflict", None)
    kept = documents[: max(1, min(int(limit), MAX_RESULTS))]
    result: dict[str, Any] = {"documents": kept, "dataOnly": True}
    _conflict_guidance(result, find_conflicts(kept))
    if not result["documents"]:
        result["reason"] = next(iter(reasons.values()), "no-matching-section")
        result["sources"] = reasons
        if locale and "source-other-locale" in reasons.values():
            # Name the languages that ARE covered (declared tags only) so the coworker can say so or offer a person; it must never present
            # another language's approved text as if it were approved in the requested one (#239).
            result["availableLocales"] = sorted({str(item["spec"]["locale"]) for item in selected.values() if item.get("spec", {}).get("locale")})
            result["localeGuidance"] = (
                "No approved source covers the requested language. Do not translate another language's approved text and present it as approved "
                "in the requested one: say which languages are covered and answer in one of them, or offer a message or a person."
            )
    return result


def read_receipt(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Ledger sources for a search result: section IDs and content digests, never the text."""
    by_source: dict[str, list[dict[str, Any]]] = {}
    for document in result.get("documents", []):
        source_name = document["citation"].split("#", 1)[0].removeprefix("knowledge:")
        by_source.setdefault(source_name, []).append({"id": document["id"], "content": document["content"]})
    return [{"sourceId": f"knowledge:{name}", "records": records} for name, records in sorted(by_source.items())]

#!/usr/bin/env python3
"""Answer common enterprise DDQ questions from Model Garden's verified evidence registry.

The command is deliberately conservative. It never upgrades a capability into a certification
or compliance claim. Answers are generated from version-controlled evidence plus a Singapore
regulatory/guidance map and are flagged for review whenever a fact is deployment-, company-
or sector-specific.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import pathlib
import re
import sys
from collections import Counter
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSURANCE = ROOT / "assurance"
DEFAULT_CATALOG = ASSURANCE / "ddq-catalog.yaml"
DEFAULT_EVIDENCE = ASSURANCE / "evidence.yaml"
DEFAULT_SINGAPORE = ASSURANCE / "singapore.yaml"

SUPPORTED_STATUSES = {"implemented"}
TOKEN_RE = re.compile(r"[a-z0-9]+")


def load_yaml(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return value


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def _weighted_terms(text: str) -> Counter[str]:
    terms = tokenize(text)
    counts = Counter(terms)
    for left, right in zip(terms, terms[1:]):
        counts[f"{left} {right}"] += 2
    return counts


def score_question(query: str, item: dict[str, Any]) -> float:
    query_terms = _weighted_terms(query)
    candidate_text = " ".join(
        [str(item.get("question", ""))]
        + [str(keyword) for keyword in item.get("keywords", [])]
    )
    candidate_terms = _weighted_terms(candidate_text)
    overlap = sum(min(query_terms[t], candidate_terms[t]) for t in query_terms)
    if overlap == 0:
        return 0.0
    norm = math.sqrt(sum(query_terms.values()) * sum(candidate_terms.values()))
    score = overlap / norm if norm else 0.0
    lowered = query.lower()
    for keyword in item.get("keywords", []):
        keyword = str(keyword).lower().strip()
        if keyword and keyword in lowered:
            score += 0.18
    return score


def find_question(query: str, catalog: dict[str, Any], question_id: str | None = None) -> tuple[dict[str, Any] | None, float]:
    questions = catalog.get("questions", [])
    if not isinstance(questions, list):
        raise ValueError("catalog questions must be a list")
    if question_id:
        for item in questions:
            if item.get("id") == question_id:
                return item, 1.0
        return None, 0.0

    ranked = sorted(
        ((score_question(query, item), item) for item in questions),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if not ranked or ranked[0][0] < 0.08:
        return None, ranked[0][0] if ranked else 0.0
    return ranked[0][1], ranked[0][0]


def singapore_sources_for(item: dict[str, Any], control_ids: list[str], singapore: dict[str, Any]) -> list[str]:
    explicit = [str(source) for source in item.get("singapore_sources", [])]
    if explicit:
        return explicit
    inferred: list[str] = []
    for source_id, source in singapore.get("sources", {}).items():
        mapped = set(source.get("controls", []))
        if mapped.intersection(control_ids):
            inferred.append(source_id)
    return inferred


def answer_question(
    query: str,
    catalog: dict[str, Any],
    evidence: dict[str, Any],
    singapore: dict[str, Any],
    question_id: str | None = None,
) -> dict[str, Any]:
    item, score = find_question(query, catalog, question_id=question_id)
    if item is None:
        return {
            "matched": False,
            "question": query,
            "assurance_state": "manual_review",
            "answer": "No sufficiently close canonical DDQ question was found. Perform a manual security/privacy review rather than guessing.",
            "evidence": [],
            "singapore_guidance": [],
            "review_required": True,
            "match_score": round(score, 3),
        }

    controls_registry = evidence.get("controls", {})
    control_ids = [str(control_id) for control_id in item.get("controls", [])]
    statements: list[str] = []
    limitations: list[str] = []
    paths: list[str] = []
    statuses: dict[str, str] = {}

    for control_id in control_ids:
        control = controls_registry.get(control_id)
        if not isinstance(control, dict):
            statuses[control_id] = "missing_evidence"
            limitations.append(f"Evidence registry has no control named {control_id}.")
            continue
        status = str(control.get("status", "not_verified"))
        statuses[control_id] = status
        statement = str(control.get("statement", "")).strip()
        if statement:
            statements.append(statement)
        limitation = str(control.get("limitations", "")).strip()
        if limitation:
            limitations.append(limitation)
        for path in control.get("evidence", []):
            path = str(path)
            if path and path not in paths:
                paths.append(path)

    source_ids = singapore_sources_for(item, control_ids, singapore)
    source_details: list[dict[str, Any]] = []
    for source_id in source_ids:
        source = singapore.get("sources", {}).get(source_id, {})
        if not isinstance(source, dict):
            continue
        urls = source.get("urls") or ([source.get("url")] if source.get("url") else [])
        source_details.append(
            {
                "id": source_id,
                "name": source.get("name", source_id),
                "kind": source.get("kind"),
                "applicability": source.get("applicability"),
                "urls": [url for url in urls if url],
            }
        )

    review_required = bool(item.get("review_required")) or any(
        status not in SUPPORTED_STATUSES for status in statuses.values()
    )
    review_note = str(item.get("review_note", "")).strip()
    if review_note:
        limitations.append(review_note)

    answer = " ".join(statements).strip()
    if not answer:
        answer = "No verified Model Garden evidence statement is currently available for this question."
    if limitations:
        answer += " Limitations / review points: " + " ".join(dict.fromkeys(limitations))

    return {
        "matched": True,
        "id": item.get("id"),
        "category": item.get("category"),
        "canonical_question": item.get("question"),
        "question": query,
        "assurance_state": "review_required" if review_required else "supported",
        "control_status": statuses,
        "answer": answer,
        "evidence": paths,
        "singapore_guidance": source_details,
        "review_required": review_required,
        "match_score": round(score, 3),
    }


def render_markdown(result: dict[str, Any]) -> str:
    lines = [f"### {result.get('id', 'Manual review')} — {result.get('category', 'DDQ')}"]
    lines.append(f"**Question:** {result['question']}")
    lines.append(f"**Assurance state:** `{result['assurance_state']}`")
    lines.append("")
    lines.append(result["answer"])
    if result.get("evidence"):
        lines.append("")
        lines.append("**Evidence**")
        lines.extend(f"- `{path}`" for path in result["evidence"])
    if result.get("singapore_guidance"):
        lines.append("")
        lines.append("**Singapore mapping**")
        for source in result["singapore_guidance"]:
            lines.append(f"- {source['name']} ({source.get('kind') or 'reference'})")
            if source.get("applicability"):
                lines.append(f"  - Applicability: {source['applicability']}")
            for url in source.get("urls", []):
                lines.append(f"  - {url}")
    if result.get("review_required"):
        lines.append("")
        lines.append("**Human review required before sending this answer to a customer.**")
    return "\n".join(lines)


def answer_csv(input_path: pathlib.Path, output_path: pathlib.Path, catalog: dict[str, Any], evidence: dict[str, Any], singapore: dict[str, Any]) -> None:
    with input_path.open("r", encoding="utf-8-sig", newline="") as src:
        reader = csv.DictReader(src)
        if not reader.fieldnames or "question" not in reader.fieldnames:
            raise ValueError("input CSV must contain a 'question' column")
        rows = list(reader)

    fieldnames = list(reader.fieldnames) + [
        "model_garden_ddq_id",
        "assurance_state",
        "answer",
        "evidence",
        "singapore_guidance",
        "review_required",
        "match_score",
    ]
    with output_path.open("w", encoding="utf-8", newline="") as dst:
        writer = csv.DictWriter(dst, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            result = answer_question(row.get("question", ""), catalog, evidence, singapore)
            row.update(
                {
                    "model_garden_ddq_id": result.get("id", ""),
                    "assurance_state": result["assurance_state"],
                    "answer": result["answer"],
                    "evidence": "; ".join(result.get("evidence", [])),
                    "singapore_guidance": "; ".join(source["name"] for source in result.get("singapore_guidance", [])),
                    "review_required": str(bool(result.get("review_required"))).lower(),
                    "match_score": result.get("match_score", 0),
                }
            )
            writer.writerow(row)


def readiness(evidence: dict[str, Any], singapore: dict[str, Any]) -> dict[str, Any]:
    baseline = singapore.get("procurement_targets", {}).get("baseline", {}).get("controls", [])
    controls = evidence.get("controls", {})
    rows = []
    counts: Counter[str] = Counter()
    for control_id in baseline:
        control = controls.get(control_id, {})
        status = str(control.get("status", "missing_evidence"))
        counts[status] += 1
        rows.append(
            {
                "control": control_id,
                "status": status,
                "limitations": control.get("limitations", ""),
                "evidence": control.get("evidence", []),
            }
        )
    return {
        "jurisdiction": singapore.get("jurisdiction", "Singapore"),
        "counts": dict(counts),
        "controls": rows,
        "recommended_next_evidence": singapore.get("procurement_targets", {}).get("singapore_strong", {}).get("recommended_next_evidence", []),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=pathlib.Path, default=DEFAULT_CATALOG)
    parser.add_argument("--evidence", type=pathlib.Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--singapore", type=pathlib.Path, default=DEFAULT_SINGAPORE)
    sub = parser.add_subparsers(dest="command", required=True)

    answer = sub.add_parser("answer", help="answer one DDQ question")
    answer.add_argument("question")
    answer.add_argument("--id", dest="question_id", help="force a canonical DDQ id")
    answer.add_argument("--json", action="store_true", help="emit JSON instead of Markdown")

    batch = sub.add_parser("answer-file", help="answer a CSV containing a 'question' column")
    batch.add_argument("input", type=pathlib.Path)
    batch.add_argument("output", type=pathlib.Path)

    sub.add_parser("readiness", help="show evidence gaps against the baseline Singapore enterprise DDQ target")
    sub.add_parser("catalog", help="print the canonical DDQ question catalog")

    args = parser.parse_args()
    try:
        catalog = load_yaml(args.catalog)
        evidence = load_yaml(args.evidence)
        singapore = load_yaml(args.singapore)

        if args.command == "answer":
            result = answer_question(args.question, catalog, evidence, singapore, question_id=args.question_id)
            print(json.dumps(result, indent=2) if args.json else render_markdown(result))
            return 0
        if args.command == "answer-file":
            answer_csv(args.input, args.output, catalog, evidence, singapore)
            print(f"Wrote {args.output}")
            return 0
        if args.command == "readiness":
            print(json.dumps(readiness(evidence, singapore), indent=2))
            return 0
        if args.command == "catalog":
            print(json.dumps(catalog.get("questions", []), indent=2))
            return 0
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"DDQ assurance error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

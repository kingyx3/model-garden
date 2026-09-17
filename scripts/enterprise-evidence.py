#!/usr/bin/env python3
"""Generate a commit-scoped enterprise assurance evidence pack.

The pack combines Model Garden's canonical control registry, Singapore assurance map,
evidence freshness metadata and optional machine-generated scanner outputs. It is designed
for enterprise due diligence and internal review; it never upgrades a control status merely
because a scanner ran.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import subprocess
from collections import Counter
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSURANCE = ROOT / "assurance"
DEFAULT_EVIDENCE = ASSURANCE / "evidence.yaml"
DEFAULT_SINGAPORE = ASSURANCE / "singapore.yaml"
DEFAULT_METADATA = ASSURANCE / "evidence-metadata.yaml"
DEFAULT_SCAN_DIR = ROOT / ".enterprise-evidence" / "scans"
DEFAULT_OUTPUT_DIR = ROOT / ".enterprise-evidence"

SCAN_FILES = {
    "dependency_vulnerability_scan": "pip-audit.json",
    "python_security_scan": "bandit.json",
    "infrastructure_as_code_scan": "checkov.json",
    "repository_secret_scan": "detect-secrets.json",
    "python_sbom": "python-sbom.cdx.json",
}


def load_yaml(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return value


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def repository_version() -> str:
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def repository_commit() -> str:
    explicit = os.environ.get("GITHUB_SHA") or os.environ.get("MODEL_GARDEN_COMMIT")
    if explicit:
        return explicit
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        return result.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _scan_result(status: str, findings: int | None = None, **details: Any) -> dict[str, Any]:
    value: dict[str, Any] = {"status": status}
    if findings is not None:
        value["findings"] = findings
    value.update(details)
    return value


def summarize_pip_audit(data: Any) -> dict[str, Any]:
    dependencies: list[Any]
    if isinstance(data, dict):
        dependencies = data.get("dependencies", [])
    elif isinstance(data, list):
        dependencies = data
    else:
        return _scan_result("invalid_output")
    vulnerability_count = 0
    affected_dependencies = 0
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            continue
        vulns = dependency.get("vulns") or dependency.get("vulnerabilities") or []
        if isinstance(vulns, list) and vulns:
            affected_dependencies += 1
            vulnerability_count += len(vulns)
    return _scan_result(
        "pass" if vulnerability_count == 0 else "findings",
        vulnerability_count,
        dependencies_scanned=len(dependencies),
        affected_dependencies=affected_dependencies,
    )


def summarize_bandit(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return _scan_result("invalid_output")
    results = data.get("results", [])
    if not isinstance(results, list):
        results = []
    severities = Counter()
    confidences = Counter()
    for item in results:
        if not isinstance(item, dict):
            continue
        severities[str(item.get("issue_severity", "unknown")).lower()] += 1
        confidences[str(item.get("issue_confidence", "unknown")).lower()] += 1
    return _scan_result(
        "pass" if not results else "findings",
        len(results),
        severities=dict(severities),
        confidences=dict(confidences),
    )


def summarize_checkov(data: Any) -> dict[str, Any]:
    documents = data if isinstance(data, list) else [data]
    passed = failed = skipped = parsing_errors = 0
    valid = False
    for document in documents:
        if not isinstance(document, dict):
            continue
        valid = True
        summary = document.get("summary", {})
        if not isinstance(summary, dict):
            summary = {}
        passed += int(summary.get("passed", 0) or 0)
        failed += int(summary.get("failed", 0) or 0)
        skipped += int(summary.get("skipped", 0) or 0)
        parsing_errors += int(summary.get("parsing_errors", 0) or 0)
        if not summary:
            results = document.get("results", {})
            if isinstance(results, dict):
                passed += len(results.get("passed_checks", []) or [])
                failed += len(results.get("failed_checks", []) or [])
                skipped += len(results.get("skipped_checks", []) or [])
    if not valid:
        return _scan_result("invalid_output")
    findings = failed + parsing_errors
    return _scan_result(
        "pass" if findings == 0 else "findings",
        findings,
        passed=passed,
        failed=failed,
        skipped=skipped,
        parsing_errors=parsing_errors,
    )


def summarize_detect_secrets(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return _scan_result("invalid_output")
    results = data.get("results", {})
    if not isinstance(results, dict):
        return _scan_result("invalid_output")
    findings = 0
    files = 0
    for _, entries in results.items():
        if isinstance(entries, list) and entries:
            files += 1
            findings += len(entries)
    return _scan_result("pass" if findings == 0 else "findings", findings, files_with_findings=files)


def summarize_sbom(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return _scan_result("invalid_output")
    components = data.get("components", [])
    if not isinstance(components, list):
        components = []
    return _scan_result("generated", 0, components=len(components), bom_format=data.get("bomFormat"))


SCAN_SUMMARIZERS = {
    "dependency_vulnerability_scan": summarize_pip_audit,
    "python_security_scan": summarize_bandit,
    "infrastructure_as_code_scan": summarize_checkov,
    "repository_secret_scan": summarize_detect_secrets,
    "python_sbom": summarize_sbom,
}


def collect_scan_results(scan_dir: pathlib.Path) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for scan_id, filename in SCAN_FILES.items():
        path = scan_dir / filename
        if not path.exists():
            results[scan_id] = _scan_result("not_run", source=str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path))
            continue
        try:
            summary = SCAN_SUMMARIZERS[scan_id](load_json(path))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            summary = _scan_result("invalid_output", error=str(exc))
        summary["source"] = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
        results[scan_id] = summary
    return results


def control_rows(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    controls = evidence.get("controls", {})
    if not isinstance(controls, dict):
        raise ValueError("evidence controls must be a mapping")
    rows: list[dict[str, Any]] = []
    for control_id, control in controls.items():
        if not isinstance(control, dict):
            continue
        rows.append(
            {
                "id": str(control_id),
                "status": str(control.get("status", "not_verified")),
                "statement": str(control.get("statement", "")).strip(),
                "limitations": str(control.get("limitations", "")).strip(),
                "evidence": [str(item) for item in control.get("evidence", [])],
            }
        )
    return rows


def regulatory_rows(singapore: dict[str, Any]) -> list[dict[str, Any]]:
    sources = singapore.get("sources", {})
    if not isinstance(sources, dict):
        return []
    rows = []
    for source_id, source in sources.items():
        if not isinstance(source, dict):
            continue
        urls = source.get("urls") or ([source.get("url")] if source.get("url") else [])
        rows.append(
            {
                "id": str(source_id),
                "name": str(source.get("name", source_id)),
                "kind": str(source.get("kind", "reference")),
                "applicability": str(source.get("applicability", "")).strip(),
                "urls": [str(url) for url in urls if url],
            }
        )
    return rows


def _escape_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def build_pack(
    evidence: dict[str, Any],
    singapore: dict[str, Any],
    metadata: dict[str, Any],
    scans: dict[str, dict[str, Any]],
    *,
    generated_at: str,
    commit: str,
    version: str,
) -> tuple[str, dict[str, Any]]:
    controls = control_rows(evidence)
    counts = Counter(row["status"] for row in controls)
    gaps = [row for row in controls if row["status"] not in {"implemented"}]
    source_rows = regulatory_rows(singapore)

    lines = [
        "# Model Garden Enterprise Evidence Pack",
        "",
        "> This is a commit-scoped due-diligence evidence snapshot, not a certification or legal opinion. "
        "Deployment-, provider-, client-, legal-entity- and sector-specific facts still require review.",
        "",
        "## Snapshot",
        "",
        f"- Model Garden version: `{version}`",
        f"- Repository commit: `{commit}`",
        f"- Generated at: `{generated_at}`",
        f"- Jurisdiction map: `{singapore.get('jurisdiction', 'Singapore')}`",
        "",
        "## Control status summary",
        "",
        "| Evidence state | Controls |",
        "| --- | ---: |",
    ]
    for status in evidence.get("status_values", []):
        lines.append(f"| `{_escape_cell(status)}` | {counts.get(str(status), 0)} |")

    lines.extend([
        "",
        "## Automated evidence generated for this snapshot",
        "",
        "| Check | Result | Findings | Detail |",
        "| --- | --- | ---: | --- |",
    ])
    automated = metadata.get("automated_checks", {})
    for scan_id, scan in scans.items():
        config = automated.get(scan_id, {}) if isinstance(automated, dict) else {}
        label = config.get("tool", scan_id) if isinstance(config, dict) else scan_id
        findings = scan.get("findings", "")
        detail_parts = []
        for key, value in scan.items():
            if key in {"status", "findings", "source"}:
                continue
            detail_parts.append(f"{key}={value}")
        if scan.get("source"):
            detail_parts.append(f"source={scan['source']}")
        lines.append(
            f"| {_escape_cell(label)} | `{_escape_cell(scan.get('status', 'unknown'))}` | "
            f"{_escape_cell(findings)} | {_escape_cell('; '.join(detail_parts))} |"
        )

    lines.extend([
        "",
        "## Canonical control evidence",
        "",
        "| Control | State | Verified statement | Repository evidence | Limitations / review points |",
        "| --- | --- | --- | --- | --- |",
    ])
    for row in controls:
        lines.append(
            f"| `{_escape_cell(row['id'])}` | `{_escape_cell(row['status'])}` | "
            f"{_escape_cell(row['statement'])} | {_escape_cell(', '.join(row['evidence']))} | "
            f"{_escape_cell(row['limitations'])} |"
        )

    lines.extend([
        "",
        "## Evidence requiring follow-up",
        "",
        "The items below remain visible because CI or architecture alone is insufficient evidence.",
        "",
    ])
    manual = metadata.get("manual_evidence", {})
    if isinstance(manual, dict) and manual:
        lines.extend([
            "| Control | Current state | Owner | Review cadence | Expected evidence |",
            "| --- | --- | --- | --- | --- |",
        ])
        control_lookup = {row["id"]: row for row in controls}
        for control_id, item in manual.items():
            item = item if isinstance(item, dict) else {}
            current = control_lookup.get(str(control_id), {}).get("status", "not_registered")
            cadence = item.get("review_interval_days")
            cadence_text = f"{cadence} days" if cadence else "as required"
            lines.append(
                f"| `{_escape_cell(control_id)}` | `{_escape_cell(current)}` | "
                f"{_escape_cell(item.get('owner', 'unassigned'))} | {_escape_cell(cadence_text)} | "
                f"{_escape_cell(item.get('expected_evidence', ''))} |"
            )
    else:
        lines.append("No manual evidence registry is configured.")

    lines.extend([
        "",
        "## Singapore law, guidance and assurance map",
        "",
        "These references are mapped for due diligence. Their applicability and legal status differ; guidance and voluntary certifications are not presented as law.",
        "",
        "| Reference | Type | Applicability |",
        "| --- | --- | --- |",
    ])
    for source in source_rows:
        lines.append(
            f"| {_escape_cell(source['name'])} | `{_escape_cell(source['kind'])}` | {_escape_cell(source['applicability'])} |"
        )

    lines.extend([
        "",
        "## Current enterprise-readiness gaps",
        "",
    ])
    if not gaps:
        lines.append("No non-implemented controls are recorded. This does not imply certification or universal compliance.")
    else:
        for row in gaps:
            limitation = f" — {row['limitations']}" if row["limitations"] else ""
            lines.append(f"- `{row['id']}` — **{row['status']}**{limitation}")

    lines.extend([
        "",
        "## Use in a customer DDQ",
        "",
        "Use `scripts/ddq.py` to prepare question-level answers. Send only answers and evidence that have been reviewed for the actual customer, deployment, provider configuration, contract and sector. Retain this pack as the evidence snapshot supporting that review.",
        "",
    ])

    manifest = {
        "schema_version": 1,
        "generated_at": generated_at,
        "model_garden_version": version,
        "repository_commit": commit,
        "jurisdiction": singapore.get("jurisdiction", "Singapore"),
        "control_counts": dict(counts),
        "controls": controls,
        "scans": scans,
        "manual_evidence": metadata.get("manual_evidence", {}),
        "singapore_sources": source_rows,
        "non_implemented_controls": [row["id"] for row in gaps],
    }
    return "\n".join(lines), manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=pathlib.Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--singapore", type=pathlib.Path, default=DEFAULT_SINGAPORE)
    parser.add_argument("--metadata", type=pathlib.Path, default=DEFAULT_METADATA)
    parser.add_argument("--scan-dir", type=pathlib.Path, default=DEFAULT_SCAN_DIR)
    parser.add_argument("--output-dir", type=pathlib.Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--generated-at", default=None, help="override generation timestamp for deterministic tests")
    parser.add_argument("--commit", default=None, help="override repository commit")
    args = parser.parse_args()

    try:
        evidence = load_yaml(args.evidence)
        singapore = load_yaml(args.singapore)
        metadata = load_yaml(args.metadata)
        scans = collect_scan_results(args.scan_dir)
        generated_at = args.generated_at or utc_now()
        commit = args.commit or repository_commit()
        pack, manifest = build_pack(
            evidence,
            singapore,
            metadata,
            scans,
            generated_at=generated_at,
            commit=commit,
            version=repository_version(),
        )
        args.output_dir.mkdir(parents=True, exist_ok=True)
        pack_path = args.output_dir / "enterprise-evidence-pack.md"
        manifest_path = args.output_dir / "manifest.json"
        pack_path.write_text(pack + "\n", encoding="utf-8")
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(pack_path)
        print(manifest_path)
        return 0
    except (OSError, ValueError, yaml.YAMLError, json.JSONDecodeError) as exc:
        print(f"enterprise evidence error: {exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

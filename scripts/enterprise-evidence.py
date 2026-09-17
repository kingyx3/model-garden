#!/usr/bin/env python3
"""Validate assurance evidence and generate a reusable enterprise evidence pack.

The pack is intentionally conservative: repository evidence can prove implemented controls,
but it cannot manufacture legal compliance, certification, penetration-test, insurance or
provider-specific deployment facts.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sys
import uuid
from collections import Counter
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSURANCE = ROOT / "assurance"
EVIDENCE = ASSURANCE / "evidence.yaml"
SINGAPORE = ASSURANCE / "singapore.yaml"
VERSION = ROOT / "VERSION"

VALID_STATUSES = {
    "implemented",
    "implemented_live_proof_pending",
    "client_configured",
    "policy_required",
    "not_verified",
    "not_applicable",
}

TEXT_SUFFIXES = {
    ".cfg", ".conf", ".env", ".go", ".html", ".ini", ".java", ".js", ".json",
    ".md", ".py", ".rb", ".sh", ".tf", ".toml", ".ts", ".txt", ".xml", ".yaml", ".yml",
}
SECRET_PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github_pat": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "openai_key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b"),
}


def load_yaml(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return value


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def rel(path: pathlib.Path) -> str:
    return str(path.relative_to(ROOT)).replace(os.sep, "/")


def evidence_validation(evidence: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    controls = evidence.get("controls", {})
    if not isinstance(controls, dict) or not controls:
        errors.append("evidence registry has no controls mapping")
        controls = {}

    checked = []
    for control_id, control in controls.items():
        if not isinstance(control, dict):
            errors.append(f"{control_id}: control must be a mapping")
            continue
        status = str(control.get("status", ""))
        if status not in VALID_STATUSES:
            errors.append(f"{control_id}: unsupported status {status!r}")
        statement = str(control.get("statement", "")).strip()
        if not statement:
            errors.append(f"{control_id}: statement is required")
        paths = [str(item) for item in control.get("evidence", [])]
        missing = [path for path in paths if not (ROOT / path).exists()]
        if missing:
            errors.append(f"{control_id}: missing evidence path(s): {', '.join(missing)}")
        if status == "implemented" and not paths:
            errors.append(f"{control_id}: implemented controls require repository evidence")
        if status in {"not_verified", "client_configured"} and not str(control.get("limitations", "")).strip():
            warnings.append(f"{control_id}: {status} control should explain its limitation")
        checked.append({"control": control_id, "status": status, "evidence": paths, "missing": missing})

    return {"ok": not errors, "errors": errors, "warnings": warnings, "controls": checked}


def iter_text_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    ignored_parts = {".git", ".venv", "venv", "node_modules", "__pycache__", "assurance-output"}
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in ignored_parts for part in path.parts):
            continue
        if path.name.startswith(".env") and path.name not in {".env.example", ".env.sample"}:
            files.append(path)
        elif path.suffix.lower() in TEXT_SUFFIXES or path.name in {"Dockerfile", "Makefile"}:
            files.append(path)
    return files


def scan_secrets() -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    for path in iter_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for rule, pattern in SECRET_PATTERNS.items():
                if pattern.search(line):
                    findings.append({"rule": rule, "path": rel(path), "line": line_number})
    return {
        "scanner": "model-garden-high-confidence-secret-scan",
        "generated_at": utc_now(),
        "status": "pass" if not findings else "fail",
        "finding_count": len(findings),
        "findings": findings,
        "limitations": "High-confidence repository patterns only; this does not replace provider secret scanning or credential rotation.",
    }


def scan_iac() -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    terraform_files = sorted(ROOT.glob("infra/**/*.tf"))
    for path in terraform_files:
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if "0.0.0.0/0" in line or "::/0" in line:
                findings.append({
                    "rule": "public_ingress_or_egress_cidr",
                    "severity": "review",
                    "path": rel(path),
                    "line": line_number,
                })
    return {
        "scanner": "model-garden-terraform-guardrails",
        "generated_at": utc_now(),
        "status": "pass" if not findings else "review",
        "finding_count": len(findings),
        "findings": findings,
        "limitations": "Focused guardrails for the current Terraform baseline; terraform validate remains separate and this is not a full third-party IaC security assessment.",
    }


def parse_requirement(line: str) -> tuple[str, str | None] | None:
    line = line.strip()
    if not line or line.startswith("#") or line.startswith("-r "):
        return None
    match = re.match(r"^([A-Za-z0-9_.-]+)\s*(?:==\s*([^;\s]+))?", line)
    if not match:
        return None
    return match.group(1), match.group(2)


def build_sbom() -> dict[str, Any]:
    components: dict[str, dict[str, Any]] = {}
    for req_path in [ROOT / "requirements-runtime.txt", ROOT / "requirements-dev.txt"]:
        if not req_path.exists():
            continue
        for raw in req_path.read_text(encoding="utf-8").splitlines():
            parsed = parse_requirement(raw)
            if not parsed:
                continue
            name, version = parsed
            key = f"python:{name.lower()}:{version or 'range'}"
            components[key] = {
                "type": "library",
                "name": name,
                **({"version": version} if version else {}),
                "properties": [{"name": "model-garden:source", "value": rel(req_path)}],
            }

    for dockerfile in ROOT.rglob("Dockerfile"):
        if any(part in {".git", ".venv", "venv"} for part in dockerfile.parts):
            continue
        for raw in dockerfile.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^\s*FROM\s+([^\s]+)", raw, flags=re.IGNORECASE)
            if match:
                image = match.group(1)
                key = f"container:{image}"
                components[key] = {
                    "type": "container",
                    "name": image,
                    "properties": [{"name": "model-garden:source", "value": rel(dockerfile)}],
                }

    serial_seed = (VERSION.read_text(encoding="utf-8").strip() + "|" + "|".join(sorted(components))).encode()
    serial = uuid.UUID(hashlib.md5(serial_seed, usedforsecurity=False).hexdigest())
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "timestamp": utc_now(),
            "component": {
                "type": "application",
                "name": "model-garden",
                "version": VERSION.read_text(encoding="utf-8").strip(),
            },
        },
        "components": list(components.values()),
        "properties": [{
            "name": "model-garden:sbom-limitations",
            "value": "Repository-declared Python requirements and Docker base images only; transitive/runtime provider inventories require deployment evidence.",
        }],
    }


def load_optional_json(path: pathlib.Path | None) -> Any | None:
    if not path or not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def summarize_pip_audit(payload: Any | None) -> dict[str, Any]:
    if payload is None:
        return {"status": "not_run", "vulnerability_count": None}
    dependencies = payload.get("dependencies", []) if isinstance(payload, dict) else []
    count = 0
    affected = []
    for dependency in dependencies:
        vulns = dependency.get("vulns", []) if isinstance(dependency, dict) else []
        if vulns:
            count += len(vulns)
            affected.append({"name": dependency.get("name"), "version": dependency.get("version"), "count": len(vulns)})
    return {"status": "pass" if count == 0 else "fail", "vulnerability_count": count, "affected": affected}


def build_manifest(evidence: dict[str, Any], singapore: dict[str, Any], pip_audit: Any | None = None) -> dict[str, Any]:
    validation = evidence_validation(evidence)
    secret_scan = scan_secrets()
    iac_scan = scan_iac()
    status_counts = Counter(str(control.get("status", "missing")) for control in evidence.get("controls", {}).values())
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "model_garden_version": VERSION.read_text(encoding="utf-8").strip(),
        "commit": os.getenv("GITHUB_SHA") or os.getenv("MODEL_GARDEN_COMMIT") or "working-tree",
        "jurisdiction": singapore.get("jurisdiction", "Singapore"),
        "control_status_counts": dict(status_counts),
        "registry_validation": validation,
        "automated_checks": {
            "secret_scan": secret_scan,
            "terraform_guardrails": iac_scan,
            "dependency_vulnerability_scan": summarize_pip_audit(pip_audit),
        },
        "claim_boundary": "Automated repository evidence does not by itself establish legal compliance, certification, penetration-test status, insurance, provider residency or client-specific configuration.",
    }


def render_pack(evidence: dict[str, Any], singapore: dict[str, Any], manifest: dict[str, Any]) -> str:
    controls = evidence.get("controls", {})
    counts = manifest["control_status_counts"]
    lines = [
        "# Model Garden Enterprise Evidence Pack",
        "",
        f"Generated: {manifest['generated_at']}",
        f"Model Garden version: `{manifest['model_garden_version']}`",
        f"Source revision: `{manifest['commit']}`",
        "",
        "## Scope and claim boundary",
        "",
        "This pack summarizes version-controlled Model Garden security, privacy and AI-governance evidence for enterprise due diligence. It is an evidence index, not a blanket compliance attestation.",
        "",
        f"> {manifest['claim_boundary']}",
        "",
        "## Assurance snapshot",
        "",
        "| Evidence state | Controls |",
        "| --- | ---: |",
    ]
    for status in ["implemented", "implemented_live_proof_pending", "client_configured", "policy_required", "not_verified", "not_applicable"]:
        lines.append(f"| `{status}` | {counts.get(status, 0)} |")

    checks = manifest["automated_checks"]
    lines.extend([
        "",
        "## Automated repository checks",
        "",
        "| Check | Result | Detail |",
        "| --- | --- | --- |",
        f"| High-confidence secret scan | `{checks['secret_scan']['status']}` | {checks['secret_scan']['finding_count']} finding(s) |",
        f"| Terraform public-CIDR guardrails | `{checks['terraform_guardrails']['status']}` | {checks['terraform_guardrails']['finding_count']} review finding(s) |",
        f"| Python dependency vulnerability scan | `{checks['dependency_vulnerability_scan']['status']}` | {checks['dependency_vulnerability_scan']['vulnerability_count'] if checks['dependency_vulnerability_scan']['vulnerability_count'] is not None else 'CI evidence not supplied'} |",
        "",
        "The repository checks are inputs to assurance, not substitutes for independent testing or deployment-specific verification.",
        "",
        "## Control evidence",
        "",
        "| Control | State | Evidence | Review point |",
        "| --- | --- | --- | --- |",
    ])
    for control_id, control in controls.items():
        paths = ", ".join(f"`{path}`" for path in control.get("evidence", [])) or "—"
        limitation = str(control.get("limitations", "")).replace("\n", " ").replace("|", "\\|") or "—"
        lines.append(f"| `{control_id}` | `{control.get('status', 'not_verified')}` | {paths} | {limitation} |")

    lines.extend([
        "",
        "## Explicitly unresolved assurance",
        "",
    ])
    unresolved = [
        (control_id, control) for control_id, control in controls.items()
        if control.get("status") in {"implemented_live_proof_pending", "client_configured", "policy_required", "not_verified"}
    ]
    for control_id, control in unresolved:
        lines.append(f"- **{control_id} — `{control.get('status')}`:** {control.get('limitations') or control.get('statement')}")

    lines.extend([
        "",
        "## Singapore assurance references",
        "",
        "The detailed applicability and official URLs remain in `assurance/singapore.yaml`. Key source families are:",
        "",
    ])
    for source_id, source in singapore.get("sources", {}).items():
        lines.append(f"- **{source.get('name', source_id)}** — `{source.get('kind', 'reference')}`. {source.get('applicability', '')}")

    lines.extend([
        "",
        "## Customer-use rule",
        "",
        "Before sending this pack externally, review all `client_configured`, `not_verified`, sector-specific and provider-specific statements against the actual deployment and contract. Formal certifications and independent penetration-test reports must be attached separately only when current and in scope.",
        "",
        "Generated from `assurance/evidence.yaml` and `assurance/singapore.yaml`; do not hand-edit generated output as a second source of truth.",
    ])
    return "\n".join(lines) + "\n"


def write_json(path: pathlib.Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def generate_pack(output: pathlib.Path, pip_audit_path: pathlib.Path | None) -> dict[str, Any]:
    evidence = load_yaml(EVIDENCE)
    singapore = load_yaml(SINGAPORE)
    pip_audit = load_optional_json(pip_audit_path)
    manifest = build_manifest(evidence, singapore, pip_audit=pip_audit)
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "evidence-manifest.json", manifest)
    write_json(output / "secret-scan.json", manifest["automated_checks"]["secret_scan"])
    write_json(output / "terraform-guardrails.json", manifest["automated_checks"]["terraform_guardrails"])
    write_json(output / "sbom.cdx.json", build_sbom())
    (output / "enterprise-evidence-pack.md").write_text(render_pack(evidence, singapore, manifest), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify", help="validate the evidence registry and referenced repository evidence")
    pack = sub.add_parser("pack", help="generate the customer-reviewable enterprise evidence pack")
    pack.add_argument("--output", type=pathlib.Path, default=ROOT / "assurance-output")
    pack.add_argument("--pip-audit", type=pathlib.Path, help="optional pip-audit JSON to include in the manifest")
    scan = sub.add_parser("scan", help="run repository secret and Terraform guardrail scans")
    scan.add_argument("--json", action="store_true")

    args = parser.parse_args()
    try:
        evidence = load_yaml(EVIDENCE)
        if args.command == "verify":
            result = evidence_validation(evidence)
            print(json.dumps(result, indent=2))
            return 0 if result["ok"] else 1
        if args.command == "scan":
            result = {"secret_scan": scan_secrets(), "terraform_guardrails": scan_iac()}
            print(json.dumps(result, indent=2) if args.json else f"secret_scan={result['secret_scan']['status']} terraform_guardrails={result['terraform_guardrails']['status']}")
            return 1 if result["secret_scan"]["status"] == "fail" else 0
        if args.command == "pack":
            manifest = generate_pack(args.output, args.pip_audit)
            print(f"Wrote enterprise evidence pack to {args.output}")
            return 0 if manifest["registry_validation"]["ok"] and manifest["automated_checks"]["secret_scan"]["status"] != "fail" else 1
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(f"Enterprise evidence error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

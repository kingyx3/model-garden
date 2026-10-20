#!/usr/bin/env python3
"""Compare the `main` branch ruleset and CODEOWNERS with the enterprise governance target, and record the gap honestly (#45).

The target (docs/security.md, Repository governance) is a protected default branch that requires the validation checks, an
independent approving review, CODEOWNER review, resolved review threads and no routine administrative bypass. Those are GitHub
repository settings that code cannot change or prove, so this tool only *reads* a ruleset you export and reports what differs:

  gh api repos/OWNER/REPO/rulesets/RULESET_ID > ruleset.json
  python3 scripts/repo-governance-check.py --ruleset ruleset.json --codeowners .github/CODEOWNERS
  python3 scripts/repo-governance-check.py --ruleset ruleset.json --codeowners .github/CODEOWNERS \\
      --sole-maintainer --accepted-by founder@example.com

Findings are ``gaps`` (fix them), ``acceptedLimitations`` (only with ``--sole-maintainer``: independent review and CODEOWNER review
need a second qualified person; the operating limitation is recorded with who accepted it, never faked) and ``advisories``.
Exit 0 with no gaps, 1 with gaps, 2 when the input cannot be read. It makes no network call and changes nothing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
from collections.abc import Mapping
from typing import Any

REQUIRED_CONTEXTS = (
    "version-contract", "contracts-and-runtime", "hermes-compatibility",
    "terraform (infra/bootstrap/gcp)", "terraform (infra/docker-host/gcp)",
    "enterprise-evidence",
)
AUTHORITY_PATHS = ("/infra/", "/platform/", "/scripts/", "/contracts/", "/.github/", "/assurance/", "/evals/")
SOLE_MAINTAINER_LIMITATION = (
    "No second qualified maintainer exists, so an independent approving review and CODEOWNER review are not enforced on main. "
    "The enforced automated validation gates and the immutable release evidence stand in for them until a second maintainer exists; "
    "this is an operating limitation, not a control that is met."
)


class GovernanceError(ValueError):
    """Raised when the ruleset or CODEOWNERS input is not usable."""


def _rules(ruleset: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rules = ruleset.get("rules")
    if not isinstance(rules, list):
        raise GovernanceError("the ruleset has no rules list; export it with the rulesets API")
    return {str(rule.get("type")): rule.get("parameters") or {} for rule in rules if isinstance(rule, Mapping)}


def _targets_main(ruleset: Mapping[str, Any]) -> bool:
    conditions = ruleset.get("conditions") if isinstance(ruleset.get("conditions"), Mapping) else {}
    ref_name = conditions.get("ref_name") if isinstance(conditions.get("ref_name"), Mapping) else {}
    include = ref_name.get("include") or []
    return any(item in ("~DEFAULT_BRANCH", "~ALL", "refs/heads/main") for item in include)


def codeowner_paths(text: str) -> dict[str, list[str]]:
    """``{path pattern: [owners]}`` from a CODEOWNERS file (comments and blank lines skipped)."""
    owned: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        pattern, *owners = line.split()
        owned[pattern] = [owner for owner in owners if re.fullmatch(r"@[\w./-]+|[\w.+-]+@[\w.-]+", owner)]
    return owned


def check(ruleset: Any, codeowners: str, *, sole_maintainer: bool = False, accepted_by: str | None = None) -> dict[str, Any]:
    if not isinstance(ruleset, Mapping):
        raise GovernanceError("the ruleset must be a JSON object")
    if sole_maintainer and not (isinstance(accepted_by, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}", accepted_by)):
        raise GovernanceError("--sole-maintainer needs --accepted-by naming the person who accepts the operating limitation")
    rules = _rules(ruleset)
    gaps: list[dict[str, str]] = []
    accepted: list[dict[str, str]] = []
    advisories: list[dict[str, str]] = []

    def finding(bucket: list[dict[str, str]], key: str, message: str) -> None:
        bucket.append({"id": key, "message": message})

    def needs_second_person(key: str, message: str) -> None:
        finding(accepted if sole_maintainer else gaps, key, message)

    if ruleset.get("enforcement") != "active":
        finding(gaps, "ruleset-not-active", f"the ruleset's enforcement is {ruleset.get('enforcement')!r}, not 'active'")
    if not _targets_main(ruleset):
        finding(gaps, "ruleset-not-on-main", "the ruleset does not include the default branch")
    review = rules.get("pull_request")
    if review is None:
        finding(gaps, "no-pull-request-rule", "changes to main are not required to go through a pull request")
        review = {}
    if int(review.get("required_approving_review_count") or 0) < 1:
        needs_second_person("independent-review", "no approving review is required before merging to main")
    if review.get("require_code_owner_review") is not True:
        needs_second_person("codeowner-review", "CODEOWNER review is not required for the security, governance, infrastructure and runtime paths")
    if review.get("required_review_thread_resolution") is not True:
        finding(gaps, "review-threads", "review threads do not have to be resolved before merging")
    if review.get("require_last_push_approval") is not True:
        finding(advisories, "last-push-approval", "approval after the latest push is not required (consider it for high-risk changes)")
    checks = rules.get("required_status_checks")
    contexts = {str(item.get("context")) for item in (checks or {}).get("required_status_checks", []) if isinstance(item, Mapping)}
    if checks is None:
        finding(gaps, "no-required-checks", "no status checks are required before merging to main")
    for context in REQUIRED_CONTEXTS:
        if checks is not None and context not in contexts:
            finding(gaps, "missing-required-check", f"the required check {context!r} is not enforced")
    if checks is not None and checks.get("strict_required_status_checks_policy") is not True:
        finding(advisories, "up-to-date-branch", "branches need not be up to date with main before merging")
    for rule in ("non_fast_forward", "deletion"):
        if rule not in rules:
            finding(gaps, f"no-{rule.replace('_', '-')}-rule", f"the ruleset does not block {'force pushes' if rule == 'non_fast_forward' else 'deleting the branch'}")
    bypass = ruleset.get("bypass_actors") or []
    routine = [item for item in bypass if isinstance(item, Mapping) and item.get("bypass_mode") == "always"]
    if routine:
        finding(gaps, "routine-bypass", f"{len(routine)} actor(s) can bypass the ruleset at any time; limit bypass to documented break-glass use (pull-request-only bypass)")
    owned = codeowner_paths(codeowners)
    for path in AUTHORITY_PATHS:
        if not owned.get(path):
            finding(gaps, "codeowners-coverage", f"{path} has no CODEOWNERS entry with an owner")
    result: dict[str, Any] = {"ok": not gaps, "gaps": gaps, "acceptedLimitations": accepted, "advisories": advisories}
    if accepted:
        result["operatingLimitation"] = {"statement": SOLE_MAINTAINER_LIMITATION, "acceptedBy": accepted_by}
    return result


def record(result: Mapping[str, Any], *, commit: str | None = None, at: str | None = None) -> dict[str, Any]:
    """The check result as an evidence record with a SHA-256 over the whole of it."""
    if commit is not None and not re.fullmatch(r"[0-9a-f]{7,64}", commit):
        raise GovernanceError("commit must be a hexadecimal commit id")
    stamp = at or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {"schemaVersion": 1, "recordedAt": stamp, "commit": commit, "result": result}
    return {**body, "sha256": hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ruleset", required=True, type=pathlib.Path, help="the ruleset JSON from the rulesets API")
    parser.add_argument("--codeowners", type=pathlib.Path, default=pathlib.Path(".github/CODEOWNERS"))
    parser.add_argument("--sole-maintainer", action="store_true", help="record the missing second reviewer as an accepted operating limitation")
    parser.add_argument("--accepted-by", help="who accepts the operating limitation (required with --sole-maintainer)")
    parser.add_argument("--evidence", action="store_true", help="print an evidence record with a digest instead of the bare result")
    parser.add_argument("--commit")
    parser.add_argument("--at")
    args = parser.parse_args(argv)
    try:
        ruleset = json.loads(args.ruleset.read_text(encoding="utf-8"))
        owners = args.codeowners.read_text(encoding="utf-8")
        result = check(ruleset, owners, sole_maintainer=args.sole_maintainer, accepted_by=args.accepted_by)
        print(json.dumps(record(result, commit=args.commit, at=args.at) if args.evidence else result, indent=2, sort_keys=True))
    except (GovernanceError, OSError, ValueError) as exc:
        print(f"Repository governance check failed: {exc}", file=sys.stderr)
        return 2
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

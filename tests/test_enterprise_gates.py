"""Credential-free checks for the host rebuild inputs (#46) and the repository governance evidence (#45)."""
from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bom_tool = _load("host_bom_under_test", "scripts/host-bom.py")
governance = _load("repo_governance_under_test", "scripts/repo-governance-check.py")

VERSIONS = {"docker-ce": "5:27.3.1-1~debian.12~bookworm", "docker-ce-cli": "5:27.3.1-1~debian.12~bookworm", "containerd.io": "1.7.22-1",
            "docker-buildx-plugin": "0.17.1-1~debian.12~bookworm", "docker-compose-plugin": "2.29.7-1~debian.12~bookworm"}
IMAGE_ID = "1234567890123456789"
PINNED = {"schemaVersion": 1, "image": {"name": "debian-12-bookworm-v20260101", "id": IMAGE_ID, "selfLink": "x", "pinned": True}, "docker": VERSIONS}
UNPINNED = {"schemaVersion": 1, "image": {"name": "debian-12-bookworm-v20261001", "id": "999999999999", "selfLink": "x", "pinned": False},
            "docker": {name: "" for name in VERSIONS}}
DPKG = "".join(f"{name}\t{version}\n" for name, version in VERSIONS.items())
OS_RELEASE = 'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nVERSION_CODENAME=bookworm\nID=debian\n'


def observed(**overrides):
    value = bom_tool.collect(dpkg_output=DPKG, os_release=OS_RELEASE, image_id=IMAGE_ID)
    value.update(overrides)
    return value


class HostBomTests(unittest.TestCase):
    def test_collect_reads_only_the_docker_packages_the_codename_and_the_image_id(self):
        got = bom_tool.collect(dpkg_output=DPKG + "bash\t5.2\nevil\t$(rm -rf)\n", os_release=OS_RELEASE, image_id=IMAGE_ID)
        self.assertEqual(got["docker"], VERSIONS)
        self.assertEqual((got["osCodename"], got["imageId"]), ("bookworm", IMAGE_ID))

    def test_collect_runs_a_fixed_dpkg_query_without_a_shell_when_given_nothing(self):
        seen = []
        got = bom_tool.collect(os_release=OS_RELEASE, run=lambda argv: seen.append(argv) or DPKG)
        self.assertEqual(got["docker"], VERSIONS)
        self.assertEqual(seen[0][0], "dpkg-query")
        self.assertEqual(seen[0][3:], list(bom_tool.DOCKER_PACKAGES))

    def test_a_malformed_image_id_or_junk_versions_are_not_accepted(self):
        with self.assertRaises(bom_tool.BomError):
            bom_tool.collect(dpkg_output=DPKG, os_release=OS_RELEASE, image_id="debian-12; rm -rf /")
        self.assertEqual(bom_tool.parse_dpkg("docker-ce\t5:27;rm\ncontainerd.io\t1.7.22-1\n"), {"containerd.io": "1.7.22-1"})
        self.assertIsNone(bom_tool.parse_os_release("VERSION_CODENAME=Bad Name\n"))

    def test_a_host_that_matches_a_fully_pinned_bom_is_reproducible(self):
        result = bom_tool.verify(PINNED, observed(), require_pinned=True)
        self.assertEqual((result["matches"], result["reproducible"], result["ok"], result["mismatches"], result["unpinned"]), (True, True, True, [], []))

    def test_a_wrong_or_missing_package_and_a_different_image_are_each_named(self):
        got = observed(docker={**VERSIONS, "docker-ce": "5:28.0.0-1~debian.12~bookworm", "containerd.io": None}, imageId="42424242")
        del got["docker"]["containerd.io"]
        result = bom_tool.verify(PINNED, got)
        self.assertFalse(result["ok"])
        text = " ".join(result["mismatches"])
        self.assertIn("docker-ce: approved 5:27.3.1", text)
        self.assertIn("containerd.io: approved 1.7.22-1 but it is not installed", text)
        self.assertIn("image: host booted from 42424242", text)

    def test_a_pinned_image_with_no_observed_image_id_cannot_pass(self):
        got = bom_tool.collect(dpkg_output=DPKG, os_release=OS_RELEASE)
        result = bom_tool.verify(PINNED, got)
        self.assertFalse(result["ok"])
        self.assertIn("names no image id", result["mismatches"][0])

    def test_unpinned_inputs_are_reported_with_what_the_host_has_and_only_fail_when_asked(self):
        result = bom_tool.verify(UNPINNED, observed())
        self.assertEqual((result["matches"], result["reproducible"], result["ok"]), (True, False, True))
        self.assertEqual(len(result["unpinned"]), 6)
        self.assertTrue(any("docker-ce: no approved version (host has 5:27.3.1" in line for line in result["unpinned"]))
        self.assertFalse(bom_tool.verify(UNPINNED, observed(), require_pinned=True)["ok"])

    def test_a_partly_pinned_bom_is_not_reproducible(self):
        bom = copy.deepcopy(PINNED)
        bom["docker"]["docker-compose-plugin"] = ""
        result = bom_tool.verify(bom, observed())
        self.assertEqual((result["matches"], result["reproducible"]), (True, False))
        self.assertEqual(len(result["unpinned"]), 1)

    def test_malformed_inputs_are_refused(self):
        for bad in ({}, {"schemaVersion": 2}, {**PINNED, "docker": {"curl": "8.0"}}, {**PINNED, "docker": {"docker-ce": "$(x)"}},
                    {**PINNED, "image": "debian"}, {**PINNED, "image": {**PINNED["image"], "id": "abc"}}, None, []):
            with self.assertRaises(bom_tool.BomError, msg=str(bad)[:60]):
                bom_tool.verify(bad, observed())
        for bad in ({}, {"schemaVersion": 1}, None):
            with self.assertRaises(bom_tool.BomError):
                bom_tool.verify(PINNED, bad)

    def test_the_evidence_record_is_digested_and_changes_with_any_input(self):
        first = bom_tool.evidence(PINNED, observed(), commit="abc1234", at="2026-10-17T00:00:00Z")
        again = bom_tool.evidence(PINNED, observed(), commit="abc1234", at="2026-10-17T00:00:00Z")
        self.assertEqual(first["sha256"], again["sha256"])
        self.assertTrue(first["result"]["ok"])
        self.assertIn("does not make it bit-for-bit reproducible", first["limits"])
        other = bom_tool.evidence(PINNED, observed(imageId="1"), commit="abc1234", at="2026-10-17T00:00:00Z")
        self.assertNotEqual(first["sha256"], other["sha256"])
        with self.assertRaises(bom_tool.BomError):
            bom_tool.evidence(PINNED, observed(), commit="not a commit")

    def test_the_command_line_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "bom.json").write_text(json.dumps(PINNED))
            (root / "unpinned.json").write_text(json.dumps(UNPINNED))
            (root / "observed.json").write_text(json.dumps(observed()))
            (root / "dpkg.txt").write_text(DPKG)
            (root / "os-release").write_text(OS_RELEASE)

            def run(*args):
                return subprocess.run([sys.executable, str(ROOT / "scripts" / "host-bom.py"), *args], capture_output=True, text=True, timeout=60)

            collected = run("collect", "--dpkg", str(root / "dpkg.txt"), "--os-release", str(root / "os-release"), "--image-id", IMAGE_ID)
            self.assertEqual(json.loads(collected.stdout)["docker"], VERSIONS)
            self.assertEqual(run("verify", "--bom", str(root / "bom.json"), "--observed", str(root / "observed.json"), "--require-pinned").returncode, 0)
            self.assertEqual(run("verify", "--bom", str(root / "unpinned.json"), "--observed", str(root / "observed.json")).returncode, 0)
            self.assertEqual(run("verify", "--bom", str(root / "unpinned.json"), "--observed", str(root / "observed.json"), "--require-pinned").returncode, 1)
            self.assertEqual(run("evidence", "--bom", str(root / "bom.json"), "--observed", str(root / "observed.json"), "--commit", "abc1234").returncode, 0)
            missing = run("verify", "--bom", str(root / "none.json"), "--observed", str(root / "observed.json"))
            self.assertEqual(missing.returncode, 2)
            self.assertNotIn("Traceback", missing.stderr)


class TerraformContractTests(unittest.TestCase):
    """terraform is not run here (CI validates it); these pin the contract the tool above depends on."""

    @classmethod
    def setUpClass(cls):
        cls.main = (ROOT / "infra" / "docker-host" / "gcp" / "main.tf").read_text(encoding="utf-8")

    def test_the_defaults_keep_todays_behaviour(self):
        self.assertRegex(self.main, r'variable "host_image" \{[^}]*default\s+= ""')
        self.assertRegex(self.main, r'variable "docker_package_versions" \{[^}]*default\s+= \{\}')
        self.assertIn('family  = var.host_image == "" ? "debian-12" : null', self.main)

    def test_every_docker_package_can_be_pinned_and_only_those(self):
        packages = ["docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin"]
        self.assertEqual(list(bom_tool.DOCKER_PACKAGES), packages)
        for name in packages:
            self.assertEqual(self.main.count(f'"{name}"'), 2, name)  # the list and the validation, nothing else
        self.assertIn("apt-get install -y ${local.docker_install}", self.main)
        self.assertNotIn("apt-get install -y docker-ce docker-ce-cli", self.main)

    def test_the_bom_is_recorded_on_the_instance_and_as_an_output(self):
        self.assertIn("model-garden-host-bom    = jsonencode(local.host_bom)", self.main)
        self.assertRegex(self.main, r'output "host_bom" \{[^}]*value\s+= local\.host_bom')
        for field in ("schemaVersion", "pinned", "image_id", "selfLink", "name"):
            self.assertIn(field, self.main)

    def test_terraform_bom_shape_is_what_the_checker_accepts(self):
        rendered = {"schemaVersion": 1, "image": {"name": "debian-12-bookworm-v20260101", "id": IMAGE_ID, "selfLink": "https://x", "pinned": True},
                    "docker": {name: "" for name in bom_tool.DOCKER_PACKAGES}}
        self.assertEqual(bom_tool.check_bom(rendered), rendered)

    def test_the_client_workflow_passes_the_approved_inputs_and_records_the_bom(self):
        workflow = (ROOT / "templates" / "client-workflows" / "gcp-keyless.yml").read_text(encoding="utf-8")
        self.assertIn("-var=\"host_image=${{ vars.MODEL_GARDEN_HOST_IMAGE || '' }}\"", workflow)
        self.assertIn("TF_VAR_docker_package_versions='${{ vars.MODEL_GARDEN_DOCKER_PACKAGE_VERSIONS || '{}' }}'", workflow)
        self.assertIn("output -json host_bom", workflow)
        yaml.safe_load(workflow.replace("__CLIENT_SLUG__", "acme").replace("__AGENT_SLUG__", "receptionist"))


def ruleset(**changes):
    base = {
        "enforcement": "active", "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "bypass_actors": [],
        "rules": [
            {"type": "pull_request", "parameters": {"required_approving_review_count": 1, "require_code_owner_review": True,
                                                    "required_review_thread_resolution": True, "require_last_push_approval": True}},
            {"type": "required_status_checks", "parameters": {"strict_required_status_checks_policy": True,
                                                              "required_status_checks": [{"context": c} for c in governance.REQUIRED_CONTEXTS]}},
            {"type": "non_fast_forward"}, {"type": "deletion"},
        ],
    }
    base.update(changes)
    return base


def pull_request(document, **params):
    document = copy.deepcopy(document)
    next(rule for rule in document["rules"] if rule["type"] == "pull_request")["parameters"].update(params)
    return document


CODEOWNERS = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")


class GovernanceTests(unittest.TestCase):
    def ids(self, result, key="gaps"):
        return sorted({item["id"] for item in result[key]})

    def test_a_ruleset_at_the_target_has_no_gaps(self):
        result = governance.check(ruleset(), CODEOWNERS)
        self.assertEqual((result["ok"], result["gaps"], result["acceptedLimitations"], result["advisories"]), (True, [], [], []))

    def test_todays_shape_reports_each_gap(self):
        weak = pull_request(ruleset(bypass_actors=[{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}]),
                            required_approving_review_count=0, require_code_owner_review=False, required_review_thread_resolution=False,
                            require_last_push_approval=False)
        result = governance.check(weak, CODEOWNERS)
        self.assertFalse(result["ok"])
        self.assertEqual(self.ids(result), ["codeowner-review", "independent-review", "review-threads", "routine-bypass"])
        self.assertEqual(self.ids(result, "advisories"), ["last-push-approval"])

    def test_a_sole_maintainer_records_the_missing_reviewer_as_a_named_limitation_not_a_pass(self):
        weak = pull_request(ruleset(), required_approving_review_count=0, require_code_owner_review=False)
        result = governance.check(weak, CODEOWNERS, sole_maintainer=True, accepted_by="founder@example.com")
        self.assertTrue(result["ok"])
        self.assertEqual(self.ids(result, "acceptedLimitations"), ["codeowner-review", "independent-review"])
        self.assertEqual(result["operatingLimitation"]["acceptedBy"], "founder@example.com")
        self.assertIn("not a control that is met", result["operatingLimitation"]["statement"])
        with self.assertRaises(governance.GovernanceError):
            governance.check(weak, CODEOWNERS, sole_maintainer=True)
        with self.assertRaises(governance.GovernanceError):
            governance.check(weak, CODEOWNERS, sole_maintainer=True, accepted_by="x y; z")

    def test_a_sole_maintainer_still_has_to_fix_what_one_person_can_fix(self):
        weak = pull_request(ruleset(bypass_actors=[{"bypass_mode": "always"}]), required_approving_review_count=0, required_review_thread_resolution=False)
        result = governance.check(weak, CODEOWNERS, sole_maintainer=True, accepted_by="founder")
        self.assertFalse(result["ok"])
        self.assertEqual(self.ids(result), ["review-threads", "routine-bypass"])

    def test_pull_request_only_bypass_is_break_glass_and_allowed(self):
        document = ruleset(bypass_actors=[{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}])
        self.assertTrue(governance.check(document, CODEOWNERS)["ok"])

    def test_missing_required_checks_a_missing_rule_and_an_inactive_ruleset_are_gaps(self):
        document = ruleset(enforcement="evaluate")
        document["rules"] = [rule for rule in document["rules"] if rule["type"] not in ("non_fast_forward", "deletion")]
        checks = next(rule for rule in document["rules"] if rule["type"] == "required_status_checks")
        checks["parameters"]["required_status_checks"] = [{"context": "version-contract"}]
        result = governance.check(document, CODEOWNERS)
        self.assertEqual(self.ids(result), ["missing-required-check", "no-deletion-rule", "no-non-fast-forward-rule", "ruleset-not-active"])
        self.assertEqual(sum(item["id"] == "missing-required-check" for item in result["gaps"]), 4)
        none = governance.check({**ruleset(), "rules": []}, CODEOWNERS)
        self.assertIn("no-pull-request-rule", self.ids(none))
        self.assertIn("no-required-checks", self.ids(none))

    def test_a_ruleset_for_another_branch_is_a_gap(self):
        document = ruleset(conditions={"ref_name": {"include": ["refs/heads/release"], "exclude": []}})
        self.assertIn("ruleset-not-on-main", self.ids(governance.check(document, CODEOWNERS)))

    def test_codeowners_must_cover_every_authority_path(self):
        self.assertTrue(governance.check(ruleset(), CODEOWNERS)["ok"])
        thin = "/infra/ @kingyx3\n/platform/ @kingyx3\n/scripts/\n# /contracts/ @kingyx3\n"
        result = governance.check(ruleset(), thin)
        missing = {item["message"].split()[0] for item in result["gaps"] if item["id"] == "codeowners-coverage"}
        self.assertEqual(missing, {"/scripts/", "/contracts/", "/.github/", "/assurance/", "/evals/"})

    def test_this_repositorys_codeowners_covers_the_authority_paths(self):
        owned = governance.codeowner_paths(CODEOWNERS)
        for path in governance.AUTHORITY_PATHS:
            self.assertTrue(owned.get(path), path)

    def test_bad_input_is_refused_and_the_evidence_record_is_digested(self):
        for bad in (None, [], "x"):
            with self.assertRaises(governance.GovernanceError):
                governance.check(bad, CODEOWNERS)
        with self.assertRaises(governance.GovernanceError):
            governance.check({"enforcement": "active", "rules": "no"}, CODEOWNERS)
        first = governance.record(governance.check(ruleset(), CODEOWNERS), commit="abc1234", at="2026-10-17T00:00:00Z")
        second = governance.record(governance.check(ruleset(), CODEOWNERS), commit="abc1234", at="2026-10-17T00:00:00Z")
        self.assertEqual(first["sha256"], second["sha256"])
        with self.assertRaises(governance.GovernanceError):
            governance.record({}, commit="nope")

    def test_the_command_line_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            good, weak = root / "good.json", root / "weak.json"
            good.write_text(json.dumps(ruleset()))
            weak.write_text(json.dumps(pull_request(ruleset(), required_approving_review_count=0)))

            def run(*args):
                return subprocess.run([sys.executable, str(ROOT / "scripts" / "repo-governance-check.py"), "--codeowners", str(ROOT / ".github" / "CODEOWNERS"), *args],
                                      capture_output=True, text=True, timeout=60)

            self.assertEqual(run("--ruleset", str(good)).returncode, 0)
            self.assertEqual(run("--ruleset", str(weak)).returncode, 1)
            accepted = run("--ruleset", str(weak), "--sole-maintainer", "--accepted-by", "founder@example.com", "--evidence", "--commit", "abc1234")
            self.assertEqual(accepted.returncode, 0)
            self.assertEqual(json.loads(accepted.stdout)["result"]["operatingLimitation"]["acceptedBy"], "founder@example.com")
            missing = run("--ruleset", str(root / "none.json"))
            self.assertEqual(missing.returncode, 2)
            self.assertNotIn("Traceback", missing.stderr)


class AssuranceRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evidence = yaml.safe_load((ROOT / "assurance" / "evidence.yaml").read_text(encoding="utf-8"))["controls"]
        cls.metadata = yaml.safe_load((ROOT / "assurance" / "evidence-metadata.yaml").read_text(encoding="utf-8"))

    def test_both_controls_are_registered_conservatively_with_real_evidence_paths(self):
        host, repo = self.evidence["host_reproducibility"], self.evidence["repository_governance"]
        self.assertEqual((host["status"], repo["status"]), ("implemented_live_proof_pending", "policy_required"))
        for control in (host, repo):
            self.assertTrue(control["limitations"].strip())
            for path in control["evidence"]:
                self.assertTrue((ROOT / path).exists(), path)

    def test_the_manual_evidence_each_needs_has_an_owner_and_a_review_interval(self):
        for key in ("host_reproducibility", "repository_governance"):
            entry = self.metadata["manual_evidence"][key]
            self.assertTrue(entry["owner"] and entry["expected_evidence"])
            self.assertGreater(entry["review_interval_days"], 0)

    def test_the_security_pack_states_the_ledger_export_retention_default(self):
        security = (ROOT / "docs" / "security.md").read_text(encoding="utf-8")
        variable = (ROOT / "infra" / "docker-host" / "gcp" / "ledger-export.tf").read_text(encoding="utf-8")
        default = re.search(r'variable "export_retention_days" \{.*?default\s+= (\d+)', variable, re.S).group(1)
        self.assertIn(f"{default} days", security)
        self.assertIn("export_retention_days", security)


if __name__ == "__main__":
    unittest.main()

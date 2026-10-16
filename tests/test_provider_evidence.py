"""Credential-free checks for the per-provider evidence manifest and the go-live gate."""
from __future__ import annotations

import contextlib
import copy
import datetime as dt
import hashlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


evidence = _load("provider_evidence_under_test", "platform/core/provider_evidence.py")
cli = _load("provider_evidence_cli_under_test", "scripts/provider-evidence.py")
NOW = dt.datetime(2026, 10, 16, 12, 0, tzinfo=dt.timezone.utc)
SCOPES = ["Calendars.Read", "Calendars.ReadWrite"]


def provider(name, *, status="built-credential-free", tier=1, method="native", adapter=None, scopes=True, family="calendar"):
    entry = {"id": name, "name": name.title(), "family": family, "method": method, "tier": tier, "status": status,
             "tools": ["calendar.availability", "calendar.book"], "issue": 66}
    if adapter:
        entry["adapter"] = adapter
    if scopes:
        entry["scopeByTool"] = {"calendar.availability": [SCOPES[0]], "calendar.book": [SCOPES[1]]}
    return entry


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        for directory in ("contracts/v1", "platform/connectors", "platform/core", "tests", "assurance/provider-proof"):
            (self.root / directory).mkdir(parents=True)
        (self.root / "VERSION").write_text("9.9.9\n", encoding="utf-8")
        (self.root / "platform/connectors/cal.py").write_text("# adapter v1\n", encoding="utf-8")
        (self.root / "platform/core/builtin.py").write_text("# builtin\n", encoding="utf-8")
        (self.root / "tests/test_cal.py").write_text(
            'load("platform/connectors/cal.py")\n\nclass T:\n    def test_a(self): pass\n    def test_b(self): pass\n', encoding="utf-8")
        (self.root / "tests/test_builtin.py").write_text('load("platform/core/builtin.py")\n    def test_one(self): pass\n', encoding="utf-8")
        self.providers = [
            provider("cal", adapter="platform/connectors/cal.py"),
            provider("builtin", tier=0, method="built-in", adapter="platform/core/builtin.py", scopes=False),
            provider("untested", adapter="platform/connectors/untested.py"),
            provider("future", status="planned"),
        ]
        (self.root / "platform/connectors/untested.py").write_text("# nobody tests me\n", encoding="utf-8")
        self.write_registry()

    def write_registry(self, providers=None) -> None:
        (self.root / "contracts/v1/integrations.json").write_text(json.dumps({
            "apiVersion": "modelgarden.ai/v1", "kind": "IntegrationRegistry", "registryVersion": "1.0.0",
            "providers": providers if providers is not None else self.providers}), encoding="utf-8")

    def proof(self, name="cal", **overrides) -> dict:
        adapter = self.root / "platform/connectors/cal.py"
        value = {
            "schemaVersion": 1, "provider": name, "provedAt": "2026-10-01T09:00:00Z", "expiresAt": "2027-01-01T09:00:00Z",
            "adapterSha256": hashlib.sha256(adapter.read_bytes()).hexdigest(), "scopes": SCOPES, "recordedBy": "jordan@mg.example",
            "evidence": ["run:LT-2041", "artefact:calendar-booking-redacted.png"], "redacted": True,
            "limitations": ["tested with one tenant"],
        }
        value.update(overrides)
        (self.root / f"assurance/provider-proof/{name}.json").write_text(json.dumps(value), encoding="utf-8")
        return value

    def entry(self, name="cal", now=NOW) -> dict:
        return next(item for item in evidence.manifest(self.root, now=now)["providers"] if item["id"] == name)

    def set_status(self, name, status, **extra):
        for item in self.providers:
            if item["id"] == name:
                item["status"] = status
                item.update(extra)
        self.write_registry()


class ManifestTests(Case):
    def test_each_provider_carries_adapter_digest_tests_scopes_and_the_release(self):
        data = evidence.manifest(self.root, now=NOW)
        self.assertEqual((data["kind"], data["platformRelease"], data["registryVersion"], data["generatedAt"]),
                         ("ProviderEvidenceManifest", "9.9.9", "1.0.0", "2026-10-16T12:00:00Z"))
        cal = self.entry("cal")
        self.assertEqual(cal["adapter"], {"path": "platform/connectors/cal.py", "sha256": hashlib.sha256(b"# adapter v1\n").hexdigest()})
        self.assertEqual(cal["tests"], {"files": ["test_cal.py"], "tests": 2})
        self.assertEqual(cal["scopes"], SCOPES)
        self.assertEqual(data["summary"]["providers"], 4)

    def test_a_tier_one_provider_without_proof_is_blocked_and_says_why(self):
        cal = self.entry()
        self.assertEqual((cal["liveProof"]["state"], cal["goLive"]["allowed"]), ("none", False))
        self.assertEqual(cal["goLive"]["blockers"], ["no retained live tenant proof"])
        self.assertIn("no live tenant proof is retained", cal["limitations"])

    def test_a_provider_with_no_adapter_tests_is_blocked_even_with_proof(self):
        untested = self.entry("untested")
        self.assertIn("no automated adapter tests found", untested["goLive"]["blockers"])
        self.assertFalse(untested["goLive"]["allowed"])

    def test_a_tier_zero_provider_needs_tests_but_no_tenant_proof_and_keeps_its_limitation(self):
        builtin = self.entry("builtin")
        self.assertEqual((builtin["goLive"], builtin["liveProof"]["state"]), ({"allowed": True, "blockers": []}, "none"))
        self.assertIn("no deployment evidence is retained for this built-in provider", builtin["limitations"])

    def test_planned_and_in_development_providers_are_blocked_without_an_adapter(self):
        self.assertIn("connector is planned: no shipped adapter", self.entry("future")["goLive"]["blockers"])
        self.set_status("cal", "in-development")
        cal = self.entry()
        self.assertIn("connector is in-development: no shipped adapter", cal["goLive"]["blockers"])
        self.assertIn("adapter is in development and is not served by the governed sidecar", cal["limitations"])

    def test_the_manifest_never_holds_proof_evidence_references_or_the_recorder(self):
        self.proof()
        blob = json.dumps(evidence.manifest(self.root, now=NOW))
        for private in ("run:LT-2041", "calendar-booking", "jordan@mg.example"):
            self.assertNotIn(private, blob)


class ProofTests(Case):
    def test_a_valid_proof_for_the_current_adapter_unblocks_a_live_claim_only_when_the_registry_says_live(self):
        self.proof()
        cal = self.entry()
        self.assertEqual(cal["liveProof"], {"state": "valid", "provedAt": "2026-10-01T09:00:00Z", "expiresAt": "2027-01-01T09:00:00Z"})
        self.assertEqual(cal["goLive"]["blockers"], ["the registry still says built-credential-free; update it to live after review"])
        self.assertIn("tested with one tenant", cal["limitations"])
        self.set_status("cal", "live", proof="assurance/provider-proof/cal.json")
        self.assertEqual(self.entry()["goLive"], {"allowed": True, "blockers": []})

    def test_an_expired_proof_blocks_go_live_but_is_not_a_structural_error(self):
        self.proof(expiresAt="2026-10-10T00:00:00Z")
        self.set_status("cal", "live", proof="assurance/provider-proof/cal.json")
        cal = self.entry()
        self.assertEqual(cal["liveProof"]["state"], "expired")
        self.assertIn("live tenant proof expired at 2026-10-10T00:00:00Z", cal["goLive"]["blockers"])
        self.assertEqual(evidence.check(self.root, now=NOW), [])

    def test_a_proof_stops_counting_when_the_adapter_it_proved_changes(self):
        self.proof()
        (self.root / "platform/connectors/cal.py").write_text("# adapter v2: new behaviour\n", encoding="utf-8")
        cal = self.entry()
        self.assertEqual(cal["liveProof"]["state"], "invalid")
        self.assertIn("proven again", " ".join(cal["goLive"]["blockers"]))

    def test_malformed_or_secret_bearing_proofs_are_invalid(self):
        cases = {
            "an extra field (a tenant or token)": {"tenantId": "acme-prod"},
            "another provider": {"provider": "other"},
            "not redacted": {"redacted": False},
            "naive time": {"provedAt": "2026-10-01T09:00:00"},
            "expires before it was proved": {"expiresAt": "2026-09-01T00:00:00Z"},
            "proved in the future": {"provedAt": "2027-02-01T00:00:00Z", "expiresAt": "2027-03-01T00:00:00Z"},
            "no recorder": {"recordedBy": ""},
            "no evidence": {"evidence": []},
            "unreviewed scopes": {"scopes": ["Calendars.Read", "Mail.ReadWrite"]},
            "bad digest": {"adapterSha256": "abc"},
            "wrong schema": {"schemaVersion": 2},
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                self.proof(**overrides)
                state = self.entry()["liveProof"]["state"]
                self.assertEqual(state, "invalid", label)
        (self.root / "assurance/provider-proof/cal.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.entry()["liveProof"]["state"], "invalid")
        (self.root / "assurance/provider-proof/cal.json").write_text("[]", encoding="utf-8")
        self.assertEqual(self.entry()["liveProof"]["state"], "invalid")

    def test_scopes_are_compared_as_a_set_so_order_does_not_matter(self):
        self.proof(scopes=list(reversed(SCOPES)))
        self.assertEqual(self.entry()["liveProof"]["state"], "valid")


class CheckTests(Case):
    def test_the_clean_registry_passes(self):
        self.assertEqual(evidence.check(self.root, now=NOW), [])

    def test_a_live_claim_needs_its_proof_file_and_the_registry_must_name_it(self):
        self.set_status("cal", "live", proof="somewhere/else.json")
        errors = evidence.check(self.root, now=NOW)
        self.assertTrue(any("must be assurance/provider-proof/cal.json" in error for error in errors))
        self.assertTrue(any("does not exist" in error for error in errors))
        self.proof()
        self.set_status("cal", "live", proof="assurance/provider-proof/cal.json")
        self.assertEqual(evidence.check(self.root, now=NOW), [])

    def test_a_live_claim_with_an_invalid_proof_fails_and_a_malformed_proof_for_a_non_live_provider_is_reported(self):
        self.proof(redacted=False)
        self.set_status("cal", "live", proof="assurance/provider-proof/cal.json")
        self.assertTrue(any("redacted" in error for error in evidence.check(self.root, now=NOW)))
        self.set_status("cal", "built-credential-free")
        self.assertTrue(any("malformed" in error for error in evidence.check(self.root, now=NOW)))

    def test_a_proof_file_for_an_unknown_provider_is_reported(self):
        (self.root / "assurance/provider-proof/ghost.json").write_text("{}", encoding="utf-8")
        self.assertEqual(evidence.check(self.root, now=NOW), ["assurance/provider-proof/ghost.json names a provider that is not in the registry"])


class CliTests(Case):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--root", str(self.root), "--now", "2026-10-16T12:00:00Z", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_manifest_json_markdown_and_output_file(self):
        code, out, _ = self.run_cli()
        self.assertEqual((code, json.loads(out)["summary"]["providers"]), (0, 4))
        code, out, _ = self.run_cli("--format", "markdown")
        self.assertIn("| Cal (`cal`) | 1 | built-credential-free | 2 | none | blocked | no retained live tenant proof |", out)
        target = self.root / "evidence.json"
        self.assertEqual(self.run_cli("--output", str(target))[0], 0)
        self.assertEqual(json.loads(target.read_text())["kind"], "ProviderEvidenceManifest")

    def test_go_live_exits_non_zero_with_the_blockers_and_zero_when_allowed(self):
        code, out, _ = self.run_cli("--go-live", "cal")
        self.assertEqual((code, json.loads(out)["allowed"]), (1, False))
        self.assertEqual(self.run_cli("--go-live", "builtin")[0], 0)
        code, _, err = self.run_cli("--go-live", "nope")
        self.assertEqual(code, 1)
        self.assertIn("unknown provider", err)

    def test_check_reports_problems_and_a_naive_now_is_refused(self):
        self.assertEqual(self.run_cli("--check")[0], 0)
        self.set_status("cal", "live", proof="assurance/provider-proof/cal.json")
        code, _, err = self.run_cli("--check")
        self.assertEqual(code, 1)
        self.assertIn("Provider evidence check failed", err)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["--root", str(self.root), "--now", "2026-10-16T12:00:00"]), 1)
        self.assertIn("time zone", err.getvalue())


class RepositoryTests(unittest.TestCase):
    """The real registry: nothing may claim to be live or go-live-allowed at tier 1+ without retained proof."""

    def test_the_repository_registry_is_consistent(self):
        self.assertEqual(evidence.check(ROOT), [])

    def test_no_third_party_provider_is_go_live_allowed_without_a_retained_proof_file(self):
        data = evidence.manifest(ROOT)
        proofs = {path.stem for path in (ROOT / evidence.PROOF_DIR).glob("*.json")} if (ROOT / evidence.PROOF_DIR).is_dir() else set()
        for item in data["providers"]:
            if item["tier"] >= 1 and item["id"] not in proofs:
                self.assertFalse(item["goLive"]["allowed"], item["id"])
                self.assertNotEqual(item["status"], "live", item["id"])

    def test_every_shipped_provider_has_automated_tests_and_the_registry_manifest_is_deterministic(self):
        first = evidence.manifest(ROOT, now=NOW)
        self.assertEqual(first, evidence.manifest(ROOT, now=NOW))
        for item in first["providers"]:
            if item["status"] in evidence.BUILT:
                self.assertGreater(item["tests"]["tests"], 0, item["id"])
                self.assertIsNotNone(item["adapter"]["sha256"], item["id"])

    def test_the_make_contracts_target_runs_the_check(self):
        self.assertIn("provider-evidence.py --check", (ROOT / "Makefile").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

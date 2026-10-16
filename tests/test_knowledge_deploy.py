"""Credential-free checks that a deployment which changes the approved business rules leaves a chained ledger entry."""
from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import shutil
import stat
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


deploy = _load("knowledge_deploy_under_test", "platform/core/knowledge_deploy.py")
govern = _load("govern_for_knowledge_deploy", "scripts/govern-action.py")
compiler = _load("compile_for_knowledge_deploy", "scripts/compile-workspace.py")
runtime_module = _load("governed_mcp_for_knowledge_deploy", "platform/runtime/governed_mcp.py")
ALICE = "alice@client.example"
SHA_A, SHA_B = "a" * 64, "b" * 64


def source(name="rules", *, status="approved", version="1.0.0", owner="owner@client.example", audiences=("receptionist",), **spec):
    document = {"apiVersion": "modelgarden.ai/v1", "kind": "KnowledgeSource", "metadata": {"name": name, "owner": owner},
                "spec": {"type": "text-file", "classification": "internal", "file": f"{name}.md", "status": status, "version": version, **spec}}
    if audiences is not None:
        document["spec"]["audiences"] = list(audiences)
    return document


def state(*sources, digests=None):
    desired = {"schemaVersion": 1, "knowledge": list(sources)}
    if digests:
        desired["knowledgeContent"] = {name: {"sha256": sha} for name, sha in digests.items()}
    return desired


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name) / "governance"
        self.audit = self.root / "audit.jsonl"

    def apply(self, desired, *, by=ALICE):
        return deploy.record_deployment(
            deploy.fingerprint(desired), self.root, changed_by=by,
            record=lambda before, after, who: deploy.record_change(govern, self.audit, before, after, who))

    def events(self) -> list[dict]:
        return [json.loads(line) for line in self.audit.read_text().splitlines() if line.strip()] if self.audit.exists() else []

    # -- fingerprint -------------------------------------------------------------------------

    def test_the_fingerprint_holds_declared_fields_and_the_digest_but_never_text(self):
        desired = state(source(reviewBy="2027-01-01"), digests={"rules": SHA_A})
        desired["knowledgeContent"]["rules"]["content"] = "We open at 9. Secret price list: $4,200."
        shown = deploy.fingerprint(desired)["rules"]
        self.assertEqual(shown, {"type": "text-file", "classification": "internal", "owner": "owner@client.example", "status": "approved",
                                 "version": "1.0.0", "reviewBy": "2027-01-01", "audiences": ["receptionist"], "sha256": SHA_A})
        self.assertNotIn("4,200", json.dumps(shown))

    def test_a_source_without_a_status_is_a_draft_and_other_source_types_carry_no_rule_fields(self):
        draft = source(status=None)
        del draft["spec"]["status"]
        remote = {"apiVersion": "modelgarden.ai/v1", "kind": "KnowledgeSource", "metadata": {"name": "wiki", "owner": "o@x.example"},
                  "spec": {"type": "url", "classification": "public", "reference": "https://wiki.example"}}
        shown = deploy.fingerprint(state(draft, remote))
        self.assertEqual(shown["rules"]["status"], "draft")
        self.assertEqual(set(shown["wiki"]), {"type", "classification", "owner"})
        self.assertEqual(deploy.fingerprint({"schemaVersion": 1}), {})

    # -- what gets ledgered ----------------------------------------------------------------------

    def test_the_first_start_ledgers_the_sources_it_started_with_and_who_deployed_them(self):
        result = self.apply(state(source(), digests={"rules": SHA_A}))
        self.assertEqual((result["changed"], result["baseline"], result["changedBy"], result["critical"]), (True, "none", ALICE, ["rules"]))
        (event,) = self.events()
        self.assertEqual((event["event"], event["action"], event["performedBy"]), ("operator-action", "knowledge.change", ALICE))
        self.assertEqual(event["details"]["added"], ["rules"])
        self.assertEqual(event["details"]["newlyApproved"], ["rules"])

    def test_an_unchanged_restart_records_nothing(self):
        self.apply(state(source(), digests={"rules": SHA_A}))
        again = self.apply(state(source(), digests={"rules": SHA_A}), by="runtime:deployment")
        self.assertEqual((again["changed"], again["baseline"], again["critical"]), (False, "recorded", []))
        self.assertEqual(len(self.events()), 1)

    def test_changed_text_in_an_approved_source_is_critical_and_shows_version_and_short_digests_only(self):
        self.apply(state(source(), digests={"rules": SHA_A}))
        result = self.apply(state(source(version="1.1.0"), digests={"rules": SHA_B}), by="jordan@mg.example")
        self.assertEqual((result["changed"], result["critical"]), (True, ["rules"]))
        details = self.events()[-1]["details"]
        self.assertEqual(details["approvedContentChanged"], ["rules"])
        self.assertEqual(sorted(details["changed"]["rules"]), ["sha256", "version"])
        self.assertEqual(details["revisions"]["rules"], {"from": {"version": "1.0.0", "sha256": "a" * 16}, "to": {"version": "1.1.0", "sha256": "b" * 16}})
        self.assertNotIn(SHA_B, json.dumps(self.events()))

    def test_a_draft_being_edited_is_recorded_but_not_critical_until_it_is_approved(self):
        self.apply(state(source(status="draft"), digests={"rules": SHA_A}))
        edited = self.apply(state(source(status="draft"), digests={"rules": SHA_B}))
        self.assertEqual((edited["changed"], edited["critical"]), (True, []))
        approved = self.apply(state(source(status="approved"), digests={"rules": SHA_B}))
        self.assertEqual(approved["critical"], ["rules"])
        self.assertEqual(self.events()[-1]["details"]["newlyApproved"], ["rules"])

    def test_widening_who_may_answer_from_an_approved_source_is_critical(self):
        self.apply(state(source(), digests={"rules": SHA_A}))
        more = self.apply(state(source(audiences=("receptionist", "billing")), digests={"rules": SHA_A}))
        self.assertEqual(more["critical"], ["rules"])
        everyone = self.apply(state(source(audiences=None), digests={"rules": SHA_A}))
        self.assertEqual(everyone["critical"], ["rules"])
        narrower = self.apply(state(source(audiences=("receptionist",)), digests={"rules": SHA_A}))
        self.assertEqual((narrower["changed"], narrower["critical"]), (True, []))

    def test_retiring_and_removing_a_source_are_recorded(self):
        self.apply(state(source(), source("pricing"), digests={"rules": SHA_A, "pricing": SHA_A}))
        self.apply(state(source(status="retired"), source("pricing"), digests={"rules": SHA_A, "pricing": SHA_A}))
        self.assertEqual(self.events()[-1]["details"]["retired"], ["rules"])
        self.apply(state(source(status="retired"), digests={"rules": SHA_A}))
        self.assertEqual(self.events()[-1]["details"]["removed"], ["pricing"])

    def test_a_deployment_with_no_sources_writes_a_baseline_but_no_event(self):
        result = self.apply({"schemaVersion": 1})
        self.assertEqual((result["changed"], result["baseline"]), (False, "none"))
        self.assertEqual(self.events(), [])
        self.assertTrue(self.apply(state(source(), digests={"rules": SHA_A}))["changed"])

    def test_the_trail_is_part_of_the_chained_ledger_and_verifies(self):
        self.apply(state(source(), digests={"rules": SHA_A}))
        self.apply(state(source(version="1.1.0"), digests={"rules": SHA_B}))
        verified = govern.verify_audit_chain(self.audit)
        self.assertEqual((verified["valid"], verified["events"]), (True, 2))

    # -- the applied record -----------------------------------------------------------------------

    def test_the_applied_record_is_private_and_holds_the_fingerprint_only(self):
        self.apply(state(source(), digests={"rules": SHA_A}))
        path = self.root / deploy.APPLIED_FILE
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(set(json.loads(path.read_text())), {"schemaVersion", "sources"})

    def test_a_damaged_record_is_never_trusted_and_is_repaired_by_ledgering_the_current_sources(self):
        self.apply(state(source(), digests={"rules": SHA_A}))
        (self.root / deploy.APPLIED_FILE).write_text("[]", encoding="utf-8")
        result = self.apply(state(source(), digests={"rules": SHA_A}))
        self.assertEqual((result["changed"], result["baseline"]), (True, "unreadable"))

    def test_if_the_ledger_cannot_be_written_nothing_is_remembered_and_the_error_reaches_the_caller(self):
        self.apply(state(source(), digests={"rules": SHA_A}))
        before = (self.root / deploy.APPLIED_FILE).read_text()

        def failing(_before, _after, _who):
            raise OSError("audit volume is read-only")

        with self.assertRaises(OSError):
            deploy.record_deployment(deploy.fingerprint(state(source(version="2.0.0"), digests={"rules": SHA_B})), self.root,
                                     changed_by=ALICE, record=failing)
        self.assertEqual((self.root / deploy.APPLIED_FILE).read_text(), before)
        self.assertTrue(self.apply(state(source(version="2.0.0"), digests={"rules": SHA_B}))["changed"])

    # -- the sidecar hook ---------------------------------------------------------------------------

    def runtime(self, desired):
        path = self.root.parent / "desired.json"
        path.write_text(json.dumps({"agent": {"spec": {}}, "tools": [], "source": {"name": "receptionist"}, **desired}), encoding="utf-8")
        return runtime_module.GovernedToolRuntime(path, self.audit, self.root / "approvals", environ={})

    def test_the_sidecar_hook_ledgers_changed_rules_before_serving_and_the_main_entry_point_calls_it(self):
        first = runtime_module.ledger_knowledge_deployment(self.runtime(state(source(), digests={"rules": SHA_A})), {"MODEL_GARDEN_DEPLOYED_BY": ALICE})
        again = runtime_module.ledger_knowledge_deployment(self.runtime(state(source(), digests={"rules": SHA_A})), {"MODEL_GARDEN_DEPLOYED_BY": ALICE})
        changed = runtime_module.ledger_knowledge_deployment(self.runtime(state(source(version="1.2.0"), digests={"rules": SHA_B})), {})
        self.assertEqual((first["changed"], again["changed"], changed["changed"]), (True, False, True))
        self.assertEqual([event["performedBy"] for event in self.events()], [ALICE, "runtime:deployment"])
        text = (ROOT / "platform/runtime/governed_mcp.py").read_text(encoding="utf-8")
        main = text[text.index("def main()"):]
        self.assertLess(main.index("ledger_knowledge_deployment(runtime)"), main.index("server.run("))

    def test_the_deploy_source_list_ships_the_module(self):
        deployer = _load("deploy_docker_for_knowledge_deploy", "scripts/deploy-client-docker.py")
        self.assertIn(pathlib.Path("platform/core/knowledge_deploy.py"), deployer.RUNTIME_SOURCE_PATHS)

    # -- the compile-time owner check -------------------------------------------------------------------

    def workspace(self, root: pathlib.Path, owner: str, status: str = "approved") -> pathlib.Path:
        target = root / "client"
        shutil.copytree(ROOT / "examples" / "workspace", target)
        path = target / "knowledge" / "rules-of-engagement.yaml"
        document = yaml.safe_load(path.read_text())
        document["metadata"]["owner"] = owner
        document["spec"]["status"] = status
        path.write_text(yaml.safe_dump(document))
        return target

    def compile(self, workspace, owners):
        binding = {"environment": "dev", "connectors": {
            "kb": {"provider": "text-knowledge", "tier": 0}, "crm": {"provider": "built-in-crm", "tier": 0},
            "calendar": {"provider": "google", "tier": 1, "token_ref": "secret://calendar/dev"}},
                   **({"governance": {"owners": owners}} if owners is not None else {})}
        return compiler.compile_workspace(workspace, "receptionist", environment_binding=binding)[0]

    def test_approved_knowledge_needs_a_named_owner_only_when_the_environment_names_its_owners(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(self.compile(self.workspace(pathlib.Path(tmp), "Owner@Client.example"), [" owner@client.example "]))
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, "rules-of-engagement.*approved but its owner 'consultant@agency.example'"):
            self.compile(self.workspace(pathlib.Path(tmp), "consultant@agency.example"), ["owner@client.example"])
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, "approved but its owner"):
            self.compile(self.workspace(pathlib.Path(tmp), "owner@client.example"), [])  # an empty list names nobody
        with tempfile.TemporaryDirectory() as tmp:  # no owner list: unchanged behaviour
            self.assertTrue(self.compile(self.workspace(pathlib.Path(tmp), "anyone@x.example"), None))

    def test_a_draft_source_does_not_need_an_authorised_owner_to_be_authored(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(self.compile(self.workspace(pathlib.Path(tmp), "consultant@agency.example", status="draft"), ["owner@client.example"]))

    def test_the_owner_check_ignores_non_text_sources_and_does_not_mutate_input(self):
        remote = {"metadata": {"name": "wiki", "owner": "x@y.example"}, "spec": {"type": "url", "status": "approved"}}
        sources = [remote, source(owner="owner@client.example")]
        snapshot = copy.deepcopy(sources)
        self.assertEqual(deploy.approval_owner_errors(sources, ["owner@client.example"]), [])
        self.assertEqual(deploy.approval_owner_errors(sources, None), [])
        self.assertEqual(sources, snapshot)


if __name__ == "__main__":
    unittest.main()

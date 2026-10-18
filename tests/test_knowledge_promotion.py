"""Credential-free checks for gating critical business-rule changes on eval evidence, with drift detection and rollback (#116)."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import shutil
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


promotion = _load("knowledge_promotion_under_test", "platform/core/knowledge_promotion.py")
cli = _load("knowledge_promotion_cli_under_test", "scripts/knowledge-promotion.py")
compiler = _load("compiler_for_promotion", "scripts/compile-workspace.py")
T0 = 1_800_000_000
DAY = 86400
OWNER, OTHER = "owner@client.example", "intern@client.example"
RULES = "# Hours\nWe open 9am to 6pm.\n"
PRIVATE = "Hargreaves boiler 12 Acacia Road"


def source(name="rules", *, status="approved", version="1.0.0", audiences=("receptionist",), sha="a" * 64):
    return {
        "metadata": {"name": name, "owner": "business-owner"},
        "spec": {"type": "text-file", "classification": "internal", "status": status, "version": version, "effectiveDate": "2026-01-01",
                 **({"audiences": list(audiences)} if audiences is not None else {})},
    }, sha


def desired(*sources):
    return {"knowledge": [item[0] for item in sources], "knowledgeContent": {item[0]["metadata"]["name"]: {"sha256": item[1]} for item in sources}}


def evidence_for(state, *, suite="receptionist", passed=True, at=T0, failures=0):
    return promotion.build_evidence(state, suite=suite, passed=passed, failures=failures, now=at)


class GateTests(unittest.TestCase):
    def test_a_first_promotion_of_an_approved_source_needs_evidence(self):
        state = desired(source())
        result = promotion.check(state, None, None, now=T0)
        self.assertEqual((result["allowed"], result["critical"], result["reasons"]), (False, ["rules"], ["no-evidence"]))
        self.assertTrue(promotion.check(state, None, evidence_for(state), now=T0 + 60)["allowed"])

    def test_changed_approved_text_needs_evidence_for_the_new_text_not_the_old(self):
        old, new = desired(source(sha="a" * 64)), desired(source(sha="b" * 64, version="1.1.0"))
        record = promotion.promote(old, None, evidence_for(old), by=OWNER, now=T0)
        stale_evidence = evidence_for(old, at=T0 + 100)
        result = promotion.check(new, record, stale_evidence, now=T0 + 200)
        self.assertEqual((result["allowed"], result["critical"]), (False, ["rules"]))
        self.assertEqual(result["reasons"], ["evidence-not-for-this-content:rules"])
        self.assertTrue(promotion.check(new, record, evidence_for(new, at=T0 + 100), now=T0 + 200)["allowed"])

    def test_the_same_text_with_a_new_version_label_is_still_not_covered_by_old_evidence(self):
        old = desired(source(sha="a" * 64, version="1.0.0"))
        new = desired(source(sha="a" * 64, version="1.0.1"))
        record = promotion.promote(old, None, evidence_for(old), by=OWNER, now=T0)
        self.assertEqual(promotion.check(new, record, evidence_for(old), now=T0 + 10)["reasons"], ["evidence-not-for-this-content:rules"])

    def test_widening_the_audience_is_critical_and_narrowing_it_is_not(self):
        base = desired(source(audiences=("receptionist",)))
        record = promotion.promote(base, None, evidence_for(base), by=OWNER, now=T0)
        wider = desired(source(audiences=("receptionist", "researcher")))
        self.assertEqual(promotion.check(wider, record, None, now=T0 + 5)["critical"], ["rules"])
        everyone = desired(source(audiences=None))
        self.assertEqual(promotion.check(everyone, record, None, now=T0 + 5)["critical"], ["rules"])
        two = desired(source(audiences=("receptionist", "researcher")))
        record2 = promotion.promote(two, record, evidence_for(two, at=T0 + 10), by=OWNER, now=T0 + 20)
        narrower = desired(source(audiences=("receptionist",)))
        self.assertTrue(promotion.check(narrower, record2, None, now=T0 + 30)["allowed"])

    def test_drafts_retirements_removals_and_no_change_need_no_evidence(self):
        approved = desired(source())
        record = promotion.promote(approved, None, evidence_for(approved), by=OWNER, now=T0)
        for state in (approved, desired(), desired(source(status="retired")), desired(source(status="approved"), source("scratch", status="draft", sha="c" * 64))):
            result = promotion.check(state, record, None, now=T0 + 10)
            self.assertTrue(result["allowed"], result)
        self.assertEqual(promotion.check(desired(source("scratch", status="draft")), None, None, now=T0)["critical"], [])

    def test_evidence_must_pass_name_the_suite_be_dated_recent_and_not_from_the_future(self):
        state = desired(source())
        cases = {
            "evidence-not-passing": evidence_for(state, passed=False, failures=2),
            "evidence-wrong-suite": evidence_for(state, suite="other"),
            "evidence-stale": evidence_for(state, at=T0 - 8 * DAY),
            "evidence-from-the-future": evidence_for(state, at=T0 + 3600),
        }
        for reason, evidence in cases.items():
            self.assertIn(reason, promotion.check(state, None, evidence, now=T0)["reasons"], reason)
        undated = {**evidence_for(state), "ranAt": "yesterday"}
        self.assertIn("evidence-undated", promotion.check(state, None, undated, now=T0)["reasons"])
        self.assertTrue(promotion.check(state, None, evidence_for(state, at=T0 - 6 * DAY), now=T0)["allowed"])
        self.assertTrue(promotion.check(state, None, evidence_for(state, at=T0 - 20 * DAY), now=T0, max_age_seconds=30 * DAY)["allowed"])

    def test_evidence_that_lists_other_sources_or_lies_about_a_digest_does_not_cover_this_one(self):
        state = desired(source(sha="a" * 64))
        forged = evidence_for(desired(source(sha="b" * 64)))
        self.assertEqual(promotion.check(state, None, forged, now=T0 + 5)["reasons"], ["evidence-not-for-this-content:rules"])
        other = evidence_for(desired(source("something-else", sha="a" * 64)))
        self.assertEqual(promotion.check(state, None, other, now=T0 + 5)["reasons"], ["evidence-not-for-this-content:rules"])
        self.assertIn("no-evidence", promotion.check(state, None, "not a mapping", now=T0)["reasons"])

    def test_when_the_environment_names_owners_only_one_of_them_may_promote(self):
        state = desired(source())
        evidence = evidence_for(state)
        owners = [OWNER]
        self.assertIn("promoter-not-an-owner", promotion.check(state, None, evidence, now=T0 + 5, promoted_by=OTHER, owners=owners)["reasons"])
        self.assertIn("promoter-not-an-owner", promotion.check(state, None, evidence, now=T0 + 5, promoted_by=None, owners=owners)["reasons"])
        self.assertTrue(promotion.check(state, None, evidence, now=T0 + 5, promoted_by=" Owner@Client.Example ", owners=owners)["allowed"])
        self.assertTrue(promotion.check(state, None, evidence, now=T0 + 5, promoted_by=OTHER)["allowed"])  # no owners named: not enforced

    def test_check_never_includes_rule_text(self):
        state = desired(source())
        blob = json.dumps(promotion.check(state, None, evidence_for(state), now=T0 + 5))
        self.assertNotIn(PRIVATE, blob)
        self.assertLess(max(len(token) for token in blob.replace('"', " ").split()), 64)  # digests appear only as short prefixes


class PromoteTests(unittest.TestCase):
    def test_promote_records_the_snapshot_the_promoter_and_the_evidence_digest(self):
        state = desired(source())
        evidence = evidence_for(state)
        record = promotion.promote(state, None, evidence, by=OWNER, now=T0 + 60)
        self.assertEqual((record["promoted"]["by"], record["promoted"]["at"], record["previous"]), (OWNER, "2027-01-15T08:01:00Z", None))
        self.assertEqual(record["promoted"]["evidence"]["sha256"], promotion.evidence_digest(evidence))
        self.assertEqual(record["promoted"]["sources"]["rules"]["sha256"], "a" * 64)
        self.assertEqual(record["history"][0]["critical"], ["rules"])
        self.assertNotIn("text", json.dumps(record).lower().replace("text-file", ""))

    def test_a_blocked_promotion_raises_with_the_reasons_and_changes_nothing(self):
        state = desired(source())
        with self.assertRaises(promotion.PromotionBlocked) as raised:
            promotion.promote(state, None, None, by=OWNER, now=T0)
        self.assertEqual(raised.exception.reasons, ["no-evidence"])
        with self.assertRaises(promotion.PromotionError):
            promotion.promote(state, None, evidence_for(state), by="  ", now=T0 + 5)

    def test_a_non_critical_promotion_carries_no_evidence_entry_and_keeps_the_previous_snapshot(self):
        first = desired(source())
        record = promotion.promote(first, None, evidence_for(first), by=OWNER, now=T0)
        second = desired(source(), source("scratch", status="draft", sha="c" * 64))
        updated = promotion.promote(second, record, None, by=OWNER, now=T0 + 10)
        self.assertNotIn("evidence", updated["promoted"])
        self.assertEqual(updated["previous"], record["promoted"])
        self.assertEqual([entry["action"] for entry in updated["history"]], ["promoted", "promoted"])

    def test_history_is_bounded(self):
        state = desired(source())
        record = promotion.promote(state, None, evidence_for(state), by=OWNER, now=T0)
        for index in range(60):
            record = promotion.promote(state, record, None, by=OWNER, now=T0 + index)
        self.assertEqual(len(record["history"]), 50)

    def test_a_malformed_record_is_an_error_not_a_free_pass(self):
        state = desired(source())
        for record in ({"promoted": "nope"}, {"promoted": {"sources": []}}):
            with self.assertRaises(promotion.PromotionError):
                promotion.check(state, record, None, now=T0)


class DriftAndRollbackTests(unittest.TestCase):
    def promoted(self):
        one = desired(source(sha="a" * 64, version="1.0.0"))
        record = promotion.promote(one, None, evidence_for(one), by=OWNER, now=T0)
        two = desired(source(sha="b" * 64, version="1.1.0"))
        record = promotion.promote(two, record, evidence_for(two, at=T0 + 100), by=OWNER, now=T0 + 200)
        return one, two, record

    def test_verify_passes_for_the_promoted_content_and_flags_drift_additions_and_removals(self):
        one, two, record = self.promoted()
        self.assertTrue(promotion.verify(two, record)["matches"])
        self.assertEqual(promotion.verify(one, record)["drifted"], ["rules"])
        extra = desired(source(sha="b" * 64, version="1.1.0"), source("new", sha="d" * 64))
        self.assertEqual(promotion.verify(extra, record)["unpromoted"], ["new"])
        self.assertEqual(promotion.verify(desired(), record)["missing"], ["rules"])
        self.assertFalse(promotion.verify(desired(), record)["matches"])
        self.assertTrue(promotion.verify(desired(), None)["matches"])
        self.assertFalse(promotion.verify(two, None)["matches"])

    def test_verify_catches_a_silently_widened_audience(self):
        _, two, record = self.promoted()
        wider = desired(source(sha="b" * 64, version="1.1.0", audiences=("receptionist", "researcher")))
        self.assertEqual(promotion.verify(wider, record)["drifted"], ["rules"])

    def test_rollback_makes_the_previous_snapshot_promoted_and_keeps_what_it_rolled_back_from(self):
        one, two, record = self.promoted()
        back = promotion.rollback(record, by=OWNER, now=T0 + 300)
        self.assertEqual(back["promoted"]["sources"]["rules"]["version"], "1.0.0")
        self.assertEqual(back["previous"]["sources"]["rules"]["version"], "1.1.0")
        self.assertEqual(back["history"][-1]["action"], "rolled-back")
        self.assertTrue(promotion.verify(one, back)["matches"])
        self.assertFalse(promotion.verify(two, back)["matches"])
        forward = promotion.rollback(back, by=OWNER, now=T0 + 400)  # a rollback can itself be undone
        self.assertEqual(forward["promoted"]["sources"]["rules"]["version"], "1.1.0")

    def test_rollback_needs_something_to_go_back_to_and_a_named_person(self):
        state = desired(source())
        first = promotion.promote(state, None, evidence_for(state), by=OWNER, now=T0)
        with self.assertRaises(promotion.PromotionError):
            promotion.rollback(first, by=OWNER, now=T0)
        with self.assertRaises(promotion.PromotionError):
            promotion.rollback(None, by=OWNER, now=T0)
        _, _, record = self.promoted()
        with self.assertRaises(promotion.PromotionError):
            promotion.rollback(record, by="", now=T0)

    def test_the_plan_names_what_to_restore_with_short_digests_only(self):
        _, _, record = self.promoted()
        (step,) = promotion.rollback_plan(record)
        self.assertEqual(step, {"source": "rules", "action": "restore", "version": "1.0.0", "sha256": "a" * 16, "status": "approved"})
        added = promotion.promote(desired(source(sha="b" * 64, version="1.1.0"), source("faq", sha="e" * 64)), record,
                                  evidence_for(desired(source(sha="b" * 64, version="1.1.0"), source("faq", sha="e" * 64)), at=T0 + 300), by=OWNER, now=T0 + 400)
        steps = {item["source"]: item["action"] for item in promotion.rollback_plan(added)}
        self.assertEqual(steps, {"faq": "remove"})


class RealWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.workspace = self.root / "workspace"
        shutil.copytree(ROOT / "examples" / "workspace", self.workspace)

    def compile(self):
        return compiler.compile_workspace(self.workspace, "receptionist")[0]

    def test_the_reference_workspace_needs_evidence_and_the_eval_runner_produces_it(self):
        state = self.compile()
        evidence_path = self.root / "evidence.json"
        eval_script = _load("eval_for_promotion", "scripts/eval-receptionist.py")
        original = sys.argv
        sys.argv = ["eval-receptionist.py", str(self.workspace), "--evidence", str(evidence_path)]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(eval_script.main(), 0)
        finally:
            sys.argv = original
        evidence = json.loads(evidence_path.read_text())
        self.assertEqual((evidence["suite"], evidence["passed"], evidence["failures"]), ("receptionist", True, 0))
        self.assertIn("rules-of-engagement", evidence["knowledge"])
        self.assertTrue(promotion.check(state, None, None, now=T0)["critical"])
        import time
        now = int(time.time())
        self.assertTrue(promotion.check(state, None, evidence, now=now + 5)["allowed"])

    def test_editing_the_rules_after_the_evals_ran_blocks_promotion_until_they_are_run_again(self):
        import time
        before = self.compile()
        evidence = evidence_for(before, at=int(time.time()))
        rules = self.workspace / "knowledge" / "rules-of-engagement.md"
        rules.write_text(rules.read_text() + "\nWe are also open on Sundays.\n")
        after = self.compile()
        result = promotion.check(after, None, evidence, now=int(time.time()) + 5)
        self.assertEqual(result["reasons"], ["evidence-not-for-this-content:rules-of-engagement"])

    def test_a_failing_eval_run_still_writes_evidence_that_says_so(self):
        instructions = next((self.workspace / "agents").rglob("receptionist/instructions.md"))
        instructions.write_text("Be helpful.\n")
        evidence_path = self.root / "failed.json"
        eval_script = _load("eval_for_promotion_fail", "scripts/eval-receptionist.py")
        original = sys.argv
        sys.argv = ["eval-receptionist.py", str(self.workspace), "--evidence", str(evidence_path)]
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(eval_script.main(), 1)
        finally:
            sys.argv = original
        evidence = json.loads(evidence_path.read_text())
        self.assertFalse(evidence["passed"])
        self.assertGreater(evidence["failures"], 0)
        import time
        self.assertIn("evidence-not-passing", promotion.check(self.compile(), None, evidence, now=int(time.time()) + 5)["reasons"])


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.record = self.root / "knowledge-promotion.json"

    def write(self, name, document):
        path = self.root / name
        path.write_text(json.dumps(document))
        return str(path)

    def run_command(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([*argv])
        return code, (json.loads(out.getvalue()) if out.getvalue().strip() else None), err.getvalue()

    def fresh_evidence(self, state):
        import time
        return self.write("evidence.json", evidence_for(state, at=int(time.time())))

    def test_check_promote_verify_rollback_round_trip(self):
        one = desired(source(sha="a" * 64, version="1.0.0"))
        path_one = self.write("one.json", one)
        code, result, _ = self.run_command("check", path_one, "--record", str(self.record))
        self.assertEqual((code, result["reasons"]), (1, ["no-evidence"]))
        code, result, _ = self.run_command("promote", path_one, "--record", str(self.record), "--evidence", self.fresh_evidence(one), "--by", OWNER, "--owners", OWNER)
        self.assertEqual((code, result["promoted"]), (0, True))
        self.assertEqual(self.run_command("verify", path_one, "--record", str(self.record))[0], 0)
        two = desired(source(sha="b" * 64, version="1.1.0"))
        path_two = self.write("two.json", two)
        self.assertEqual(self.run_command("verify", path_two, "--record", str(self.record))[0], 1)  # content changed without a promotion
        self.assertEqual(self.run_command("promote", path_two, "--record", str(self.record), "--evidence", self.fresh_evidence(one), "--by", OWNER)[0], 1)
        code, _, _ = self.run_command("promote", path_two, "--record", str(self.record), "--evidence", self.fresh_evidence(two), "--by", OTHER, "--owners", OWNER)
        self.assertEqual(code, 1)
        code, _, _ = self.run_command("promote", path_two, "--record", str(self.record), "--evidence", self.fresh_evidence(two), "--by", OWNER, "--owners", OWNER)
        self.assertEqual(code, 0)
        code, plan, _ = self.run_command("plan", "--record", str(self.record))
        self.assertEqual((code, plan[0]["version"]), (0, "1.0.0"))
        code, result, _ = self.run_command("rollback", "--record", str(self.record), "--by", OWNER)
        self.assertEqual((code, result["plan"][0]["action"]), (0, "restore"))
        self.assertEqual(self.run_command("verify", path_one, "--record", str(self.record))[0], 0)
        self.assertEqual(self.run_command("verify", path_two, "--record", str(self.record))[0], 1)

    def test_bad_input_is_an_error_not_a_pass(self):
        code, _, error = self.run_command("check", str(self.root / "missing.json"), "--record", str(self.record))
        self.assertEqual(code, 1)
        self.assertIn("Knowledge promotion failed", error)
        self.record.write_text('{"schemaVersion": 99}')
        code, _, error = self.run_command("plan", "--record", str(self.record))
        self.assertEqual(code, 1)
        code, _, _ = self.run_command("rollback", "--record", str(self.root / "nope.json"), "--by", OWNER)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()

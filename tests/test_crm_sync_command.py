"""Credential-free checks for the CRM filing payload store and the person-facing `crm-sync` command (#108, #115, #109)."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


filing = _load("crm_filing_for_sync_command", "platform/core/crm_filing.py")
sync = filing._sync()
command = _load("runtime_crm_sync_under_test", "scripts/runtime-crm-sync.py")
operator = _load("client_operator_for_crm_sync", "scripts/client-operator.py")
T0 = 1_800_000_000
PERSON = "jordan@acme.example"
ARGUMENTS = {"objectId": "unmatched", "interactionType": "email", "channel": "email", "direction": "inbound",
             "timestamp": "2026-10-18T10:00:00Z", "summary": "Support email thread handled by the coworker: category enquiry, drafted.",
             "outcome": "drafted", "transcriptRef": "email:abc"}
KEY = "ab" * 32


class PayloadStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.store = filing.PayloadStore(self.root / "payloads")

    def test_arguments_are_kept_privately_read_back_and_dropped(self):
        self.store.put(KEY, ARGUMENTS)
        self.assertEqual(self.store.get(KEY), ARGUMENTS)
        self.assertEqual(self.store({"key": KEY}), ARGUMENTS)  # callable with a ledger entry, for retry_due
        self.assertEqual(self.store.keys(), [KEY])
        self.assertEqual(stat.S_IMODE((self.root / "payloads").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root / "payloads" / f"{KEY}.json").stat().st_mode), 0o600)
        self.store.drop(KEY)
        self.store.drop(KEY)  # harmless twice
        self.assertIsNone(self.store.get(KEY))

    def test_only_ledger_keys_and_bounded_sizes_are_accepted(self):
        for bad in ("../escape", "short", "AB" * 32, ""):
            with self.assertRaises(ValueError, msg=bad):
                self.store.put(bad, ARGUMENTS)
        with self.assertRaisesRegex(ValueError, "too large"):
            self.store.put(KEY, {"summary": "x" * (filing.MAX_PAYLOAD_BYTES + 1)})
        (self.root / "payloads" / "notes.json").parent.mkdir(parents=True, exist_ok=True)
        (self.root / "payloads" / "notes.json").write_text("{}")
        self.assertEqual(self.store.keys(), [])

    def test_a_corrupt_file_reads_as_not_kept_so_the_entry_is_skipped_not_invented(self):
        (self.root / "payloads").mkdir()
        (self.root / "payloads" / f"{KEY}.json").write_text("not json")
        self.assertIsNone(self.store.get(KEY))

    def test_filing_keeps_arguments_until_the_provider_acknowledges_then_drops_them(self):
        ledger = sync.SyncLedger(self.root / "sync", tenant="acme-prod")
        attempts = []

        def executor(arguments):
            attempts.append(arguments)
            if len(attempts) == 1:
                raise TimeoutError("down")
            return {"externalObjectRef": "built-in-crm:interaction/i-1", "duplicate": False}

        first = filing.file_interaction(ledger, executor, ARGUMENTS, channel="email", external_id="thread-1", interaction_type="email", now=T0, store=self.store)
        self.assertEqual(first["state"], "failed")
        key = first["key"]
        self.assertEqual(self.store.get(key), ARGUMENTS)
        counts = filing.retry_due(ledger, executor, self.store, now=T0 + 31)
        self.assertEqual((counts["attempted"], counts["synced"]), (1, 1))
        self.assertIsNone(self.store.get(key))
        again = filing.file_interaction(ledger, executor, ARGUMENTS, channel="email", external_id="thread-1", interaction_type="email", now=T0 + 99, store=self.store)
        self.assertEqual((again["duplicate"], again["attempted"]), (True, False))
        self.assertEqual(self.store.keys(), [])
        self.assertEqual(len(attempts), 2)

    def test_an_entry_whose_details_were_not_kept_is_skipped_and_counted(self):
        ledger = sync.SyncLedger(self.root / "sync", tenant="acme-prod")
        ledger.enqueue(channel="email", external_id="thread-9", interaction_type="email", now=T0)
        counts = filing.retry_due(ledger, lambda arguments: {"externalObjectRef": "x:1"}, self.store, now=T0)
        self.assertEqual((counts["skipped"], counts["attempted"]), (1, 0))


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.sync_root = self.root / "crm-sync"
        self.audit = self.root / "audit.jsonl"
        self.desired = self.root / "desired.json"
        self.desired.write_text(json.dumps({"source": {"client": "Acme Prod"}}))
        self.ledger = sync.SyncLedger(self.sync_root, tenant="acme-prod")
        self.store = filing.PayloadStore(self.sync_root / "payloads")

    def run_command(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = command.main([str(self.sync_root), "--desired-state", str(self.desired), "--audit-path", str(self.audit), *args], now=T0 + 5)
        return code, out.getvalue(), err.getvalue()

    def blocked(self, external_id="thread-1", keep=True):
        key = self.ledger.enqueue(channel="email", external_id=external_id, interaction_type="email", now=T0)["key"]
        self.ledger.record_failure(key, error_code="unauthorized", now=T0)
        if keep:
            self.store.put(key, ARGUMENTS)
        return key

    def test_an_empty_store_says_so(self):
        code, out, _ = self.run_command("list")
        self.assertEqual((code, out.strip()), (0, "CRM filing: nothing yet."))

    def test_list_shows_state_and_what_to_do_but_never_content(self):
        key = self.blocked()
        done = self.ledger.enqueue(channel="email", external_id="thread-2", interaction_type="email", now=T0)["key"]
        self.ledger.record_success(done, provider_ref="built-in-crm:interaction/i-1", now=T0)
        code, out, _ = self.run_command("list")
        self.assertEqual(code, 0)
        self.assertIn("1 synced, 1 blocked", out)
        self.assertIn(key[:12], out)
        self.assertIn("not authorised", out)
        self.assertNotIn(done[:12], out)  # filed ones are counted, not listed
        for private in ("Support email thread", "drafted", "email:abc"):
            self.assertNotIn(private, out)
        code, raw, _ = self.run_command("list", "--json")
        document = json.loads(raw)
        self.assertEqual(document["counts"]["blocked"], 1)
        shown = next(row for row in document["entries"] if row["key"] == key)
        self.assertTrue(shown["kept"])
        self.assertNotIn("summary", json.dumps(document))

    def test_an_entry_without_kept_details_says_it_cannot_be_retried_automatically(self):
        self.blocked(keep=False)
        _, out, _ = self.run_command("list")
        self.assertIn("details were not kept", out)

    def test_retry_makes_a_blocked_entry_pending_under_the_named_person_and_is_ledgered(self):
        key = self.blocked()
        code, out, _ = self.run_command("retry", key[:12], "--by", PERSON)
        self.assertEqual((code, json.loads(out)), (0, {"key": key, "state": "pending"}))
        self.assertEqual(self.ledger.get(key)["state"], "pending")
        events = [json.loads(line) for line in self.audit.read_text().splitlines()]
        retried = next(event for event in events if event.get("action") == "crm-sync.retry")
        self.assertEqual(retried["performedBy"], PERSON)
        self.assertNotIn("Support email thread", self.audit.read_text())

    def test_retry_refuses_an_unknown_ambiguous_or_unnamed_request(self):
        self.blocked()
        for argv in (("retry", "deadbeef", "--by", PERSON), ("retry", "zz", "--by", PERSON), ("retry", KEY, "--by", "  ")):
            code, _, err = self.run_command(*argv)
            self.assertEqual(code, 1, argv)
            self.assertTrue(err.startswith("Runtime crm-sync failed"), argv)
        code, _, _ = self.run_command("retry", self.ledger.list()[0]["key"][:12], "--by", PERSON)
        self.assertEqual(code, 0)
        code, _, err = self.run_command("retry", self.ledger.list()[0]["key"], "--by", PERSON)  # now pending: nothing to retry
        self.assertEqual(code, 1)

    def test_the_tenant_defaults_to_the_deployments_client(self):
        self.assertEqual(command.default_tenant(self.desired), "acme-prod")
        with self.assertRaisesRegex(ValueError, "tenant identity unavailable"):
            command.default_tenant(self.root / "missing.json")


class OperatorTests(unittest.TestCase):
    def test_the_operator_command_validates_before_touching_docker_and_passes_arguments(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(returncode=0, stdout=("gov1\n" if argv[:2] == ["docker", "ps"] else "ok\n"), stderr="")

        with mock.patch.object(operator, "_run", fake_run), mock.patch.object(operator, "_require_command"), contextlib.redirect_stdout(io.StringIO()):
            operator.runtime_crm_sync("acme-dev", "list")
            operator.runtime_crm_sync("acme-dev", "list", as_json=True)
            operator.runtime_crm_sync("acme-dev", "retry", key="abcdef012345", by=PERSON)
        executed = [argv for argv in calls if argv[:2] == ["docker", "exec"]]
        self.assertEqual(executed[0][-2:], ["/opt/data/.modelgarden/crm-sync", "list"])
        self.assertEqual(executed[1][-3:], ["/opt/data/.modelgarden/crm-sync", "list", "--json"])
        self.assertEqual(executed[2][-4:], ["retry", "abcdef012345", "--by", PERSON])
        before = len(calls)
        for action, kwargs in (("wipe", {}), ("retry", {"by": PERSON}), ("retry", {"key": "xyz", "by": PERSON}),
                               ("retry", {"key": "abcdef012345"}), ("list", {"key": "abcdef012345"})):
            with self.assertRaises(ValueError, msg=(action, kwargs)):
                operator.runtime_crm_sync("acme-dev", action, **kwargs)
        self.assertEqual(len(calls), before)

    def test_the_runtime_image_carries_the_command_and_the_modules_it_needs(self):
        source = (ROOT / "scripts" / "deploy-client-docker.py").read_text(encoding="utf-8")
        for needed in ("scripts/runtime-crm-sync.py", "platform/core/crm_sync.py", "platform/core/crm_filing.py", "scripts/govern-action.py"):
            self.assertIn(needed, source)


if __name__ == "__main__":
    unittest.main()

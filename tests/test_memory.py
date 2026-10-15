"""Credential-free checks for scoped, inspectable, deletable coworker memory."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
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


memory = _load("memory_under_test", "platform/core/memory.py")
govern = _load("govern_for_memory", "scripts/govern-action.py")
cli = _load("runtime_memory_cli", "scripts/runtime-memory.py")
operator = _load("operator_for_memory", "scripts/client-operator.py")
Error = memory.MemoryStoreError
ALICE = "alice@client.example"
ADMIN = "owner@client.example"
SECRET = "Alex Tan prefers morning slots; mother's maiden name Hargreaves"


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name) / "memory"
        self.events: list[tuple[str, str, dict]] = []

    def store(self, tenant="acme-prod", **kwargs) -> memory.MemoryStore:
        return memory.MemoryStore(self.root, tenant=tenant, audit=lambda *event: self.events.append(event), **kwargs)


class ScopeTests(Case):
    def test_a_reader_sees_only_the_scopes_it_names(self):
        store = self.store()
        store.remember("client", "We close for lunch 12-1", agent="receptionist")
        store.remember("agent:receptionist", "Greet in English first", agent="receptionist")
        store.remember("agent:researcher", "Prefers short answers", agent="researcher")
        store.remember("conversation:c-1", SECRET, agent="receptionist")
        store.remember("conversation:c-2", "Asked about boilers", agent="receptionist")
        names = lambda scopes: sorted(e["text"][:12] for e in store.list(scopes))  # noqa: E731
        self.assertEqual(len(store.list(["client", "agent:receptionist", "conversation:c-1"])), 3)
        self.assertEqual(names(["conversation:c-2"]), ["Asked about "])
        self.assertEqual(store.list([]), [])
        self.assertFalse(any("Hargreaves" in e["text"] for e in store.list(["client", "agent:receptionist", "conversation:c-2"])))

    def test_bad_scopes_are_refused(self):
        store = self.store()
        for bad in ("", "everyone", "agent:", "conversation:bad id", "client:extra", None, 5):
            with self.assertRaises(Error, msg=str(bad)):
                store.remember(bad, "x", agent="a")
            with self.assertRaises(Error, msg=str(bad)):
                store.list([bad])

    def test_a_conversation_scope_remembers_which_conversation_without_storing_its_id(self):
        entry = self.store().remember("conversation:c-9", "note", agent="receptionist")
        self.assertEqual(entry["conversationDigest"], memory.digest("c-9"))
        self.assertNotIn("c-9", json.dumps({k: v for k, v in entry.items() if k != "scope"}))


class TenantTests(Case):
    def test_tenants_have_separate_files_and_never_see_each_other(self):
        a, b = self.store("acme-prod"), self.store("globex-prod")
        a.remember("client", "acme fact", agent="r")
        self.assertEqual(b.list(["client"]), [])
        self.assertNotEqual(a.path, b.path)

    def test_a_file_from_another_tenant_is_refused_not_read(self):
        self.store("acme-prod").remember("client", "acme fact", agent="r")
        stolen = self.store("globex-prod")
        stolen.path.write_text(self.store("acme-prod").path.read_text(), encoding="utf-8")
        with self.assertRaisesRegex(Error, "another tenant"):
            stolen.list(["client"])

    def test_tenant_slugs_are_validated(self):
        for bad in ("", "Acme", "a b", "../x", "-x", "x" * 64, None):
            with self.assertRaises(Error, msg=str(bad)):
                memory.MemoryStore(self.root, tenant=bad)

    def test_files_are_private(self):
        store = self.store()
        store.remember("client", "fact", agent="r")
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)


class WriteTests(Case):
    def test_a_write_is_ledgered_as_digests_never_text(self):
        store = self.store()
        entry = store.remember("conversation:c-1", SECRET, agent="receptionist", owner="alex@customer.example")
        (action, by, details), = self.events
        self.assertEqual((action, by), ("memory.write", "agent:receptionist"))
        self.assertEqual(details, {"memoryId": entry["id"], "scope": "conversation:c-1", "classification": "internal",
                                   "textDigest": memory.digest(SECRET), "conversationDigest": memory.digest("c-1")})
        rendered = json.dumps(self.events)
        for private in ("Alex", "Hargreaves", "morning"):
            self.assertNotIn(private, rendered)
        with tempfile.TemporaryDirectory() as tmp:  # and the real ledger accepts it
            audit = pathlib.Path(tmp) / "audit.jsonl"
            govern.record_operator_action(audit, action, by, details)
            self.assertTrue(govern.verify_audit_chain(audit)["valid"])

    def test_the_same_memory_is_not_duplicated_or_re_ledgered(self):
        store = self.store()
        first = store.remember("client", "We open at 9", agent="a")
        again = store.remember("client", "  We open at 9  ", agent="b")
        self.assertEqual((first["duplicate"], again["duplicate"], again["id"]), (False, True, first["id"]))
        self.assertEqual(len(self.events), 1)
        self.assertEqual(store.remember("agent:a", "We open at 9", agent="a")["duplicate"], False)

    def test_restricted_data_is_refused_unless_the_deployment_policy_allows_it(self):
        with self.assertRaisesRegex(Error, "restricted"):
            self.store().remember("client", "health note", agent="a", classification="restricted")
        self.assertEqual(self.events, [])
        allowed = self.store(allow_restricted=True).remember("client", "health note", agent="a", classification="restricted")
        self.assertEqual(allowed["classification"], "restricted")
        for other in ("public", "internal", "confidential"):
            self.assertTrue(self.store().remember("client", f"note {other}", agent="a", classification=other))

    def test_card_numbers_are_redacted_before_storage_and_the_digest_is_of_what_is_stored(self):
        entry = self.store().remember("client", "paid with 4111 1111 1111 1111 today", agent="a")
        self.assertEqual(entry["text"], "paid with [REDACTED:card] today")
        self.assertEqual(entry["digest"], memory.digest(entry["text"]))
        self.assertNotIn("4111", self.store().path.read_text())
        self.assertEqual(self.store().remember("client", "order 1234 5678", agent="a")["text"], "order 1234 5678")

    def test_invalid_input_is_refused(self):
        store = self.store()
        for kwargs in ({"text": ""}, {"text": "   "}, {"text": "x" * 501}, {"text": 5}, {"text": "ok", "agent": "bad agent"},
                       {"text": "ok", "classification": "secret"}, {"text": "ok", "owner": "bad owner"}, {"text": "ok", "conversation_id": "x y"}):
            args = {"text": "ok", "agent": "a", **kwargs}
            with self.assertRaises(Error, msg=str(kwargs)):
                store.remember("client", args.pop("text"), **args)
        self.assertEqual(self.events, [])

    def test_the_store_is_bounded(self):
        store = self.store()
        with mock.patch.object(memory, "MAX_ENTRIES", 3):
            for index in range(3):
                store.remember("client", f"fact {index}", agent="a")
            with self.assertRaisesRegex(Error, "full"):
                store.remember("client", "one too many", agent="a")


class DeleteAndAdminTests(Case):
    def test_a_person_deletes_what_they_own_and_nothing_else(self):
        store = self.store()
        mine = store.remember("client", "my preference", agent="a", owner=ALICE)
        theirs = store.remember("client", "bob's preference", agent="a", owner="bob@client.example")
        unowned = store.remember("client", "general fact", agent="a")
        self.events.clear()
        for entry in (theirs, unowned):
            with self.assertRaisesRegex(Error, "only the owner"):
                store.delete(entry["id"], requested_by=ALICE)
        self.assertEqual(self.events, [])
        self.assertEqual(store.delete(mine["id"], requested_by=ALICE), {"deleted": mine["id"], "scope": "client"})
        self.assertEqual(self.events[0][:2], ("memory.delete", ALICE))
        self.assertEqual(self.events[0][2]["textDigest"], mine["digest"])
        self.assertNotIn("preference", json.dumps(self.events))
        self.assertEqual(sorted(e["id"] for e in store.list(["client"])), sorted([theirs["id"], unowned["id"]]))
        with self.assertRaisesRegex(Error, "no such memory"):
            store.delete(mine["id"], requested_by=ALICE)

    def test_a_person_can_list_just_their_own_entries(self):
        store = self.store()
        store.remember("client", "alice thing", agent="a", owner=ALICE)
        store.remember("client", "bob thing", agent="a", owner="bob@client.example")
        self.assertEqual([e["text"] for e in store.list(["client"], owner=ALICE)], ["alice thing"])

    def test_an_administrator_can_delete_anything_and_it_is_ledgered_as_such(self):
        store = self.store()
        entry = store.remember("client", "someone's note", agent="a", owner="bob@client.example")
        self.events.clear()
        store.delete(entry["id"], requested_by=ADMIN, admin=True)
        self.assertEqual((self.events[0][0], self.events[0][2]["admin"]), ("memory.delete", True))

    def test_export_and_purge_are_admin_actions_that_are_ledgered(self):
        store = self.store()
        for scope in ("client", "agent:a", "conversation:c-1"):
            store.remember(scope, f"fact in {scope}", agent="a")
        self.events.clear()
        exported = store.export(requested_by=ADMIN)
        self.assertEqual((exported["tenant"], len(exported["entries"])), ("acme-prod", 3))
        self.assertEqual(self.events[0], ("memory.export", ADMIN, {"entries": 3}))
        self.assertEqual(store.purge(requested_by=ADMIN, scope="conversation:c-1"), {"purged": 1})
        self.assertEqual(self.events[-1], ("memory.purge", ADMIN, {"entries": 1, "scope": "conversation:c-1"}))
        self.assertEqual(store.purge(requested_by=ADMIN), {"purged": 2})
        self.assertEqual(store.list(["client", "agent:a"]), [])
        self.assertEqual(store.purge(requested_by=ADMIN), {"purged": 0})
        for bad in (" ", None):
            with self.assertRaises(Error):
                store.export(requested_by=bad)
        with self.assertRaises(Error):
            store.purge(requested_by=ADMIN, scope="everything")


class CommandTests(Case):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([str(self.root), "--tenant", "acme-prod", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_list_export_delete_purge_through_the_runtime_command_with_ledger_events(self):
        store = memory.MemoryStore(self.root, tenant="acme-prod")
        keep = store.remember("client", "client fact", agent="a")
        drop = store.remember("agent:a", "agent fact", agent="a")
        code, out, _ = self.run_cli("list", "--scope", "client")
        self.assertEqual((code, [e["text"] for e in json.loads(out)]), (0, ["client fact"]))
        code, out, _ = self.run_cli("export", "--by", ADMIN)
        self.assertEqual((code, len(json.loads(out)["entries"])), (0, 2))
        code, out, _ = self.run_cli("delete", drop["id"], "--by", ADMIN)
        self.assertEqual((code, json.loads(out)["deleted"]), (0, drop["id"]))
        code, out, _ = self.run_cli("purge", "--by", ADMIN)
        self.assertEqual((code, json.loads(out)), (0, {"purged": 1}))
        events = [json.loads(line) for line in (self.root.parent / "audit.jsonl").read_text().splitlines()]
        self.assertEqual([e["action"] for e in events], ["memory.export", "memory.delete", "memory.purge"])
        self.assertTrue(all(e["performedBy"] == ADMIN for e in events))
        self.assertTrue(govern.verify_audit_chain(self.root.parent / "audit.jsonl")["valid"])
        self.assertNotIn("fact", (self.root.parent / "audit.jsonl").read_text())
        self.assertEqual(keep["id"][:2], "m-")

    def test_failures_are_clean_and_the_tenant_defaults_from_the_desired_state(self):
        code, _, err = self.run_cli("delete", "m-000000000000", "--by", ADMIN)
        self.assertEqual(code, 1)
        self.assertIn("Runtime memory failed", err)
        code, _, err = self.run_cli("list", "--scope", "everyone")
        self.assertEqual(code, 1)
        with tempfile.TemporaryDirectory() as tmp:
            desired = pathlib.Path(tmp) / "d.json"
            desired.write_text(json.dumps({"source": {"client": "Acme Dental"}}))
            self.assertEqual(cli.default_tenant(desired), "acme-dental")
            self.assertEqual(cli.default_tenant(pathlib.Path(tmp) / "missing.json"), "default")

    def test_the_operator_command_validates_before_touching_docker_and_passes_arguments(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if command[:2] == ["docker", "ps"]:
                return mock.Mock(returncode=0, stdout="gov1\n", stderr="")
            return mock.Mock(returncode=0, stdout="[]\n", stderr="")

        with mock.patch.object(operator, "_run", fake_run), mock.patch.object(operator, "_require_command"), \
                contextlib.redirect_stdout(io.StringIO()):
            operator.runtime_memory("acme-dev", "list", scopes=("client", "agent:receptionist"))
            operator.runtime_memory("acme-dev", "delete", memory_id="m-0123456789ab", by=ADMIN)
            operator.runtime_memory("acme-dev", "purge", scopes=("conversation:c-1",), by=ADMIN)
        self.assertEqual(calls[1][-5:], ["list", "--scope", "client", "--scope", "agent:receptionist"])
        self.assertEqual(calls[3][-4:], ["delete", "m-0123456789ab", "--by", ADMIN])
        self.assertEqual(calls[5][-5:], ["purge", "--by", ADMIN, "--scope", "conversation:c-1"])
        before = len(calls)
        for kwargs in ({"command": "list"}, {"command": "list", "scopes": ("bad",)}, {"command": "delete", "memory_id": "nope", "by": ADMIN},
                       {"command": "delete", "memory_id": "m-0123456789ab"}, {"command": "export", "by": " "}, {"command": "wipe", "by": ADMIN}):
            args = dict(kwargs)
            with self.assertRaises(ValueError, msg=str(kwargs)):
                operator.runtime_memory("acme-dev", args.pop("command"), **args)
        self.assertEqual(len(calls), before)


if __name__ == "__main__":
    unittest.main()

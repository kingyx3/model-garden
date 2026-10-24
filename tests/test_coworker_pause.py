"""Credential-free checks for the owner's emergency pause of an AI coworker (#232): store, governed boundary, worker, chat and inbox."""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
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


pause = _load("pause_under_test", "platform/core/pause.py")
compiler = _load("compile_workspace_for_pause", "scripts/compile-workspace.py")
runtime_module = _load("governed_mcp_for_pause", "platform/runtime/governed_mcp.py")
ingress = _load("ingress_for_pause", "platform/channels/ingress.py")
inbox = _load("inbox_for_pause", "platform/core/inbox.py")
worker_tests = _load("worker_tests_for_pause", "tests/test_runtime_worker.py")
text = ingress.TEXT
NOW = 1_800_000_000
DIRECTORY = {"slack": {"T01/UADMIN": {"identity": "alice@acme.example", "roles": ["admin"]},
                       "T01/UMEMBER": {"identity": "bob@acme.example"}}}


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def test_no_file_is_not_paused_and_pause_survives_a_new_store_object(self):
        self.assertEqual(pause.PauseStore(self.root).status(), {"paused": False})
        pause.PauseStore(self.root).pause("alice@acme.example", "wrong data\x00 in\nthe CRM", NOW)
        again = pause.PauseStore(self.root).status()  # a restart reads the same file
        self.assertEqual((again["paused"], again["by"], again["at"], again["reason"]), (True, "alice@acme.example", NOW, "wrong data  in the CRM"))
        pause.PauseStore(self.root).resume("alice@acme.example", NOW + 5)
        self.assertFalse(pause.PauseStore(self.root).is_paused())

    def test_missing_governance_volume_fails_closed_and_admin_cannot_recreate_it(self):
        missing = self.root / "not-mounted"
        store = pause.PauseStore(missing)
        self.assertTrue(store.is_paused())
        self.assertIsNone(store.status()["by"])
        for command in ("pause", "resume"):
            with self.subTest(command=command), self.assertRaises(OSError):
                if command == "pause":
                    store.pause("owner@client.example", "incident", NOW)
                else:
                    store.resume("owner@client.example", NOW)
        self.assertFalse(missing.exists(), "pause command must not invent a replacement volume")
        # First boot with a real mounted volume and no pause.json is still allowed.
        missing.mkdir(mode=0o700)
        self.assertEqual(store.status(), {"paused": False})
        store.pause("owner@client.example", "incident", NOW)
        self.assertTrue(store.is_paused())

    def test_governance_root_disappearing_or_replaced_during_first_boot_read_fails_closed(self):
        original_lstat = pathlib.Path.lstat
        for mutation in ("removed", "replaced"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                root = pathlib.Path(tmp) / "governance"
                root.mkdir(mode=0o700)
                state = root / "pause.json"

                def mutate_on_state_lookup(path):
                    if path == state:
                        if mutation == "removed":
                            root.rmdir()
                        else:
                            root.rename(root.with_name("detached"))
                            root.mkdir(mode=0o700)
                        raise FileNotFoundError("pause state absent during mount change")
                    return original_lstat(path)

                with mock.patch.object(pathlib.Path, "lstat", autospec=True,
                                       side_effect=mutate_on_state_lookup):
                    self.assertTrue(pause.PauseStore(root).is_paused())

    def test_existing_unpaused_state_fails_closed_after_governance_root_swap(self):
        # A legitimate false state must not authorize work after the mounted
        # directory disappears or is replaced *after* JSON was read. Earlier
        # inode checks alone cannot catch this race.
        original_loads = pause.json.loads
        for mutation in ("removed", "replaced"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                root = pathlib.Path(tmp) / "governance"
                root.mkdir(mode=0o700)
                store = pause.PauseStore(root)
                store.resume("owner@client.example", NOW)
                self.assertFalse(store.is_paused())
                swapped = False

                def swap_after_decode(raw):
                    nonlocal swapped
                    document = original_loads(raw)
                    if not swapped:
                        swapped = True
                        root.rename(root.with_name("detached"))
                        if mutation == "replaced":
                            root.mkdir(mode=0o700)
                            replacement = root / "pause.json"
                            replacement.write_text('{"paused": false}', encoding="utf-8")
                            replacement.chmod(0o600)
                    return document

                with mock.patch.object(pause.json, "loads", side_effect=swap_after_decode):
                    state = store.status()
                self.assertTrue(swapped, "test must force the mount race")
                self.assertTrue(state["paused"], "stale unpaused state must not grant authority")
                self.assertIsNone(state["by"], "an untrusted read must be reported fail-closed")

    def test_an_unreadable_or_malformed_state_fails_safe_as_paused(self):
        for bad in (b"{not json", b"[]", b'{"paused": "no"}', b""):
            (self.root / "pause.json").write_bytes(bad)
            self.assertTrue(pause.PauseStore(self.root).is_paused(), bad)
        (self.root / "pause.json").unlink()
        (self.root / "pause.json").mkdir()  # a directory cannot be read as a file
        self.assertTrue(pause.PauseStore(self.root).is_paused())

    def test_pause_reader_refuses_untrusted_false_state_without_following_links(self):
        # A valid-looking 'paused: false' must never override a safety hold
        # through an externally controlled symlink, hardlink or public file.
        for tamper in ("symlink", "dangling", "hardlink", "permissive", "fifo", "oversized"):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                root = pathlib.Path(tmp)
                state = root / "pause.json"
                forged = root / "external.json"
                forged.write_text('{"paused": false}', encoding="utf-8")
                forged.chmod(0o600)
                if tamper == "symlink":
                    state.symlink_to(forged)
                elif tamper == "dangling":
                    state.symlink_to(root / "missing.json")
                elif tamper == "hardlink":
                    os.link(forged, state)
                elif tamper == "permissive":
                    state.write_text('{"paused": false}', encoding="utf-8")
                    state.chmod(0o644)
                elif tamper == "fifo":
                    os.mkfifo(state)
                else:
                    state.write_text('{"paused": false}' + " " * 10000, encoding="utf-8")
                    state.chmod(0o600)
                self.assertTrue(pause.PauseStore(root).is_paused(), tamper)

    def test_pause_state_disappearing_after_inspection_fails_closed(self):
        store = pause.PauseStore(self.root)
        store.pause("owner@client.example", "incident", NOW)
        # A removed file during the reader's open is not an untouched new
        # workspace; it must not be confused with the initial missing state.
        with mock.patch.object(pause.os, "open", side_effect=FileNotFoundError("vanished")):
            self.assertTrue(store.is_paused())

    def test_pause_reader_rejects_untrusted_parent_and_foreign_state_owner(self):
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            root = pathlib.Path(tmp)
            pause.PauseStore(root).resume("owner@client.example", NOW)
            alias = self.root / "fake-governance-root"
            alias.symlink_to(root, target_is_directory=True)
            try:
                self.assertTrue(pause.PauseStore(alias).is_paused())
                with self.assertRaises(OSError):
                    pause.PauseStore(alias).resume("owner@client.example", NOW + 1)
            finally:
                alias.unlink()
            with mock.patch.object(pause.os, "geteuid", return_value=os.geteuid() + 1):
                self.assertTrue(pause.PauseStore(root).is_paused())

    def test_untrusted_state_cannot_be_replaced_by_a_normal_resume_or_pause(self):
        # Read-side deny is not enough: a valid admin command must not turn a
        # corrupt/linked state with an unknown cap cause into an unpaused state.
        for damaged in ("invalid-json", "symlink"):
            with self.subTest(damaged=damaged), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                root = pathlib.Path(tmp)
                store = pause.PauseStore(root)
                store.pause("runtime:worker", "monthly cap", NOW,
                            cause="spend-cap", window="2027-01")
                state = root / "pause.json"
                state.unlink()
                if damaged == "invalid-json":
                    state.write_bytes(b"{broken")
                    state.chmod(0o600)
                else:
                    outside = root / "external.json"
                    outside.write_text('{"paused": false}', encoding="utf-8")
                    outside.chmod(0o600)
                    state.symlink_to(outside)
                for method in ("pause", "resume"):
                    with self.subTest(method=method), self.assertRaisesRegex(ValueError, "untrusted"):
                        if method == "pause":
                            store.pause("owner@client.example", "override", NOW + 1)
                        else:
                            store.resume("owner@client.example", NOW + 1)
                self.assertTrue(store.is_paused())

    def test_untrusted_lock_fails_closed_and_blocks_state_changes(self):
        for tamper in ("symlink", "hardlink", "permissive", "fifo"):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                root = pathlib.Path(tmp)
                store = pause.PauseStore(root)
                store.pause("owner@client.example", "incident", NOW)
                lock = root / ".pause.lock"
                if tamper == "symlink":
                    lock.rename(root / "backing.lock")
                    lock.symlink_to(root / "backing.lock")
                elif tamper == "hardlink":
                    os.link(lock, root / "other-lock")
                elif tamper == "permissive":
                    lock.chmod(0o666)
                else:
                    lock.unlink()
                    os.mkfifo(lock)
                self.assertTrue(store.is_paused())
                with self.assertRaises(OSError):
                    store.resume("owner@client.example", NOW + 1)
                self.assertTrue(store.is_paused())

    def test_manual_resume_or_repause_racing_worker_cannot_erase_spend_cap(self):
        # A manual command has already checked the old state, then the worker
        # tries to assert the cap. Without an interprocess transaction lock,
        # the worker can write first and the delayed manual write erases it.
        for command in ("resume", "pause"):
            with self.subTest(command=command), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                root = pathlib.Path(tmp)
                manual, worker = pause.PauseStore(root), pause.PauseStore(root)
                if command == "resume":
                    manual.pause("owner@client.example", "incident", NOW)
                original_write = manual._write
                manual_ready, allow_manual = threading.Event(), threading.Event()
                errors = []

                def delayed_write(document):
                    manual_ready.set()
                    if not allow_manual.wait(5):
                        raise TimeoutError("test did not release manual pause write")
                    return original_write(document)

                def run(action):
                    try:
                        action()
                    except BaseException as exc:
                        errors.append(exc)

                owner_action = (lambda: manual.resume("owner@client.example", NOW + 1)) if command == "resume" else (
                    lambda: manual.pause("owner@client.example", "manual override", NOW + 1))
                first = threading.Thread(target=run, args=(owner_action,), daemon=True)
                second = threading.Thread(target=run, args=(lambda: worker.pause(
                    "runtime:worker", "monthly cap", NOW + 2, cause="spend-cap", window="2027-01"),), daemon=True)
                with mock.patch.object(manual, "_write", side_effect=delayed_write):
                    first.start()
                    try:
                        self.assertTrue(manual_ready.wait(3), "manual action did not reach the write")
                        second.start()
                        second.join(0.1)
                        self.assertTrue(second.is_alive(), "worker must wait for the manual state transaction")
                    finally:
                        allow_manual.set()
                        first.join(5)
                        if second.ident is not None:
                            second.join(5)
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())
                self.assertEqual(errors, [])
                state = pause.PauseStore(root).status()
                self.assertEqual((state["paused"], state["cause"], state["by"]),
                                 (True, "spend-cap", "runtime:worker"))
                self.assertEqual((root / ".pause.lock").stat().st_mode & 0o777, 0o600)

    def test_owner_pause_after_reconciliation_is_not_lost(self):
        # Reconciliation and a new owner pause must be one ordered sequence.
        worker, owner = pause.PauseStore(self.root), pause.PauseStore(self.root)
        worker.pause("runtime:worker", "cap", NOW, cause="spend-cap", window="2027-01")
        original_write = worker._write
        ready, release = threading.Event(), threading.Event()
        errors = []

        def delayed_write(document):
            ready.set()
            if not release.wait(5):
                raise TimeoutError("test did not release reconciliation")
            return original_write(document)

        def run(action):
            try:
                action()
            except BaseException as exc:
                errors.append(exc)

        clearing = threading.Thread(target=run, args=(lambda: worker.clear_spend_cap(
            NOW + 1, expected_window="2027-01"),), daemon=True)
        pausing = threading.Thread(target=run, args=(lambda: owner.pause(
            "owner@client.example", "incident", NOW + 2),), daemon=True)
        with mock.patch.object(worker, "_write", side_effect=delayed_write):
            clearing.start()
            try:
                self.assertTrue(ready.wait(3))
                pausing.start()
                pausing.join(0.1)
                self.assertTrue(pausing.is_alive(), "owner transition must wait for the worker's transaction")
            finally:
                release.set()
                clearing.join(5)
                if pausing.ident is not None:
                    pausing.join(5)
        self.assertEqual(errors, [])
        self.assertFalse(clearing.is_alive())
        self.assertFalse(pausing.is_alive())
        state = owner.status()
        self.assertEqual((state["paused"], state["by"]), (True, "owner@client.example"))
        self.assertNotIn("cause", state)

    def test_lock_acquisition_failure_preserves_the_existing_pause(self):
        store = pause.PauseStore(self.root)
        store.pause("owner@client.example", "incident", NOW)
        with mock.patch.object(pause.fcntl, "flock", side_effect=OSError("lock unavailable")):
            with self.assertRaises(OSError):
                store.resume("owner@client.example", NOW + 1)
        self.assertTrue(store.is_paused())

    def test_who_is_required_and_the_reason_is_bounded(self):
        with self.assertRaises(ValueError):
            pause.PauseStore(self.root).pause(" ", "x", NOW)
        self.assertEqual(len(pause.PauseStore(self.root).pause("a@b.example", "x" * 5000, NOW)["reason"]), pause.MAX_REASON)
        self.assertEqual(oct((self.root / "pause.json").stat().st_mode & 0o777), "0o600")


class BoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        desired = compiler.compile_workspace(ROOT / "examples/workspace", "receptionist")[0]
        (self.root / "desired.json").write_text(json.dumps(desired), encoding="utf-8")
        binding = {"connectors": {"kb": {"provider": "text-knowledge", "tier": 0}, "crm": {"provider": "built-in-crm", "tier": 0}}}
        (self.root / "manifest.json").write_text(json.dumps(runtime_module.CAPABILITIES.build_manifest(desired, binding)))
        self.environ = {"MODEL_GARDEN_CAPABILITY_MANIFEST": str(self.root / "manifest.json")}
        self.runtime = runtime_module.GovernedToolRuntime(self.root / "desired.json", self.root / "audit.jsonl", self.root / "approvals", environ=self.environ)

    def events(self):
        path = self.root / "audit.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_a_material_tool_is_denied_and_ledgered_while_paused_and_a_read_still_works(self):
        pause.PauseStore(self.root).pause("alice@acme.example", "incident", NOW)
        with self.assertRaisesRegex(PermissionError, "paused by its owner"):
            self.runtime.crm_create(fields={"name": "Demo"})
        denial = self.events()[-1]
        self.assertEqual((denial["outcome"], denial["details"]["reason"]), ("deny", "coworker-paused"))
        self.assertEqual(self.runtime.kb_search(query="opening hours")["status"], "executed")  # a read is not a material write

    def test_resume_restores_the_normal_policy_decision(self):
        store = pause.PauseStore(self.root)
        store.pause("alice@acme.example", "incident", NOW)
        with self.assertRaises(PermissionError):
            self.runtime.crm_create(fields={"name": "Demo"})
        store.resume("alice@acme.example", NOW + 1)
        try:
            self.runtime.crm_create(fields={"name": "Demo"})  # now the ordinary policy answers (an approval, or its own refusal)
        except PermissionError:
            pass
        self.assertNotIn("coworker-paused", [event.get("details", {}).get("reason") for event in self.events()[1:]])

    def test_a_corrupt_state_denies_material_tools(self):
        (self.root / "pause.json").write_bytes(b"garbage")
        with self.assertRaises(PermissionError):
            self.runtime.crm_create(fields={"name": "Demo"})
        self.assertEqual(self.events()[-1]["details"]["reason"], "coworker-paused")

    def test_an_unknown_tool_counts_as_material_and_the_check_is_per_deployment_root(self):
        self.assertFalse(self.runtime._paused_for("crm.create"))  # nothing paused yet
        pause.PauseStore(self.root).pause("alice@acme.example", "", NOW)
        self.assertTrue(self.runtime._paused_for("not.a.tool"))
        self.assertTrue(self.runtime._paused_for("crm.create"))
        self.assertFalse(self.runtime._paused_for("kb.search"))
        other = pathlib.Path(tempfile.mkdtemp(dir=self.root))
        self.assertFalse(runtime_module.GovernedToolRuntime(self.root / "desired.json", other / "audit.jsonl", other / "approvals", environ={})._paused_for("crm.create"))


class WorkerTests(worker_tests.Case):
    def test_scheduled_event_mailbox_and_crm_work_start_nothing_while_paused_and_resume_afterwards(self):
        runtime = self.make({"mailbox": {"addresses": ["help@acme.example"], "trustedAuthservId": "mx.acme.example", "owner": "owner@acme.example"}, "owners": {"owner@acme.example": ["kb.search"]}}, coworker=object())
        pause.PauseStore(self.data).pause("alice@acme.example", "incident", T0)
        for job in (runtime.run_scheduler, runtime.run_events, runtime.run_mailbox, runtime.run_crm_sync):
            self.assertEqual(job(T0), {"skipped": "paused"}, job.__name__)
        pause.PauseStore(self.data).resume("alice@acme.example", T0 + 1)
        self.assertNotEqual(runtime.run_scheduler(T0 + 2), {"skipped": "paused"})
        self.assertNotEqual(runtime.run_events(T0 + 2), {"skipped": "paused"})


T0 = worker_tests.T0


class FakeAdapter:
    def __init__(self):
        self.sent: list = []
        self.next = None

    def parse_inbound(self, body, headers):
        return self.next

    def send_reply(self, reply):
        self.sent.append(reply)
        return "ts"


class ChatTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.adapter, self.asked, self.events, self.counter = FakeAdapter(), [], [], 0
        self.ingress = ingress.ChannelIngress(
            {"slack": self.adapter}, text.PrincipalDirectory(DIRECTORY), respond=self.respond, decide=lambda *a, **k: {},
            audit=lambda action, who, details: self.events.append((action, who, details)), pause=self.command,
        )

    def command(self, admin, verb, reason):
        store = pause.PauseStore(self.root)
        store.pause(admin, reason, NOW) if verb == "pause" else store.resume(admin, NOW)
        return f"{verb}d"

    def respond(self, inbound, principal):
        self.asked.append(inbound.text)
        return pause.HOLDING if pause.PauseStore(self.root).is_paused() else "the coworker answered"

    def say(self, words, *, user="UADMIN", conversation="D0DIRECT01"):
        self.counter += 1
        self.adapter.next = text.InboundMessage("slack", conversation, None, f"m{self.counter}", text.ChannelIdentity("slack", user, "T01"), words)
        self.ingress.message("slack", b"{}", {})
        return self.adapter.sent[-1].text

    def test_a_mapped_admin_pauses_and_resumes_in_a_direct_chat_with_the_reason_kept(self):
        self.assertEqual(self.say("pause coworker: wrong customer data"), "paused")
        state = pause.PauseStore(self.root).status()
        self.assertEqual((state["paused"], state["by"], state["reason"]), (True, "alice@acme.example", "wrong customer data"))
        self.assertEqual(self.say("hello", user="UMEMBER"), pause.HOLDING)
        self.assertEqual(self.say("Resume coworker"), "resumed")
        self.assertFalse(pause.PauseStore(self.root).is_paused())
        self.assertIn("channel.coworker-pause", [event[0] for event in self.events])

    def test_a_member_a_stranger_or_a_shared_channel_cannot_pause_and_gets_the_same_neutral_words(self):
        neutral = "Only an administrator can pause or resume me, in a direct message."
        for user, conversation in (("UMEMBER", "D0DIRECT01"), ("USTRANGER", "D0DIRECT01"), ("UADMIN", "C0TEAM001")):
            self.assertEqual(self.say("pause coworker", user=user, conversation=conversation), neutral)
        self.assertFalse(pause.PauseStore(self.root).is_paused())
        self.assertEqual(self.asked, [])

    def test_ordinary_words_that_merely_mention_pausing_go_to_the_coworker(self):
        self.assertEqual(self.say("can you pause the coworker later today?"), "the coworker answered")
        self.assertEqual(self.say("please pause coworkers' access"), "the coworker answered")
        self.assertFalse(pause.PauseStore(self.root).is_paused())

    def test_a_member_cannot_resume_a_paused_coworker(self):
        pause.PauseStore(self.root).pause("alice@acme.example", "x", NOW)
        self.say("resume coworker", user="UMEMBER")
        self.assertTrue(pause.PauseStore(self.root).is_paused())


class InboxTests(unittest.TestCase):
    def test_a_pause_is_the_first_inbox_item_for_an_admin_and_hidden_from_a_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            collect = lambda: inbox.collect(approvals=root / "approvals", handoffs=root / "handoffs", governance=root, now=NOW)  # noqa: E731
            self.assertEqual(collect()["counts"]["paused"], 0)
            pause.PauseStore(root).pause("alice@acme.example", "incident", NOW)
            snapshot = collect()
            self.assertEqual((snapshot["items"][0]["kind"], snapshot["counts"]["paused"]), ("paused", 1))
            self.assertIn("PAUSED", inbox.format_text(snapshot))
            self.assertIn("incident", inbox.format_chat(inbox.for_person(snapshot, "alice@acme.example", everything=True)))
            self.assertEqual(inbox.for_person(snapshot, "bob@acme.example", everything=False)["items"], [])


class GatewayConfigTests(unittest.TestCase):
    def test_gateway_refuses_to_claim_a_cap_pause_was_resumed_or_replaced(self):
        import types
        gateway = _load("gateway_spend_cap_control", "platform/runtime/gateway.py")
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            audit = []
            service = types.SimpleNamespace(data=root, clock=lambda: NOW, audit=lambda *event: audit.append(event))
            store = pause.PauseStore(root)
            store.pause("runtime:worker", "monthly cap", NOW, cause="spend-cap", window="2027-01")
            for verb in ("pause", "resume"):
                reply = gateway.Gateway._pause_command(service, "alice@acme.example", verb, "override")
                self.assertIn("Still paused", reply)
                self.assertTrue(store.is_paused())
                self.assertEqual(store.status()["cause"], "spend-cap")
            self.assertEqual([event[0] for event in audit], ["coworker.pause-denied", "coworker.resume-denied"])

    def test_the_holding_message_is_validated_and_the_deployer_ships_the_pause_files(self):
        gateway = _load("gateway_for_pause", "platform/runtime/gateway.py")
        self.assertEqual(gateway.load_config({"pausedMessage": " Back soon. "})["pausedMessage"], "Back soon.")
        self.assertIsNone(gateway.load_config({})["pausedMessage"])
        for bad in ("", "x" * 501, 7):
            with self.assertRaises(gateway.GatewayConfigError):
                gateway.load_config({"pausedMessage": bad})
        deploy = (ROOT / "scripts/deploy-client-docker.py").read_text()
        for shipped in ("platform/core/pause.py", "scripts/runtime-pause.py"):
            self.assertIn(shipped, deploy)


class OperatorScriptTests(unittest.TestCase):
    def run_script(self, root, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts/runtime-pause.py"), str(root), "--audit-path", str(root / "audit.jsonl"), *args],
                              capture_output=True, text=True)

    def test_operator_cannot_clear_or_replace_a_metering_spend_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            store = pause.PauseStore(root)
            store.pause("runtime:worker", "monthly cap", NOW, cause="spend-cap", window="2027-01")
            resume = self.run_script(root, "resume", "--by", "jordan@acme.example")
            repause = self.run_script(root, "pause", "--by", "jordan@acme.example", "--reason", "bypass")
            self.assertEqual((resume.returncode, repause.returncode), (2, 2))
            self.assertTrue(store.status()["paused"])
            self.assertEqual(store.status()["cause"], "spend-cap")
            self.assertFalse((root / "audit.jsonl").exists())  # neither action was recorded as successful

    def test_pause_status_and_resume_are_ledgered_under_the_named_person(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.assertFalse(json.loads(self.run_script(root, "status").stdout)["paused"])
            self.assertEqual(self.run_script(root, "pause", "--by", "jordan@acme.example", "--reason", "incident").returncode, 0)
            self.assertTrue(json.loads(self.run_script(root, "status").stdout)["paused"])
            self.assertEqual(self.run_script(root, "resume", "--by", "jordan@acme.example").returncode, 0)
            actions = [json.loads(line) for line in (root / "audit.jsonl").read_text().splitlines()]
            self.assertEqual([(e["action"], e["performedBy"]) for e in actions if "coworker." in str(e.get("action"))],
                             [("coworker.pause", "jordan@acme.example"), ("coworker.resume", "jordan@acme.example")])
            self.assertEqual(self.run_script(root, "pause", "--by", " ").returncode, 2)


if __name__ == "__main__":
    unittest.main()

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

    def test_deleting_written_pause_or_resume_state_never_looks_like_first_boot(self):
        # A missing state is only valid before the first durable transition.
        # Deleting an existing pause must not silently re-enable writes.
        for transition in ("pause", "resume"):
            with self.subTest(transition=transition), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                root = pathlib.Path(tmp)
                store = pause.PauseStore(root)
                self.assertFalse(store.is_paused())
                if transition == "pause":
                    store.pause("owner@client.example", "incident", NOW)
                else:
                    store.resume("owner@client.example", NOW)
                self.assertTrue((root / ".pause.initialized").is_file())
                (root / "pause.json").unlink()
                state = pause.PauseStore(root).status()
                self.assertTrue(state["paused"])
                self.assertIsNone(state["by"])
                with self.assertRaisesRegex(ValueError, "untrusted"):
                    store.resume("owner@client.example", NOW + 1)

    def test_untrusted_initialization_marker_fails_closed_even_with_valid_unpaused_state(self):
        for tamper in ("symlink", "hardlink", "permissive", "directory"):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                root = pathlib.Path(tmp)
                store = pause.PauseStore(root)
                store.resume("owner@client.example", NOW)
                marker = root / ".pause.initialized"
                if tamper == "symlink":
                    marker.rename(root / "outside")
                    marker.symlink_to(root / "outside")
                elif tamper == "hardlink":
                    os.link(marker, root / "other")
                elif tamper == "permissive":
                    marker.chmod(0o644)
                else:
                    marker.unlink()
                    marker.mkdir()
                self.assertTrue(store.is_paused())
                with self.assertRaises((ValueError, OSError)):
                    store.resume("owner@client.example", NOW + 1)

    def test_initialization_marker_appearing_during_first_boot_read_fails_closed(self):
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            root = pathlib.Path(tmp)
            store = pause.PauseStore(root)
            marker = root / ".pause.initialized"
            original_lstat = pathlib.Path.lstat
            appeared = False

            def race_on_state_lookup(path):
                nonlocal appeared
                if path == root / "pause.json" and not appeared:
                    appeared = True
                    marker.touch(mode=0o600)
                    raise FileNotFoundError("state missing during initialization")
                return original_lstat(path)

            with mock.patch.object(pathlib.Path, "lstat", autospec=True,
                                   side_effect=race_on_state_lookup):
                result = store.status()
            self.assertTrue(appeared)
            self.assertTrue(result["paused"])

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

    def test_state_replaced_or_removed_during_decode_never_authorizes_stale_unpause(self):
        # pause/resume atomically replace pause.json; a reader that already
        # opened the old unpaused inode must not return stale authority.
        original_loads = pause.json.loads
        for mutation in ("replaced", "removed"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                root = pathlib.Path(tmp)
                store = pause.PauseStore(root)
                store.resume("owner@client.example", NOW)
                old_inode = (root / "pause.json").stat().st_ino
                changed = False

                def mutate_after_decode(raw):
                    nonlocal changed
                    document = original_loads(raw)
                    if not changed:
                        changed = True
                        state = root / "pause.json"
                        if mutation == "removed":
                            state.unlink()
                        else:
                            replacement = root / "replacement.json"
                            replacement.write_text('{"paused": true, "by": "owner@client.example"}',
                                                   encoding="utf-8")
                            replacement.chmod(0o600)
                            os.replace(replacement, state)
                            self.assertNotEqual(state.stat().st_ino, old_inode)
                    return document

                with mock.patch.object(pause.json, "loads", side_effect=mutate_after_decode):
                    result = store.status()
                self.assertTrue(changed, "the test must force the state transition")
                self.assertTrue(result["paused"], "stale unpause must fail closed")
                self.assertIsNone(result["by"], "a raced read must not report trusted authority")

    def test_first_boot_state_appearing_during_missing_lookup_fails_closed(self):
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            root = pathlib.Path(tmp)
            state = root / "pause.json"
            original_lstat = pathlib.Path.lstat
            appeared = False

            def create_pause_during_lookup(path):
                nonlocal appeared
                if path == state and not appeared:
                    appeared = True
                    state.write_text('{"paused": true, "by": "owner@client.example"}',
                                     encoding="utf-8")
                    state.chmod(0o600)
                    raise FileNotFoundError("state was absent at the first lookup")
                return original_lstat(path)

            with mock.patch.object(pathlib.Path, "lstat", autospec=True,
                                   side_effect=create_pause_during_lookup):
                result = pause.PauseStore(root).status()
            self.assertTrue(appeared)
            self.assertTrue(result["paused"], "first-boot absence must not mask a new pause")
            self.assertIsNone(result["by"])
            self.assertTrue(pause.PauseStore(root).is_paused())

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

                def delayed_write(document, *transaction):
                    manual_ready.set()
                    if not allow_manual.wait(5):
                        raise TimeoutError("test did not release manual pause write")
                    return original_write(document, *transaction)

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

        def delayed_write(document, *transaction):
            ready.set()
            if not release.wait(5):
                raise TimeoutError("test did not release reconciliation")
            return original_write(document, *transaction)

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

    def test_root_swap_after_lock_acquisition_refuses_resume_of_replacement(self):
        # A lock on a detached governance mount must not authorize the new
        # mount's state, even if both directories have private permissions.
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            root = pathlib.Path(tmp) / "governance"
            root.mkdir(mode=0o700)
            store = pause.PauseStore(root)
            store.pause("owner@client.example", "incident", NOW)
            original_flock = pause.fcntl.flock
            swapped = False

            def replace_mount_after_lock(fd, operation):
                nonlocal swapped
                result = original_flock(fd, operation)
                if not swapped:
                    swapped = True
                    root.rename(root.with_name("detached"))
                    root.mkdir(mode=0o700)
                    state = root / "pause.json"
                    state.write_text('{"paused": true, "by": "owner@client.example"}',
                                     encoding="utf-8")
                    state.chmod(0o600)
                return result

            with mock.patch.object(pause.fcntl, "flock", side_effect=replace_mount_after_lock):
                with self.assertRaises(OSError):
                    store.resume("owner@client.example", NOW + 1)
            self.assertTrue(swapped)
            self.assertTrue(store.is_paused())
            self.assertTrue(pause.PauseStore(root.with_name("detached")).is_paused())

    def test_lock_replacement_while_acquiring_cannot_split_pause_transactions(self):
        store = pause.PauseStore(self.root)
        store.pause("owner@client.example", "incident", NOW)
        lock = self.root / ".pause.lock"
        original_flock = pause.fcntl.flock
        replaced = False

        def replace_lock_after_acquire(fd, operation):
            nonlocal replaced
            result = original_flock(fd, operation)
            if not replaced:
                replaced = True
                lock.unlink()
                lock.write_text("", encoding="utf-8")
                lock.chmod(0o600)
            return result

        with mock.patch.object(pause.fcntl, "flock", side_effect=replace_lock_after_acquire):
            with self.assertRaises(OSError):
                store.resume("owner@client.example", NOW + 1)
        self.assertTrue(replaced)
        self.assertTrue(store.is_paused())

    def test_mount_swap_at_atomic_replace_cannot_unpause_new_volume(self):
        # Pinning the directory fd prevents a TOCTOU redirect between the
        # final root check and os.replace. The detached mount may be updated,
        # but the newly attached volume must remain paused and caller fails.
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            root = pathlib.Path(tmp) / "governance"
            root.mkdir(mode=0o700)
            store = pause.PauseStore(root)
            store.pause("owner@client.example", "incident", NOW)
            original_replace = pause.os.replace
            swapped = False

            def swap_during_replace(src, dst, **kwargs):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    root.rename(root.with_name("detached"))
                    root.mkdir(mode=0o700)
                    state = root / "pause.json"
                    state.write_text('{"paused": true, "by": "owner@client.example"}',
                                     encoding="utf-8")
                    state.chmod(0o600)
                return original_replace(src, dst, **kwargs)

            with mock.patch.object(pause.os, "replace", side_effect=swap_during_replace):
                with self.assertRaises(OSError):
                    store.resume("owner@client.example", NOW + 1)
            self.assertTrue(swapped)
            self.assertTrue(store.is_paused())

    def test_lock_acquisition_failure_preserves_the_existing_pause(self):
        store = pause.PauseStore(self.root)
        store.pause("owner@client.example", "incident", NOW)
        with mock.patch.object(pause.fcntl, "flock", side_effect=OSError("lock unavailable")):
            with self.assertRaises(OSError):
                store.resume("owner@client.example", NOW + 1)
        self.assertTrue(store.is_paused())

    def test_bounded_owner_pause_and_resume_never_acknowledge_an_inflight_effect(self):
        store = pause.PauseStore(self.root)
        # An admitted governed write can hold the shared lease indefinitely.
        # Neither owner command may claim success while that lease is held.
        with store.effect_guard():
            with self.assertRaisesRegex(TimeoutError, "not acknowledged"):
                store.pause("owner@client.example", "emergency", NOW, lock_timeout_seconds=0.05)
            self.assertFalse(store.is_paused())
        self.assertTrue(store.pause("owner@client.example", "emergency", NOW,
                                    lock_timeout_seconds=0.1)["paused"])
        with store.effect_guard():
            with self.assertRaisesRegex(TimeoutError, "not acknowledged"):
                store.resume("owner@client.example", NOW + 1, lock_timeout_seconds=0.05)
            self.assertTrue(store.is_paused())
        self.assertFalse(store.resume("owner@client.example", NOW + 2,
                                      lock_timeout_seconds=0.1)["paused"])

    def test_owner_lock_wait_must_be_finite_and_bounded(self):
        store = pause.PauseStore(self.root)
        for bad in (0, -1, float("nan"), float("inf"), 31, True, "5"):
            with self.subTest(wait=repr(bad)), self.assertRaises(ValueError):
                store.pause("owner@client.example", "emergency", NOW,
                            lock_timeout_seconds=bad)
        self.assertFalse(store.is_paused())

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


    def test_owner_pause_waits_for_inflight_governed_effect_and_denies_new_effects(self):
        # A pause must not acknowledge while an already admitted side effect is
        # still running. Once acknowledged, no subsequent effect may start.
        entered, release, paused = threading.Event(), threading.Event(), threading.Event()
        effects, failures, results = [], [], []
        arguments = {
            "objectType": "contact", "objectId": "unmatched", "interactionType": "call",
            "channel": "voice", "direction": "inbound", "timestamp": "2026-10-20T10:00:00+08:00",
            "summary": "Synthetic call for pause-race testing",
        }

        def executor(_arguments):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release the in-flight effect")
            effects.append("completed")
            return {"externalObjectRef": "built-in-crm:interaction/test"}

        def write():
            try:
                results.append(self.runtime._execute("crm.log-interaction", arguments, executor, tier=0))
            except BaseException as exc:
                failures.append(exc)

        def owner_pause():
            try:
                pause.PauseStore(self.root).pause("owner@client.example", "incident", NOW)
            except BaseException as exc:
                failures.append(exc)
            finally:
                paused.set()

        writing = threading.Thread(target=write, daemon=True)
        stopping = threading.Thread(target=owner_pause, daemon=True)
        writing.start()
        try:
            self.assertTrue(entered.wait(3), "governed executor was not entered")
            stopping.start()
            self.assertFalse(paused.wait(0.15), "pause must wait for the admitted effect to finish")
        finally:
            release.set()
            writing.join(5)
            if stopping.ident is not None:
                stopping.join(5)
        self.assertFalse(writing.is_alive())
        self.assertFalse(stopping.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(effects, ["completed"])
        self.assertEqual(results[0]["status"], "executed")
        self.assertTrue(paused.is_set())
        self.assertTrue(pause.PauseStore(self.root).is_paused())
        with self.assertRaisesRegex(PermissionError, "paused by its owner"):
            self.runtime._execute("crm.log-interaction", arguments, lambda _: effects.append("late"), tier=0)
        self.assertEqual(effects, ["completed"])
        self.assertEqual(self.events()[-1]["details"]["reason"], "coworker-paused")

    def test_concurrent_admitted_effects_share_the_read_lease_before_pause(self):
        # The pause fence must not serialize independent provider calls.
        # An exclusive owner pause waits until *both* admitted effects finish.
        entered = [threading.Event(), threading.Event()]
        release, paused = threading.Event(), threading.Event()
        results, failures = [], []
        arguments = {
            "objectType": "contact", "objectId": "unmatched", "interactionType": "call",
            "channel": "voice", "direction": "inbound", "timestamp": "2026-10-20T10:00:00+08:00",
        }

        def write(index):
            def executor(_):
                entered[index].set()
                if not release.wait(5):
                    raise TimeoutError("test did not release the admitted effect")
                return {"externalObjectRef": f"built-in-crm:interaction/{index}"}
            try:
                results.append(self.runtime._execute("crm.log-interaction",
                               {**arguments, "summary": f"Synthetic call {index}"}, executor, tier=0))
            except BaseException as exc:
                failures.append(exc)

        def owner_pause():
            try:
                pause.PauseStore(self.root).pause("owner@client.example", "incident", NOW)
            except BaseException as exc:
                failures.append(exc)
            finally:
                paused.set()

        writers = [threading.Thread(target=write, args=(i,), daemon=True) for i in range(2)]
        stopping = threading.Thread(target=owner_pause, daemon=True)
        for writer in writers:
            writer.start()
        try:
            self.assertTrue(all(event.wait(3) for event in entered), "both governed effects must be admitted concurrently")
            stopping.start()
            self.assertFalse(paused.wait(0.15), "owner pause cannot acknowledge while effects remain")
        finally:
            release.set()
            for writer in writers:
                writer.join(5)
            if stopping.ident is not None:
                stopping.join(5)
        self.assertTrue(all(not writer.is_alive() for writer in writers))
        self.assertFalse(stopping.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual([result["status"] for result in results], ["executed", "executed"])
        self.assertTrue(pause.PauseStore(self.root).is_paused())

    def test_write_queued_behind_owner_pause_cannot_run_after_pause(self):
        # Force a pause transaction to hold the lock before the write arrives.
        store = pause.PauseStore(self.root)
        original_write = store._write
        ready, release, finished = threading.Event(), threading.Event(), threading.Event()
        effects, failures = [], []

        def delayed_write(document, *transaction):
            ready.set()
            if not release.wait(5):
                raise TimeoutError("test did not release the pause transition")
            return original_write(document, *transaction)

        def owner_pause():
            try:
                store.pause("owner@client.example", "incident", NOW)
            except BaseException as exc:
                failures.append(exc)

        def attempt():
            try:
                self.runtime._execute("crm.log-interaction", {"summary": "Synthetic"},
                                      lambda _: effects.append("unauthorized"), tier=0)
            except PermissionError:
                pass
            except BaseException as exc:
                failures.append(exc)
            finally:
                finished.set()

        stopping = threading.Thread(target=owner_pause, daemon=True)
        writing = threading.Thread(target=attempt, daemon=True)
        with mock.patch.object(store, "_write", side_effect=delayed_write):
            stopping.start()
            try:
                self.assertTrue(ready.wait(3))
                writing.start()
                self.assertFalse(finished.wait(0.15), "material write must wait for the pause transition")
            finally:
                release.set()
                stopping.join(5)
                if writing.ident is not None:
                    writing.join(5)
        self.assertFalse(stopping.is_alive())
        self.assertFalse(writing.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(effects, [])
        self.assertEqual(self.events()[-1]["details"]["reason"], "coworker-paused")

    def test_untrusted_pause_transaction_lock_fails_closed_before_effect(self):
        store = pause.PauseStore(self.root)
        store.resume("owner@client.example", NOW)
        lock = self.root / ".pause.lock"
        lock.rename(self.root / "detached.lock")
        lock.symlink_to(self.root / "detached.lock")
        effects = []
        with self.assertRaises(OSError):
            self.runtime._execute("crm.log-interaction", {"summary": "Synthetic"},
                                  lambda _: effects.append("unauthorized"), tier=0)
        self.assertEqual(effects, [])



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



class GatewayEgressPauseTests(unittest.TestCase):
    """A signed chat assignment and a file upload are material effects, not pause exemptions."""

    def setUp(self):
        import types
        self.types = types
        self.gateway = _load("gateway_egress_pause_tests", "platform/runtime/gateway.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.store = pause.PauseStore(self.root)
        self.assignments = []

        class TaskInbox:
            def assign_task(inner, person, agent, *, goal):
                self.assignments.append((person, agent, goal))
                return {"taskId": "task-synthetic"}

        self.team = TaskInbox()
        self.service = types.SimpleNamespace(
            data=self.root, _team_lock=threading.Lock(), _team=lambda: self.team,
        )

    def test_paused_chat_assignment_creates_no_task_and_resume_restores_it(self):
        self.store.pause("owner@client.example", "incident", NOW)
        with self.assertRaisesRegex(ValueError, "paused"):
            self.gateway.Gateway._assign_task(self.service, "staff@client.example", "sales", "synthetic goal")
        self.assertEqual(self.assignments, [])
        self.store.resume("owner@client.example", NOW + 1)
        self.assertIn("task-synthetic", self.gateway.Gateway._assign_task(
            self.service, "staff@client.example", "sales", "synthetic goal"))
        self.assertEqual(len(self.assignments), 1)

    def test_corrupt_pause_state_never_authorizes_delegated_task(self):
        self.store.resume("owner@client.example", NOW)
        self.store.path.write_text("{}", encoding="utf-8")
        self.store.path.chmod(0o600)
        self.assertTrue(self.store.is_paused())
        with self.assertRaisesRegex(ValueError, "paused"):
            self.gateway.Gateway._assign_task(self.service, "staff@client.example", "sales", "synthetic goal")
        self.assertEqual(self.assignments, [])

    def test_inflight_assignment_finishes_before_pause_is_acknowledged(self):
        entered, release, pause_done = threading.Event(), threading.Event(), threading.Event()
        outcomes = []

        class SlowTeam:
            def assign_task(inner, person, agent, *, goal):
                entered.set()
                if not release.wait(3):
                    raise AssertionError("test release was not signalled")
                return {"taskId": "task-synthetic"}

        self.service._team = lambda: SlowTeam()

        def assign():
            try:
                outcomes.append(self.gateway.Gateway._assign_task(
                    self.service, "staff@client.example", "sales", "synthetic goal"))
            except Exception as exc:
                outcomes.append(exc)

        def pause_owner():
            try:
                self.store.pause("owner@client.example", "incident", NOW)
            finally:
                pause_done.set()

        worker = threading.Thread(target=assign)
        worker.start()
        self.assertTrue(entered.wait(2))
        owner = threading.Thread(target=pause_owner)
        owner.start()
        try:
            self.assertFalse(pause_done.wait(0.1), "pause must wait for an admitted task write")
        finally:
            release.set()
            worker.join(3)
            owner.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(owner.is_alive())
        self.assertEqual(outcomes, ["Handed to sales as task task-synthetic. It is in the Agent's inbox."])
        self.assertTrue(self.store.is_paused())

    def test_paused_slack_file_upload_is_refused_without_fetching_bytes(self):
        reads, uploads, events = [], [], []
        attachment = self.types.SimpleNamespace(attachment_id="tier0:tenant/a-0123456789ab", name="safe.txt")

        class Artifacts:
            def get(inner, artifact_id, *, requested_by):
                reads.append((artifact_id, requested_by))
                return {}, b"synthetic"

        service = self.types.SimpleNamespace(
            data=self.root, _owner_lock=threading.Lock(),
            _deliverable_owner={attachment.attachment_id: "staff@client.example"},
            slack_transport=self.types.SimpleNamespace(upload_file=lambda *args: uploads.append(args)),
            _artifacts=lambda: Artifacts(), audit=lambda *args: events.append(args),
        )
        self.store.pause("owner@client.example", "incident", NOW)
        with self.assertRaisesRegex(PermissionError, "paused"):
            self.gateway.Gateway._upload_to_slack(service, "D01", None, attachment)
        self.assertEqual((reads, uploads), ([], []))
        self.store.resume("owner@client.example", NOW + 1)
        self.gateway.Gateway._upload_to_slack(service, "D01", None, attachment)
        self.assertEqual(len(reads), 1)
        self.assertEqual(len(uploads), 1)
        self.assertEqual(events[-1][0], "deliverable.uploaded")

    def test_paused_reply_does_not_stage_file_attachments(self):
        reads = []
        service = self.types.SimpleNamespace(
            data=self.root, tenant="tenant",
            _artifacts=lambda: reads.append("read") or None,
        )
        self.store.pause("owner@client.example", "incident", NOW)
        result = self.gateway.Gateway._deliverables(
            service, self.types.SimpleNamespace(channel="slack"),
            self.types.SimpleNamespace(mapped=True, identity="staff@client.example"),
            "tier0:tenant/a-0123456789ab",
        )
        self.assertEqual(result, ())
        self.assertEqual(reads, [])



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

    def test_operator_timeout_never_claims_pause_and_records_unconfirmed_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            store = pause.PauseStore(root)
            with store.effect_guard():
                result = self.run_script(root, "pause", "--by", "owner@client.example",
                                         "--wait-seconds", "0.1", "--reason", "incident")
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn("not confirmed", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertFalse(store.is_paused())
            actions = [json.loads(line)["action"] for line in (root / "audit.jsonl").read_text().splitlines()]
            self.assertIn("coworker.pause-unconfirmed", actions)
            self.assertNotIn("coworker.pause", actions)
            self.assertEqual(self.run_script(root, "pause", "--by", "owner@client.example",
                                             "--wait-seconds", "0.1").returncode, 0)

    def test_admin_chat_timeout_does_not_claim_success_or_emit_success_audit(self):
        gateway = _load("gateway_pause_timeout_test", "platform/runtime/gateway.py")
        service = mock.Mock()
        service.data = pathlib.Path("/unused")
        service.clock.return_value = NOW
        fake_store = mock.Mock()
        fake_store.pause.side_effect = TimeoutError("pause not acknowledged")
        fake_store.resume.side_effect = TimeoutError("resume not acknowledged")
        with mock.patch.object(gateway, "_module", return_value=mock.Mock(PauseStore=lambda root: fake_store)):
            for verb in ("pause", "resume"):
                answer = gateway.Gateway._pause_command(service, "owner@client.example", verb, "incident")
                self.assertIn("not confirmed", answer)
        actions = [call.args[0] for call in service.audit.call_args_list]
        self.assertEqual(actions, ["coworker.pause-unconfirmed", "coworker.resume-unconfirmed"])

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

"""Conversation memory from Hermes uses verified channel identity, not model-provided identifiers."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


compiler = load("memory_chat_compiler", "scripts/compile-workspace.py")
runtime = load("memory_chat_runtime", "platform/runtime/governed_mcp.py")
text_channel = load("memory_chat_channel", "platform/channels/text_channel.py")


def principal(identity, *, mapped=True, roles=frozenset()):
    return text_channel.Principal(
        identity=identity, channel="webchat", access="member" if mapped else "public",
        mapped=mapped, roles=frozenset(roles),
    )


class MemoryChatCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        desired = compiler.compile_workspace(ROOT / "examples/workspace", "receptionist")[0]
        desired["source"]["client"] = "acme"
        path = self.root / "desired.json"
        path.write_text(json.dumps(desired), encoding="utf-8")
        self.user = principal("alice@client.example")
        self.conversation = "thread-1"
        self.worker = runtime.GovernedToolRuntime(
            path, self.root / "audit.jsonl", self.root / "approvals",
            environ={}, principal_resolver=lambda: self.user,
            conversation_resolver=lambda: self.conversation,
        )

    def test_chat_can_remember_list_and_delete_only_own_data(self):
        first = self.worker.memory_remember(text="Prefers morning meetings")
        self.assertEqual(first["scope"], "conversation:thread-1")
        self.assertEqual([x["text"] for x in self.worker.memory_list()["memories"]], ["Prefers morning meetings"])
        self.assertEqual(self.worker.memory_remember(text="Prefers morning meetings")["duplicate"], True)
        self.user = principal("bob@client.example")
        self.assertEqual(self.worker.memory_list()["memories"], [])
        with self.assertRaisesRegex(ValueError, "owner"):
            self.worker.memory_delete(first["memoryId"])
        self.user = principal("alice@client.example")
        self.assertEqual(self.worker.memory_delete(first["memoryId"])["deleted"], first["memoryId"])
        self.assertEqual(self.worker.memory_list()["memories"], [])

    def test_identical_memory_is_not_deduplicated_across_owners(self):
        alice = self.worker.memory_remember(text="Same shared preference", scope="client")
        self.user = principal("bob@client.example")
        bob = self.worker.memory_remember(text="Same shared preference", scope="client")
        self.assertNotEqual(alice["memoryId"], bob["memoryId"])
        self.assertFalse(bob["duplicate"])
        self.assertEqual(len(self.worker.memory_list(scope="client")["memories"]), 1)
        self.user = principal("alice@client.example")
        self.assertEqual(len(self.worker.memory_list(scope="client")["memories"]), 1)
        self.worker.memory_delete(alice["memoryId"])
        self.user = principal("bob@client.example")
        self.assertEqual(len(self.worker.memory_list(scope="client")["memories"]), 1)

    def test_conversation_and_agent_scopes_are_independent_and_non_forgeable(self):
        self.worker.memory_remember(text="Private thread", scope="conversation")
        self.worker.memory_remember(text="Agent preference", scope="agent")
        self.conversation = "thread-2"
        self.assertEqual(self.worker.memory_list()["memories"], [])
        self.assertEqual([r["text"] for r in self.worker.memory_list(scope="agent")["memories"]], ["Agent preference"])
        with self.assertRaisesRegex(ValueError, "scope"):
            self.worker.memory_list(scope="conversation:thread-1")
        self.worker.conversation_resolver = None
        with self.assertRaisesRegex(PermissionError, "verified channel"):
            self.worker.memory_list(scope="conversation")

    def test_public_and_unattested_principals_never_receive_memory(self):
        self.worker.memory_remember(text="confidential business note", scope="client")
        self.user = principal("attacker@outside.example", mapped=False)
        for op in (lambda: self.worker.memory_list(scope="client"), lambda: self.worker.memory_remember(text="attack", scope="client")):
            with self.assertRaises(PermissionError):
                op()
        self.worker.principal_resolver = None
        with self.assertRaisesRegex(PermissionError, "verified chat principal"):
            self.worker.memory_list(scope="client")

    def test_admin_can_delete_another_members_entry_without_impersonation(self):
        entry = self.worker.memory_remember(text="Owner approved discount", scope="client")
        self.user = principal("admin@client.example", roles={"admin"})
        self.assertEqual(self.worker.memory_delete(entry["memoryId"])["deleted"], entry["memoryId"])

    def test_restricted_is_blocked_and_ledger_has_only_digest_not_memory_text(self):
        with self.assertRaisesRegex(ValueError, "restricted"):
            self.worker.memory_remember(text="customer medical records", scope="client", classification="restricted")
        self.worker.memory_remember(text="Close at 5 on Saturdays", scope="client")
        audit = (self.root / "audit.jsonl").read_text()
        self.assertNotIn("Close at 5", audit)
        self.assertIn("memory.write", audit)
        self.assertIn("textDigest", audit)

    def test_memory_requires_tenant_identity_and_does_not_cross_client_stores(self):
        self.worker.memory_remember(text="client A preference", scope="client")
        source = self.root / "desired.json"
        other_state = json.loads(source.read_text())
        other_state["source"]["client"] = "other"
        other_path = self.root / "other.json"
        other_path.write_text(json.dumps(other_state))
        other = runtime.GovernedToolRuntime(
            other_path, self.root / "audit.jsonl", self.root / "approvals",
            environ={}, principal_resolver=lambda: self.user, conversation_resolver=lambda: self.conversation,
        )
        self.assertEqual(other.memory_list(scope="client")["memories"], [])


if __name__ == "__main__":
    unittest.main()

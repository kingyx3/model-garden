"""Credential-free checks for the chat `assign` command: a mapped person hands a task to an Agent from a direct message (#95, #96, #98)."""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ingress = _load("ingress_for_assign_command", "platform/channels/ingress.py")
text = ingress.TEXT
DIRECTORY = {"slack": {"T01/UALICE": {"identity": "alice@acme.example"}}}


class FakeAdapter:
    def __init__(self):
        self.sent: list = []
        self.next = None

    def parse_inbound(self, body, headers):
        return self.next

    def send_reply(self, reply):
        self.sent.append(reply)
        return "ts"


class ParseTests(unittest.TestCase):
    def test_parses_agent_and_goal(self):
        for words in ("assign sales: draft the renewal quote", "/assign sales: draft the renewal quote", "Delegate  sales : draft the renewal quote",
                      "assign to sales: draft the renewal quote", "assign sales - draft the renewal quote"):
            self.assertEqual(ingress.parse_assign(words), ("sales", "draft the renewal quote"), words)

    def test_not_a_command(self):
        for words in ("assign", "assign sales:", "assign Sales Team: x", "please assign sales: x", "assignment due", "assign sales"):
            self.assertIsNone(ingress.parse_assign(words), words)


class AssignCommandTests(unittest.TestCase):
    def setUp(self):
        self.adapter = FakeAdapter()
        self.asked: list = []
        self.assigned: list = []
        self.events: list = []
        self.counter = 0
        self.ingress = ingress.ChannelIngress(
            {"slack": self.adapter}, text.PrincipalDirectory(DIRECTORY), respond=self.respond, decide=lambda *a, **k: {},
            audit=lambda action, who, details: self.events.append((action, who, details)), assign=self.assign,
        )

    def respond(self, inbound, principal):
        self.asked.append(inbound.text)
        return "the coworker answered"

    def assign(self, person, agent, goal):
        self.assigned.append((person, agent, goal))
        if agent == "ghost":
            raise ValueError("no such Agent")
        return f"Handed to {agent}."

    def say(self, words, *, user="UALICE", conversation="D0DIRECT01"):
        self.counter += 1
        self.adapter.next = text.InboundMessage("slack", conversation, None, f"m{self.counter}", text.ChannelIdentity("slack", user, "T01"), words)
        self.ingress.message("slack", b"{}", {})
        return self.adapter.sent[-1].text

    def test_mapped_person_in_direct_message_assigns_under_their_own_identity(self):
        self.assertEqual(self.say("assign sales: draft the renewal quote"), "Handed to sales.")
        self.assertEqual(self.assigned, [("alice@acme.example", "sales", "draft the renewal quote")])
        self.assertEqual(self.asked, [])
        self.assertEqual(self.events[-1][0], "channel.task-assigned")
        self.assertNotIn("renewal", str(self.events[-1]))  # the goal text stays out of the ledger

    def test_stranger_and_shared_channel_assign_nothing_and_learn_nothing(self):
        stranger = self.say("assign sales: x", user="UNOBODY")
        shared = self.say("assign sales: x", conversation="C0SHARED")
        self.assertEqual(stranger, shared)
        self.assertEqual(self.assigned, [])

    def test_a_refused_assignment_says_so_without_the_goal(self):
        reply = self.say("assign ghost: secret plan")
        self.assertIn("no such Agent", reply)
        self.assertNotIn("secret plan", reply)

    def test_ordinary_message_still_goes_to_the_coworker(self):
        self.assertEqual(self.say("what is the weather"), "the coworker answered")
        self.assertEqual(self.assigned, [])

    def test_not_wired_means_the_words_are_a_normal_question(self):
        self.ingress._assign = None
        self.assertEqual(self.say("assign sales: draft it"), "the coworker answered")


if __name__ == "__main__":
    unittest.main()

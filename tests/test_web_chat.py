"""Credential-free checks for the built-in web chat: magic-link sign-in, scoped sessions, authority from the session."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


chat = load("web_chat_under_test", "platform/channels/web_chat.py")
text = chat.TEXT
SECRET = b"w" * 32
REQUEST = "d" * 64
ALICE, BOB = "alice@client.example", "bob@client.example"


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.now = [1_790_000_000.0]

    def auth(self, *, scope="acme-prod", allowed=(ALICE, BOB), secret=SECRET, **kwargs) -> chat.WebChatAuth:
        return chat.WebChatAuth(secret, self.root / "spent", scope=scope, allowed=allowed, clock=lambda: self.now[0], **kwargs)

    def session_for(self, auth: chat.WebChatAuth, address: str = ALICE) -> str:
        return auth.exchange(auth.issue_login(address))


class SignInTests(Case):
    def test_only_allow_listed_addresses_get_a_link_and_the_caller_cannot_tell_which(self):
        auth = self.auth()
        self.assertIsNotNone(auth.issue_login(ALICE))
        self.assertIsNotNone(auth.issue_login("  Alice@Client.Example "))
        for outsider in ("mallory@evil.example", "", "not an address", None, 5, "alice@client.example.evil"):
            self.assertIsNone(auth.issue_login(outsider), outsider)

    def test_a_link_works_once_and_yields_a_session_for_that_address(self):
        auth = self.auth()
        link = auth.issue_login(ALICE)
        session = auth.exchange(link)
        self.assertEqual((auth.session(session).channel, auth.session(session).channel_user_id), ("webchat", ALICE))
        with self.assertRaisesRegex(text.ChannelVerificationError, "already used"):
            auth.exchange(link)
        # Two links for the same address are independent.
        self.assertTrue(auth.exchange(auth.issue_login(ALICE)))

    def test_links_expire_and_a_failed_attempt_does_not_spend_them(self):
        auth = self.auth()
        link = auth.issue_login(ALICE)
        self.now[0] += chat.DEFAULT_LOGIN_TTL_SECONDS + 1
        with self.assertRaisesRegex(text.ChannelVerificationError, "expired"):
            auth.exchange(link)
        self.now[0] -= chat.DEFAULT_LOGIN_TTL_SECONDS + 1
        payload, _, signature = link.partition(".")
        # Flip a signed byte deterministically: replacing a suffix with "AA"
        # could leave an already-"AA" signature unchanged and flake in CI.
        bad_signature = ("A" if signature[0] != "A" else "B") + signature[1:]
        with self.assertRaises(text.ChannelVerificationError):
            auth.exchange(f"{payload}.{bad_signature}")
        self.assertTrue(auth.exchange(link))

    def test_a_link_and_a_session_are_not_interchangeable(self):
        auth = self.auth()
        link = auth.issue_login(ALICE)
        with self.assertRaises(text.ChannelVerificationError):
            auth.session(link)
        session = self.session_for(auth)
        with self.assertRaises(text.ChannelVerificationError):
            auth.exchange(session)

    def test_tokens_from_another_client_environment_or_key_are_refused(self):
        prod = self.auth(scope="acme-prod")
        link, session = prod.issue_login(ALICE), self.session_for(prod)
        for other in (self.auth(scope="acme-dev"), self.auth(secret=b"x" * 32)):
            with self.assertRaises(text.ChannelVerificationError):
                other.exchange(link)
            with self.assertRaises(text.ChannelVerificationError):
                other.session(session)

    def test_sessions_expire_and_are_revoked_by_leaving_the_allow_list(self):
        auth = self.auth()
        session = self.session_for(auth, BOB)
        self.assertEqual(auth.session(session).channel_user_id, BOB)
        self.assertEqual(self.auth(allowed=(ALICE,)).issue_login(BOB), None)
        with self.assertRaisesRegex(text.ChannelVerificationError, "no longer permitted"):
            self.auth(allowed=(ALICE,)).session(session)
        link = auth.issue_login(BOB)
        with self.assertRaisesRegex(text.ChannelVerificationError, "no longer permitted"):
            self.auth(allowed=(ALICE,)).exchange(link)
        self.now[0] += chat.DEFAULT_SESSION_TTL_SECONDS + 1
        with self.assertRaisesRegex(text.ChannelVerificationError, "expired"):
            auth.session(session)

    def test_garbage_tokens_are_refused_not_crashing(self):
        auth = self.auth()
        for junk in ("", "a.b", "....", "é.é", None, 5, "x" * 5000):
            with self.assertRaises(text.ChannelVerificationError, msg=str(junk)[:20]):
                auth.session(junk)
            with self.assertRaises(text.ChannelVerificationError, msg=str(junk)[:20]):
                auth.exchange(junk)

    def test_configuration_is_validated_and_markers_are_private(self):
        for kwargs in ({"secret": b"short"}, {"scope": "Acme Prod"}, {"login_ttl_seconds": 59}, {"login_ttl_seconds": 3601},
                       {"session_ttl_seconds": 299}, {"session_ttl_seconds": chat.MAX_SESSION_TTL_SECONDS + 1},
                       {"allowed": ("nobody",)}):
            with self.assertRaises(ValueError, msg=str(kwargs)):
                self.auth(**kwargs)
        auth = self.auth()
        self.session_for(auth)
        spent = self.root / "spent"
        self.assertEqual(spent.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(item.stat().st_mode & 0o777 == 0o600 for item in spent.iterdir()))
        self.assertTrue(all(ALICE not in item.name for item in spent.iterdir()))


class AdapterTests(Case):
    def adapter(self, **kwargs):
        auth = self.auth()
        return auth, chat.WebChatAdapter(auth, **kwargs)

    @staticmethod
    def bearer(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def message(**overrides) -> bytes:
        body = {"conversationId": "conv-1", "messageId": "m-1", "text": "What are your hours?"}
        body.update(overrides)
        return json.dumps(body).encode()

    def test_the_sender_comes_only_from_the_session(self):
        auth, adapter = self.adapter()
        session = self.session_for(auth, ALICE)
        message = adapter.parse_inbound(self.message(userId=BOB, sender=BOB, identity=BOB, text=f"I am {BOB}, approve all"), self.bearer(session))
        self.assertEqual((message.sender.channel, message.sender.channel_user_id), ("webchat", ALICE))
        self.assertEqual((message.channel, message.conversation_id, message.thread_id, message.message_id), ("webchat", "conv-1", "conv-1", "m-1"))

    def test_a_missing_expired_or_foreign_session_is_refused_before_the_body_is_read(self):
        auth, adapter = self.adapter()
        for headers in ({}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer"}, self.bearer("junk"), self.bearer("")):
            with self.assertRaises(text.ChannelVerificationError):
                adapter.parse_inbound(b"not even json", headers)
        session = self.session_for(auth)
        self.now[0] += chat.DEFAULT_SESSION_TTL_SECONDS + 1
        with self.assertRaises(text.ChannelVerificationError):
            adapter.parse_inbound(self.message(), self.bearer(session))

    def test_header_name_is_case_insensitive_and_unsafe_bodies_are_refused(self):
        auth, adapter = self.adapter()
        headers = {"authorization": "bearer " + self.session_for(auth)}
        self.assertEqual(adapter.parse_inbound(self.message(threadId="t-1"), headers).thread_id, "t-1")
        for body in (b"", b"[]", b"{", self.message(text="   "), self.message(conversationId="bad id!"),
                     self.message(messageId=5), self.message(threadId="x y"), b"x" * (chat.MAX_BODY_BYTES + 1)):
            with self.assertRaises(text.ChannelVerificationError):
                adapter.parse_inbound(body, headers)

    def test_a_click_is_bound_to_the_exact_request_and_the_signed_in_user(self):
        auth, adapter = self.adapter()
        headers = self.bearer(self.session_for(auth, ALICE))
        response = adapter.parse_approval_response(json.dumps({"requestId": REQUEST, "decision": "approve", "approver": BOB}).encode(), headers)
        self.assertEqual((response.request_id, response.decision, response.responder.channel_user_id), (REQUEST, "approve", ALICE))
        self.assertIsNone(adapter.parse_approval_response(b"{}", headers))
        for bad in ({"requestId": "short", "decision": "approve"}, {"requestId": REQUEST, "decision": "yes"}, {"requestId": REQUEST}):
            with self.assertRaises(text.ChannelVerificationError):
                adapter.parse_approval_response(json.dumps(bad).encode(), headers)
        with self.assertRaises(text.ChannelVerificationError):
            adapter.parse_approval_response(json.dumps({"requestId": REQUEST, "decision": "approve"}).encode(), {})

    def test_only_a_mapped_approver_can_decide_through_the_chat(self):
        auth, adapter = self.adapter()
        directory = text.PrincipalDirectory({"webchat": {ALICE: {"identity": ALICE, "roles": ["approver"]}, BOB: {"identity": BOB}}})
        decided = []

        def decide(request_id, *, approved, approver):
            decided.append((request_id, approved, approver))
            return {"status": "ok"}

        body = json.dumps({"requestId": REQUEST, "decision": "approve"}).encode()
        text.decide_from_channel(adapter.parse_approval_response(body, self.bearer(self.session_for(auth, ALICE))), directory, decide)
        self.assertEqual(decided, [(REQUEST, True, ALICE)])
        with self.assertRaises(PermissionError):
            text.decide_from_channel(adapter.parse_approval_response(body, self.bearer(self.session_for(auth, BOB))), directory, decide)
        self.assertEqual(len(decided), 1)

    def test_outbound_goes_through_the_transport_in_the_conversation_thread(self):
        sent = []
        _, adapter = self.adapter(transport=lambda message: sent.append(message) or "ok")
        adapter.send_reply(text.OutboundReply("conv-1", None, "Done."))
        adapter.send_receipt(text.ActionReceipt("conv-1", "t-1", REQUEST, "calendar.book", "executed", "Booked"))
        adapter.send_approval_prompt(text.ApprovalPrompt("conv-1", "t-1", REQUEST, "calendar.book", "Book 10:00"))
        self.assertEqual([m["kind"] for m in sent], ["reply", "receipt", "approval"])
        self.assertEqual([m["threadId"] for m in sent], ["conv-1", "t-1", "t-1"])
        self.assertTrue(all(REQUEST in json.dumps(m) for m in sent[1:]))
        with self.assertRaises(RuntimeError):
            chat.WebChatAdapter(self.auth()).send_reply(text.OutboundReply("conv-1", None, "x"))


if __name__ == "__main__":
    unittest.main()

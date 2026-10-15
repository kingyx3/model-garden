"""Credential-free checks for the Gmail and Microsoft Graph mail adapters, including one contract run against both."""
from __future__ import annotations

import base64
import email
import importlib.util
import json
import pathlib
import sys
import unittest
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gmail = _load("gmail_under_test", "platform/connectors/gmail.py")
graph = _load("microsoft_mail_under_test", "platform/connectors/microsoft_mail.py")
common = gmail._common()
CATALOG = json.loads((ROOT / "contracts/v1/capabilities.json").read_text())["families"]["mail"]["tools"]
MAILBOX = "support@acme.example"
TOKEN = "ya29.super-secret-token"
LONG_BODY = "x" * 30_000
SENDERS = '"Alex Tan" <alex@customer.example>'


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


class FakeGmail:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict, dict | None]] = []
        self.drafts: dict[str, dict] = {}
        self.fail: tuple[int, str] | None = None

    def __call__(self, method, url, headers, payload):
        self.calls.append((method, url, headers, payload))
        if self.fail:
            return self.fail[0], {"error": {"message": self.fail[1]}}
        path = urllib.parse.urlparse(url).path
        if method == "GET" and path.endswith("/messages"):
            return 200, {"messages": [{"id": "m1", "threadId": "t1"}, {"id": "m2", "threadId": "t2"}]}
        if method == "GET" and "/messages/" in path:
            message_id = path.rsplit("/", 1)[1]
            headers_ = [{"name": "From", "value": SENDERS}, {"name": "Subject", "value": f"Question {message_id}"},
                        {"name": "Date", "value": "Thu, 15 Oct 2026 10:00:00 +0800"}, {"name": "To", "value": f"{MAILBOX}, other@acme.example"}]
            if "format=metadata" in url:
                return 200, {"id": message_id, "snippet": "s" * 400, "payload": {"headers": headers_}}
            html_only = message_id == "m2"
            parts = [{"mimeType": "text/html", "body": {"data": b64("<p>hello</p>")}}]
            if not html_only:
                parts = [{"mimeType": "multipart/alternative", "parts": [
                    {"mimeType": "text/plain", "body": {"data": b64("Hello, what are your opening hours? " + LONG_BODY)}}, parts[0]]}]
            return 200, {"id": message_id, "snippet": "Hello", "payload": {"headers": headers_, "mimeType": "multipart/mixed", "parts": parts}}
        if method == "POST" and path.endswith("/drafts"):
            self.drafts["d1"] = payload
            return 200, {"id": "d1", "message": {"id": "dm1"}}
        if method == "POST" and path.endswith("/drafts/send"):
            return 200, {"id": "sent1", "threadId": "t9"}
        return 404, {"error": {"message": "not found"}}


class FakeGraph:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict, dict | None]] = []
        self.is_draft = True
        self.fail: tuple[int, str] | None = None

    def __call__(self, method, url, headers, payload):
        self.calls.append((method, url, headers, payload))
        if self.fail:
            return self.fail[0], {"error": {"message": self.fail[1]}}
        path = urllib.parse.urlparse(url).path
        if method == "GET" and path.endswith("/messages"):
            return 200, {"value": [
                {"id": "m1", "subject": "Question m1", "from": {"emailAddress": {"address": "alex@customer.example"}},
                 "receivedDateTime": "2026-10-15T02:00:00Z", "bodyPreview": "s" * 400},
                {"id": "m2", "subject": "Question m2", "from": {"emailAddress": {"address": "alex@customer.example"}},
                 "receivedDateTime": "2026-10-15T02:00:00Z", "bodyPreview": "short"}]}
        if method == "GET" and "$select=id,isDraft" in url:
            return 200, {"id": path.rsplit("/", 1)[1], "isDraft": self.is_draft}
        if method == "GET" and "/messages/" in path:
            message_id = path.rsplit("/", 1)[1]
            html = message_id == "m2"
            return 200, {"id": message_id, "subject": f"Question {message_id}", "from": {"emailAddress": {"address": "alex@customer.example"}},
                         "toRecipients": [{"emailAddress": {"address": MAILBOX}}, {"emailAddress": {"address": "other@acme.example"}}],
                         "receivedDateTime": "2026-10-15T02:00:00Z", "bodyPreview": "Hello",
                         "body": {"contentType": "html" if html else "text", "content": "<p>hello</p>" if html else "Hello, what are your opening hours? " + LONG_BODY}}
        if method == "POST" and path.endswith("/messages"):
            return 201, {"id": "d1"}
        if method == "POST" and path.endswith("/send"):
            return 202, {}
        return 404, {"error": {"message": "not found"}}


def make_gmail(**kwargs):
    fake = FakeGmail()
    return gmail.GmailClient(TOKEN, mailbox=MAILBOX, transport=fake, **kwargs), fake


def make_graph(**kwargs):
    fake = FakeGraph()
    return graph.MicrosoftMailClient(TOKEN, mailbox=MAILBOX, transport=fake, **kwargs), fake


DRAFT = {"to": ["alex@customer.example"], "subject": "Opening hours", "body": "We open 9am to 5pm, Monday to Friday."}
PROVIDERS = (("gmail", make_gmail), ("outlook", make_graph))


class ContractCase(unittest.TestCase):
    """One Agent, either provider: the same arguments give the same portable result shapes."""

    def test_the_same_calls_give_the_same_shapes_with_either_provider(self):
        shapes = {}
        for name, make in PROVIDERS:
            client, _ = make()
            search = client.search({"query": "opening hours", "limit": 2})
            read = client.read({"messageId": "m1"})
            draft = client.draft(DRAFT)
            sent = client.send({"draftId": draft["draftId"]})
            shapes[name] = (
                sorted(search), sorted(search["messages"][0]), sorted(read["message"]), sorted(draft), sorted(sent) if name == "gmail" else sorted(set(sent) - {"sentFromDraft"}),
            )
            self.assertEqual(search["provider"], name)
            self.assertEqual(len(search["messages"]), 2)
            self.assertEqual(read["message"]["subject"], "Question m1")
            self.assertEqual(read["message"]["from"].endswith("alex@customer.example>") or read["message"]["from"] == "alex@customer.example", True)
            self.assertEqual(read["message"]["to"][0], MAILBOX)
            self.assertTrue(read["message"]["body"].startswith("Hello, what are your opening hours?"))
            self.assertEqual(draft["draftId"], "d1")
            self.assertTrue(sent["messageId"])
        self.assertEqual(shapes["gmail"], shapes["outlook"])

    def test_results_carry_external_object_refs_not_bodies_for_the_ledger(self):
        for name, make in PROVIDERS:
            client, _ = make()
            search = client.search({"query": "hours"})
            self.assertTrue(all(item["externalObjectRef"].startswith(f"{name}:message/") for item in search["messages"]))
            self.assertTrue(all(len(item["snippet"]) <= common.MAX_SNIPPET for item in search["messages"]))
            self.assertTrue(client.draft(DRAFT)["externalObjectRef"].startswith(f"{name}:draft/"))
            self.assertTrue(client.send({"draftId": "d1"})["externalObjectRef"].startswith(f"{name}:message/"))

    def test_the_executors_cover_exactly_the_portable_mail_tools(self):
        for _, make in PROVIDERS:
            self.assertEqual(sorted(make()[0].executors()), sorted(CATALOG))

    def test_send_is_external_communication_and_drafting_is_not(self):
        self.assertEqual(CATALOG["mail.send"]["risk"], "external-communication")
        self.assertEqual(CATALOG["mail.draft"]["risk"], "write")
        self.assertEqual(CATALOG["mail.send"]["arguments"]["required"], ["draftId"])  # sending takes a draft id, nothing else

    def test_reading_returns_text_never_markup_and_is_bounded(self):
        for name, make in PROVIDERS:
            client, _ = make()
            self.assertLessEqual(len(client.read({"messageId": "m1"})["message"]["body"]), common.MAX_READ_BODY)
            self.assertEqual(client.read({"messageId": "m2"})["message"]["body"], "", name)  # HTML-only: no raw markup handed to the model

    def test_scopes_cover_every_tool_and_stay_least_privilege(self):
        for module in (gmail, graph):
            self.assertEqual(sorted(module.SCOPES), sorted(CATALOG))
            self.assertEqual(len(module.SCOPES["mail.send"]), 1)
        flat = " ".join(scope for scopes in gmail.SCOPES.values() for scope in scopes)
        self.assertNotIn("https://mail.google.com/", flat)  # no full-mailbox scope
        self.assertTrue(all(scope != "Mail.ReadWrite.All" for scopes in graph.SCOPES.values() for scope in scopes))


class MailboxCase(unittest.TestCase):
    def test_every_request_is_for_the_configured_mailbox_only(self):
        for name, make in PROVIDERS:
            client, fake = make()
            client.search({"query": "x"})
            client.read({"messageId": "m1"})
            client.draft(DRAFT)
            client.send({"draftId": "d1"})
            quoted = urllib.parse.quote(MAILBOX, safe="") if name == "gmail" else MAILBOX
            self.assertTrue(all(f"/users/{quoted}/" in call[1] for call in fake.calls), name)
            self.assertTrue(all(call[2]["Authorization"] == f"Bearer {TOKEN}" for call in fake.calls))

    def test_a_call_cannot_name_another_mailbox_or_sender(self):
        for name, make in PROVIDERS:
            client, fake = make()
            for key in ("mailbox", "userId", "user", "account", "from", "sender"):
                for call, arguments in ((client.search, {"query": "x"}), (client.read, {"messageId": "m1"}), (client.draft, DRAFT), (client.send, {"draftId": "d1"})):
                    with self.subTest(provider=name, key=key, tool=call.__name__), self.assertRaisesRegex(ValueError, "configured mailbox"):
                        call({**arguments, key: "ceo@acme.example"})
            self.assertEqual(fake.calls, [])  # refused before any request

    def test_invalid_construction_is_refused(self):
        for kwargs in ({"mailbox": ""}, {"mailbox": "not-an-address"}, {"mailbox": "a@b"}, {"mailbox": "a b@c.example"}, {"mailbox": "x/y@c.example"}):
            for cls, key in ((gmail.GmailClient, "mailbox"), (graph.MicrosoftMailClient, "mailbox")):
                with self.subTest(kwargs=kwargs, cls=cls.__name__), self.assertRaises(ValueError):
                    cls(TOKEN, **kwargs, transport=lambda *a: (200, {}))
        for cls in (gmail.GmailClient, graph.MicrosoftMailClient):
            with self.assertRaises(ValueError):
                cls("", mailbox=MAILBOX)


class DraftFirstCase(unittest.TestCase):
    def test_a_gmail_draft_is_a_well_formed_message_from_the_mailbox_and_nothing_is_sent_by_drafting(self):
        client, fake = make_gmail()
        client.draft({**DRAFT, "to": ["alex@customer.example", "alex@customer.example", "sam@customer.example"]})
        method, url, _, payload = fake.calls[-1]
        self.assertTrue(url.endswith("/drafts"))
        message = email.message_from_bytes(base64.urlsafe_b64decode(payload["message"]["raw"] + "=="))
        self.assertEqual((message["From"], message["To"], message["Subject"]), (MAILBOX, "alex@customer.example, sam@customer.example", "Opening hours"))
        self.assertIn("We open 9am", message.get_payload(decode=True).decode())
        self.assertFalse(any("/send" in call[1] for call in fake.calls))

    def test_a_graph_draft_creates_a_draft_message_and_does_not_send(self):
        client, fake = make_graph()
        client.draft(DRAFT)
        method, url, _, payload = fake.calls[-1]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/messages"))
        self.assertEqual(payload["toRecipients"], [{"emailAddress": {"address": "alex@customer.example"}}])
        self.assertEqual(payload["body"]["contentType"], "Text")
        self.assertFalse(any(call[1].endswith("/send") for call in fake.calls))

    def test_gmail_send_posts_only_a_draft_id(self):
        client, fake = make_gmail()
        client.send({"draftId": "d1"})
        method, url, _, payload = fake.calls[-1]
        self.assertEqual((method, payload), ("POST", {"id": "d1"}))
        self.assertTrue(url.endswith("/drafts/send"))

    def test_graph_refuses_to_send_a_message_that_is_not_a_draft(self):
        client, fake = make_graph()
        fake.is_draft = False
        with self.assertRaisesRegex(ValueError, "still a draft"):
            client.send({"draftId": "m-already-sent"})
        self.assertFalse(any(call[0] == "POST" for call in fake.calls))
        fake.is_draft = True
        self.assertTrue(client.send({"draftId": "d1"})["sentFromDraft"])
        self.assertTrue(fake.calls[-1][1].endswith("/d1/send"))

    def test_invalid_drafts_and_sends_are_refused_before_any_request(self):
        for make in (make_gmail, make_graph):
            client, fake = make()
            bad_drafts = [
                {**DRAFT, "to": []}, {**DRAFT, "to": "alex@customer.example"}, {**DRAFT, "to": ["a@x.example\nBcc: boss@acme.example"]},
                {**DRAFT, "to": ["Alex <alex@customer.example>"]}, {**DRAFT, "to": [f"u{n}@x.example" for n in range(21)]},
                {**DRAFT, "subject": "Hi\r\nBcc: boss@acme.example"}, {**DRAFT, "subject": " "}, {**DRAFT, "subject": "s" * 256},
                {**DRAFT, "body": " "}, {**DRAFT, "body": "b" * 50_001}, {**DRAFT, "cc": ["x@y.example"]}, {**DRAFT, "bcc": ["x@y.example"]},
                {"to": DRAFT["to"], "subject": "no body"},
            ]
            for arguments in bad_drafts:
                with self.subTest(arguments=str(arguments)[:60]), self.assertRaises(ValueError):
                    client.draft(arguments)
            for arguments in ({}, {"draftId": ""}, {"draftId": "../x"}, {"draftId": "d1", "to": ["x@y.example"]}, {"draftId": 5}):
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    client.send(arguments)
            for arguments in ({}, {"query": " "}, {"query": "q" * 201}, {"query": "q", "limit": 0}, {"query": "q", "limit": 26}, {"query": "q", "limit": True}, {"query": "a\x00b"}):
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    client.search(arguments)
            with self.assertRaises(ValueError):
                client.read({"messageId": "../../x"})
            self.assertEqual(fake.calls, [])

    def test_control_characters_are_stripped_from_the_body(self):
        client, fake = make_graph()
        client.draft({**DRAFT, "body": "Hello\x00 there\x07"})
        self.assertEqual(fake.calls[-1][3]["body"]["content"], "Hello there")


class FailureCase(unittest.TestCase):
    def test_provider_errors_surface_without_the_token(self):
        for _, make in PROVIDERS:
            client, fake = make()
            fake.fail = (403, "Insufficient permissions")
            for call, arguments in ((client.search, {"query": "x"}), (client.draft, DRAFT), (client.send, {"draftId": "d1"})):
                with self.assertRaisesRegex(RuntimeError, r"\(403\): Insufficient permissions") as raised:
                    call(arguments)
                self.assertNotIn(TOKEN, str(raised.exception))

    def test_default_transports_only_reach_their_fixed_https_hosts(self):
        for module, good in ((gmail, "https://gmail.googleapis.com/gmail/v1/users/x/messages"), (graph, "https://graph.microsoft.com/v1.0/users/x/messages")):
            for bad in ("http://" + good[8:], good.replace("googleapis.com", "evil.example").replace("microsoft.com", "evil.example"),
                        "https://gmail.googleapis.com:8443/x" if module is gmail else "https://graph.microsoft.com:8443/x"):
                with self.subTest(module=module.__name__, url=bad), self.assertRaises(ValueError):
                    module._urllib_transport("GET", bad, {}, None)


class CommonCase(unittest.TestCase):
    def test_summary_tolerates_missing_fields_and_bounds_snippets(self):
        shaped = common.summary("m1", None, 5, None, "x" * 1000)
        self.assertEqual((shaped["from"], shaped["subject"], shaped["date"], len(shaped["snippet"])), ("", "", "", common.MAX_SNIPPET))


if __name__ == "__main__":
    unittest.main()

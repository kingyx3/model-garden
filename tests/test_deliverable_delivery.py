"""Credential-free checks that a deliverable the coworker kept is posted back to the person who asked (#101, #82, #83)."""
from __future__ import annotations

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


gateway_tests = _load("gateway_cases_for_delivery", "tests/test_channel_gateway.py")
G = gateway_tests
transports = G.transports
slack = G.slack
text = G.gateway._module("text")
ingress_module = G.gateway._module("ingress")
BOT_TOKEN = G.ENV["MODEL_GARDEN_SLACK_BOT_TOKEN"]
CONTENT = b"PK\x03\x04 a finished spreadsheet"


class UploadHttp:
    """Slack's external upload flow: a URL, the bytes, then completion. Records every call."""

    def __init__(self, *, upload_url="https://files.slack.com/upload/v1/abc", file_id="F0UPLOAD01", upload_status=200, complete_error=None):
        self.calls: list[tuple[str, bytes | None, dict]] = []
        self.upload_url, self.file_id, self.upload_status, self.complete_error = upload_url, file_id, upload_status, complete_error

    def __call__(self, url, body, headers, timeout):
        self.calls.append((url, body, dict(headers)))
        if url.endswith("files.getUploadURLExternal"):
            return 200, json.dumps({"ok": True, "upload_url": self.upload_url, "file_id": self.file_id}).encode()
        if url == self.upload_url:
            return self.upload_status, b"OK"
        if url.endswith("files.completeUploadExternal"):
            return 200, json.dumps({"ok": self.complete_error is None, **({"error": self.complete_error} if self.complete_error else {})}).encode()
        if url.endswith("chat.postMessage"):
            return 200, json.dumps({"ok": True, "ts": "1700000000.000100"}).encode()
        return 404, b"{}"

    def fields(self, method):
        body = next(body for url, body, _ in self.calls if url.endswith(method))
        return {key: value[0] for key, value in urllib.parse.parse_qs(body.decode()).items()}


class SlackUploadTests(unittest.TestCase):
    def upload(self, http, **kwargs):
        transport = transports.SlackTransport(BOT_TOKEN, http=http)
        arguments = {"channel": "D0DIRECT01", "thread": "1700000000.000200", "name": "Weekly report.xlsx", "content": CONTENT}
        arguments.update(kwargs)
        return transport.upload_file(arguments["channel"], arguments["thread"], arguments["name"], arguments["content"])

    def test_the_three_step_flow_posts_the_bytes_into_the_thread(self):
        http = UploadHttp()
        self.assertEqual(self.upload(http), "F0UPLOAD01")
        urls = [url.rsplit("/", 1)[-1] for url, _, _ in http.calls]
        self.assertEqual(urls, ["files.getUploadURLExternal", "abc", "files.completeUploadExternal"])
        self.assertEqual(http.fields("files.getUploadURLExternal"), {"filename": "Weekly report.xlsx", "length": str(len(CONTENT))})
        done = http.fields("files.completeUploadExternal")
        self.assertEqual((done["channel_id"], done["thread_ts"]), ("D0DIRECT01", "1700000000.000200"))
        self.assertEqual(json.loads(done["files"]), [{"id": "F0UPLOAD01", "title": "Weekly report.xlsx"}])
        self.assertEqual(http.calls[1][1], CONTENT)

    def test_the_bot_token_goes_only_to_slacks_api_never_to_the_upload_address(self):
        http = UploadHttp()
        self.upload(http)
        for url, _, headers in http.calls:
            if url.startswith("https://slack.com/api/"):
                self.assertEqual(headers["Authorization"], f"Bearer {BOT_TOKEN}")
                self.assertEqual(headers["Content-Type"], "application/x-www-form-urlencoded")
            else:
                self.assertNotIn("Authorization", headers)

    def test_an_upload_address_off_slacks_file_host_is_refused_before_any_bytes_are_sent(self):
        for address in ("https://evil.example/upload", "http://files.slack.com/upload", "https://files.slack.com.evil.example/x", "ftp://files.slack.com/x", None):
            http = UploadHttp(upload_url=address)
            with self.assertRaisesRegex(transports.TransportError, "file host"):
                self.upload(http)
            self.assertFalse(any(body == CONTENT for _, body, _ in http.calls), address)

    def test_bad_inputs_are_refused_without_a_network_call(self):
        for kwargs in ({"channel": "bad"}, {"channel": "C0123;DROP"}, {"thread": "not-a-ts"}, {"content": b""}, {"content": None},
                       {"content": b"x" * (transports.MAX_FILE_BYTES + 1)}):
            http = UploadHttp()
            with self.assertRaises(transports.TransportError, msg=str(list(kwargs))[:40]):
                self.upload(http, **kwargs)
            self.assertEqual(http.calls, [])

    def test_failures_carry_a_short_code_and_never_the_file(self):
        with self.assertRaisesRegex(transports.TransportError, "HTTP 500"):
            self.upload(UploadHttp(upload_status=500))
        with self.assertRaisesRegex(transports.TransportError, "missing_scope"):
            self.upload(UploadHttp(complete_error="missing_scope"))
        with self.assertRaisesRegex(transports.TransportError, "no file id"):
            self.upload(UploadHttp(file_id="not-a-file-id"))

    def test_a_hostile_name_cannot_inject_path_or_control_characters(self):
        http = UploadHttp()
        self.upload(http, name="../../etc/passwd\x00\n.xlsx")
        label = http.fields("files.getUploadURLExternal")["filename"]
        self.assertNotIn("/", label)
        self.assertNotIn("\x00", label)
        self.assertNotIn("\n", label)


class AdapterTests(unittest.TestCase):
    def adapter(self, upload=None):
        self.posted = []
        return slack.SlackChannelAdapter(G.SIGNING, bot_user_id=G.BOT, transport=lambda message: self.posted.append(message) or "1700000000.000100", upload=upload)

    def reply(self, count=1):
        attachments = tuple(text.Attachment(attachment_id=f"tier0:acme/a-00000000000{i}", name=f"Report {i}.xlsx",
                                            media_type="application/vnd.ms-excel", size_bytes=10, digest="d" * 64) for i in range(count))
        return text.OutboundReply(conversation_id="D0DIRECT01", thread_id="1700000000.000200", text="Your report is done.", attachments=attachments)

    def test_without_an_uploader_the_reply_names_the_file(self):
        self.adapter().send_reply(self.reply())
        self.assertIn("File ready: Report 0.xlsx", self.posted[0]["text"])

    def test_with_an_uploader_the_file_is_posted_and_not_described_twice(self):
        sent = []
        adapter = self.adapter(upload=lambda conversation, thread, item: sent.append((conversation, thread, item.name)))
        adapter.send_reply(self.reply())
        self.assertEqual(sent, [("D0DIRECT01", "1700000000.000200", "Report 0.xlsx")])
        self.assertEqual(self.posted[0]["text"], "Your report is done.")

    def test_a_failed_upload_falls_back_to_the_reference_line_for_that_file_only(self):
        def upload(conversation, thread, item):
            if item.name == "Report 1.xlsx":
                raise RuntimeError("boom")
        adapter = self.adapter(upload=upload)
        adapter.send_reply(self.reply(2))
        self.assertNotIn("Report 0.xlsx", self.posted[0]["text"])
        self.assertIn("Report 1.xlsx", self.posted[0]["text"])
        self.assertNotIn("boom", self.posted[0]["text"])

    def test_at_most_three_files_are_uploaded_and_the_rest_are_named(self):
        sent = []
        self.adapter(upload=lambda c, t, item: sent.append(item.name)).send_reply(self.reply(5))
        self.assertEqual(len(sent), 3)
        self.assertIn("Report 3.xlsx", self.posted[0]["text"])
        self.assertIn("Report 4.xlsx", self.posted[0]["text"])


class IngressTests(unittest.TestCase):
    def run_ingress(self, deliverables):
        sent, notes = [], []

        class Adapter:
            channel = "slack"

            def send_reply(self, reply):
                sent.append(reply)

        inbound = text.InboundMessage(channel="slack", conversation_id="D1", thread_id="1", message_id="m1",
                                      sender=text.ChannelIdentity("slack", "U1", workspace_id="T01"), text="make me a report")
        ingress = ingress_module.ChannelIngress(
            {"slack": Adapter()}, text.PrincipalDirectory(), respond=lambda inbound, principal: "Done", decide=lambda *a, **k: {},
            audit=lambda action, by, details: notes.append((action, details)), deliverables=deliverables,
        )
        adapter = Adapter()
        ingress._adapters = {"slack": adapter}
        outcome = ingress._process(adapter, inbound)
        return outcome, sent, notes

    def test_the_policys_attachments_ride_on_the_reply_up_to_three(self):
        item = text.Attachment(attachment_id="tier0:acme/a-000000000001", name="r.xlsx", media_type="x", size_bytes=1)
        outcome, sent, _ = self.run_ingress(lambda inbound, principal, answer: [item] * 5)
        self.assertEqual((outcome, len(sent[0].attachments)), ("answered", 3))

    def test_a_failing_policy_never_stops_the_answer(self):
        def boom(inbound, principal, answer):
            raise RuntimeError("customer text must not leak")
        outcome, sent, notes = self.run_ingress(boom)
        self.assertEqual((outcome, sent[0].text, sent[0].attachments), ("answered", "Done", ()))
        self.assertEqual(notes, [("channel.deliverables-failed", {"channel": "slack", "error": "RuntimeError"})])

    def test_no_policy_means_no_attachments(self):
        outcome, sent, _ = self.run_ingress(None)
        self.assertEqual((outcome, sent[0].attachments), ("answered", ()))


class GatewayDeliveryTests(G.GatewayCase):
    config = {**G.GatewayCase.config, "slack": {"botUserId": G.BOT, "uploadFiles": True}}

    def setUp(self):
        super().setUp()
        self.upload_http = UploadHttp()
        self.gateway.slack_transport._http = self.upload_http
        self.http = self.upload_http  # GatewayCase.posts() reads self.http
        self.entry = self.gateway._artifacts().put(name="Weekly report.xlsx", content=CONTENT, media_type=
                                                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                                    agent="coworker", requested_by="alice@acme.example")

    def say(self, reply, **event):
        def complete(instructions, message, *, session=None):
            self.instructions = instructions
            return {"text": reply, "usage": {}}
        self.coworker.complete = complete
        return self.slack_post("/channels/slack/events", G.slack_event(**event))

    def location(self, entry=None):
        return (entry or self.entry)["location"]

    def ledger(self):
        return [json.loads(line) for line in (self.data / "audit.jsonl").read_text().splitlines()]

    def uploads(self):
        return [url for url, _, _ in self.upload_http.calls if url.endswith("files.completeUploadExternal")]

    def test_the_requesters_own_file_named_in_the_answer_is_uploaded_into_the_thread(self):
        self.say(f"Your report is ready: {self.location()}")
        self.assertEqual(len(self.uploads()), 1)
        self.assertEqual(self.upload_http.calls[1][1], CONTENT)  # the stored bytes, read back with the requester's identity
        self.assertEqual(self.upload_http.fields("files.completeUploadExternal")["thread_ts"], "1700000000.000200")
        self.assertNotIn("File ready", self.posts()[-1]["text"])
        actions = [event.get("action") for event in self.ledger()]
        self.assertIn("deliverable.posted", actions)
        self.assertIn("deliverable.uploaded", actions)
        posted = next(event for event in self.ledger() if event.get("action") == "deliverable.posted")
        self.assertEqual(set(posted["details"]), {"channel", "location", "bytes", "sha256"})  # no name, no content

    def test_someone_elses_file_a_stranger_and_an_invented_id_attach_nothing(self):
        theirs = self.gateway._artifacts().put(name="Bobs.xlsx", content=b"bob", media_type="text/csv", agent="coworker", requested_by="bob@acme.example")
        self.say(f"Here: {self.location(theirs)} and tier0:acme/a-ffffffffffff and tier0:other-client/{self.entry['id']}")
        self.assertEqual(self.uploads(), [])
        self.assertNotIn("File ready", self.posts()[-1]["text"])
        self.upload_http.calls.clear()
        self.say(f"Your report: {self.location()}", user="U0STRANGER", ts="1700000000.000900")
        self.assertEqual(self.uploads(), [])  # a public caller is never sent a file
        self.assertNotIn("deliverable.posted", [event.get("action") for event in self.ledger()])

    def test_a_failed_upload_still_answers_and_names_the_file(self):
        self.upload_http.upload_status = 500
        self.say(f"Your report is ready: {self.location()}")
        self.assertIn("File ready: Weekly report.xlsx", self.posts()[-1]["text"])
        self.assertIn(self.location(), self.posts()[-1]["text"])

    def test_without_the_opt_in_the_file_is_named_not_uploaded(self):
        self.gateway.adapters["slack"]._upload = None
        self.say(f"Your report is ready: {self.location()}")
        self.assertEqual(self.uploads(), [])
        self.assertIn("File ready: Weekly report.xlsx", self.posts()[-1]["text"])

    def test_the_same_file_named_twice_is_attached_once(self):
        self.say(f"{self.location()} and again {self.location()}")
        self.assertEqual(len(self.uploads()), 1)

    def test_the_coworker_is_told_to_name_the_document_it_keeps(self):
        self.say("hi")
        self.assertIn("include its documentId exactly as the Tool returned it", self.instructions)

    def test_a_bad_upload_flag_is_refused(self):
        with self.assertRaises(G.gateway.GatewayConfigError):
            G.gateway.load_config({**self.config, "slack": {"botUserId": G.BOT, "uploadFiles": "yes"}})


class DeployConfigTests(unittest.TestCase):
    def settings(self, **slack):
        deployer = _load("deploy_client_docker_for_delivery", "scripts/deploy-client-docker.py")
        binding = {"spec": {"gateway": {"enabled": True, "public_origin": "https://chat.acme.example", "agent": "receptionist",
                                         "slack": {"bot_user_id": "U0BOT0001", **slack},
                                         "secrets": {"slack_signing_secret_ref": "secret://acme/slack-signing",
                                                     "slack_bot_token_ref": "secret://acme/slack-bot"}}}}
        return deployer._gateway_settings(binding)[0]

    def test_the_opt_in_reaches_the_gateway_only_when_set(self):
        self.assertEqual(self.settings(upload_files=True)["slack"]["uploadFiles"], True)
        self.assertNotIn("uploadFiles", self.settings()["slack"])
        self.assertNotIn("uploadFiles", self.settings(upload_files="yes")["slack"])  # only a real true turns file upload on


if __name__ == "__main__":
    unittest.main()

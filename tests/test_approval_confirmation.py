"""Credential-free checks for the page behind an emailed approval link (#84): preview on GET, decide only on POST."""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile
import unittest
from urllib.parse import urlencode

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


email = _load("email_for_confirmation", "platform/channels/email_channel.py")
page = _load("approval_confirmation_under_test", "platform/channels/approval_confirmation.py")
REQUEST = "ab" * 32
APPROVER = "owner@client.example"
SECRET = b"s" * 32
ORIGIN = "https://approvals.client.example"


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clock = Clock()
        self.links = email.ApprovalLinks(SECRET, pathlib.Path(self._tmp.name) / "spent", clock=self.clock)
        self.decisions: list = []
        self.events: list = []
        self.failing = False

    def handler(self, **kwargs):
        def decide(response):
            if self.failing:
                raise RuntimeError("store unavailable: secret-detail")
            self.decisions.append(response)
            return "ok"

        kwargs.setdefault("audit", lambda *event: self.events.append(event))
        return page.ApprovalConfirmation(self.links, decide, **kwargs)

    def token(self, decision="approve"):
        return self.links.issue(REQUEST, APPROVER, decision)

    @staticmethod
    def form(token, **extra):
        return urlencode({"token": token, **extra}).encode()


class PreviewTests(Case):
    def test_get_shows_the_decision_and_never_spends_the_link(self):
        token, handler = self.token(), self.handler(summary_for=lambda rid: "Book a haircut for Sam on Friday")
        for _ in range(3):
            response = handler.handle("GET", token=token)
            self.assertEqual(response.status, 200)
        self.assertIn("Approve this request?", response.body)
        self.assertIn(APPROVER, response.body)
        self.assertIn(REQUEST[:12], response.body)
        self.assertIn("Book a haircut for Sam on Friday", response.body)
        self.assertIn("Nothing happens until you press the button", response.body)
        self.assertEqual(self.decisions, [])
        self.assertEqual(handler.handle("POST", body=self.form(token)).status, 200)  # still spendable after any number of GETs

    def test_the_page_is_hardened(self):
        headers = self.handler().handle("GET", token=self.token()).headers
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("<script", self.handler().handle("GET", token=self.token()).body.lower())

    def test_a_reject_token_offers_reject(self):
        response = self.handler().handle("GET", token=self.token("reject"))
        self.assertIn("Reject this request?", response.body)
        self.assertIn("Reject this request</button>", response.body)

    def test_bad_expired_and_used_links_are_refused_without_detail(self):
        handler = self.handler()
        for token in (None, "", "x" * 3000, "garbage", "a.b"):
            self.assertEqual(handler.handle("GET", token=token).status, 400, token)
        token = self.token()
        self.clock.now += 8 * 86400
        expired = handler.handle("GET", token=token)
        self.assertEqual(expired.status, 410)
        self.assertIn("expired", expired.body)
        self.clock.now = 1_800_000_000.0
        fresh = self.token()
        handler.handle("POST", body=self.form(fresh))
        used = handler.handle("GET", token=fresh)
        self.assertEqual(used.status, 410)
        self.assertIn("already used", used.body)

    def test_the_summary_is_escaped_bounded_and_failure_tolerant(self):
        response = self.handler(summary_for=lambda rid: "<script>alert(1)</script>" + "x" * 2000).handle("GET", token=self.token())
        self.assertNotIn("<script>", response.body)
        self.assertIn("&lt;script&gt;", response.body)
        self.assertLess(len(response.body), 3000)

        def broken(_):
            raise RuntimeError("lookup failed")

        self.assertEqual(self.handler(summary_for=broken).handle("GET", token=self.token()).status, 200)

    def test_the_token_is_escaped_in_the_form(self):
        response = self.handler().handle("GET", token=self.token())
        self.assertIn('<input type="hidden" name="token" value="', response.body)
        self.assertNotIn('"><', response.body.split('name="token" value="')[1].split('"')[0])


class ConfirmTests(Case):
    def test_post_spends_the_link_and_hands_one_decision_to_the_governed_path(self):
        token = self.token()
        response = self.handler().handle("POST", body=self.form(token))
        self.assertEqual(response.status, 200)
        self.assertIn("Decision recorded", response.body)
        (decision,) = self.decisions
        self.assertEqual((decision.request_id, decision.decision, decision.responder.channel_user_id, decision.responder.channel), (REQUEST, "approve", APPROVER, "email"))

    def test_a_second_post_cannot_decide_again(self):
        token, handler = self.token(), self.handler()
        handler.handle("POST", body=self.form(token))
        again = handler.handle("POST", body=self.form(token))
        self.assertEqual(again.status, 410)
        self.assertEqual(len(self.decisions), 1)

    def test_approve_and_reject_links_for_the_same_request_spend_each_other(self):
        handler = self.handler()
        handler.handle("POST", body=self.form(self.token("approve")))
        self.assertEqual(handler.handle("POST", body=self.form(self.token("reject"))).status, 410)
        self.assertEqual([item.decision for item in self.decisions], ["approve"])

    def test_form_fields_cannot_change_the_decision_request_or_approver(self):
        token = self.token("reject")
        self.handler().handle("POST", body=self.form(token, decision="approve", request="f" * 64, approver="evil@x.example"))
        (decision,) = self.decisions
        self.assertEqual((decision.decision, decision.request_id, decision.responder.channel_user_id), ("reject", REQUEST, APPROVER))

    def test_malformed_posts_decide_nothing(self):
        handler = self.handler()
        for body in (None, b"", b"x" * 5000, b"nothing", b"token=", b"token=a&token=b", b"\xff\xfe", b"a=1&b=2&c=3&d=4&e=5&token=x"):
            self.assertIn(handler.handle("POST", body=body).status, (400, 410), body)
        self.assertEqual(self.decisions, [])

    def test_a_cross_origin_post_is_refused_and_does_not_spend_the_link(self):
        token, handler = self.token(), self.handler(allowed_origin=ORIGIN)
        refused = handler.handle("POST", body=self.form(token), headers={"Origin": "https://evil.example"})
        self.assertEqual(refused.status, 403)
        self.assertEqual(self.decisions, [])
        self.assertEqual(handler.handle("POST", body=self.form(token), headers={"origin": ORIGIN}).status, 200)

    def test_a_missing_origin_is_allowed_because_the_token_is_the_proof(self):
        self.assertEqual(self.handler(allowed_origin=ORIGIN).handle("POST", body=self.form(self.token())).status, 200)

    def test_a_failed_decision_is_reported_truthfully_and_the_link_stays_spent(self):
        token, handler = self.token(), self.handler()
        self.failing = True
        response = handler.handle("POST", body=self.form(token))
        self.assertEqual(response.status, 500)
        self.assertIn("not recorded", response.body)
        self.assertNotIn("Decision recorded", response.body)
        self.assertNotIn("secret-detail", response.body)
        self.failing = False
        self.assertEqual(handler.handle("POST", body=self.form(token)).status, 410)
        self.assertEqual(self.decisions, [])
        self.assertIn("RuntimeError", [event[2]["detail"] for event in self.events])
        self.assertNotIn("secret-detail", str(self.events))

    def test_other_methods_are_refused(self):
        for method in ("PUT", "DELETE", "HEAD", "", None):
            response = self.handler().handle(method, token=self.token(), body=self.form(self.token()))
            self.assertEqual(response.status, 405, method)
        self.assertEqual(self.decisions, [])


class SetupTests(Case):
    def test_setup_is_checked(self):
        for kwargs in ({"action_path": "no-slash"}, {"action_path": "/a?b=1"}, {"allowed_origin": "http://x.example"}, {"allowed_origin": "https://x.example/path"}):
            with self.assertRaises(ValueError):
                self.handler(**kwargs)
        with self.assertRaises(ValueError):
            page.ApprovalConfirmation(object(), lambda r: None)
        with self.assertRaises(ValueError):
            page.ApprovalConfirmation(self.links, "nope")

    def test_peek_does_not_spend_and_refuses_used_links(self):
        token = self.token()
        self.assertEqual(self.links.peek(token), (REQUEST, APPROVER, "approve"))
        self.assertEqual(self.links.peek(token), (REQUEST, APPROVER, "approve"))
        self.links.consume(token)
        with self.assertRaises(email.ChannelVerificationError):
            self.links.peek(token)


if __name__ == "__main__":
    unittest.main()

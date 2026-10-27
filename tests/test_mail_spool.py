"""Credential-free checks for the built-in inbound mailbox spool that feeds the support mailbox (#77, #84, #109)."""
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

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


spool_module = _load("mail_spool_under_test", "platform/channels/mail_spool.py")
support = _load("support_for_spool", "platform/channels/support_mailbox.py")
runtime = _load("runtime_mailbox_under_test", "scripts/runtime-mailbox.py")
email_channel = support._email()
text = email_channel.TEXT
T0 = 1_800_000_000
MX = "mx.acme.example"
MAILBOX = "support@acme.example"
SECRET_SUBJECT = "Boiler at 12 Acacia Road"
SECRET_BODY = "My card 4111 1111 1111 1111 was charged twice."


def raw(*, message_id="<c1@customer.example>", to=MAILBOX, sender="Alex Tan <alex@customer.example>", extra="", auth=True,
        subject=SECRET_SUBJECT, body=SECRET_BODY) -> bytes:
    header = f"Authentication-Results: {MX}; spf=pass; dkim=pass; dmarc=pass header.from=customer.example\r\n" if auth else ""
    return (f"{header}From: {sender}\r\n" + (f"To: {to}\r\n" if to else "") + f"Subject: {subject}\r\nMessage-ID: {message_id}\r\n{extra}"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n").encode()


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.events: list[tuple] = []

    def spool(self, tenant="acme-prod", addresses=(MAILBOX,), **kwargs):
        return spool_module.MailSpool(self.root / "mail", tenant=tenant, addresses=addresses,
                                      audit=lambda *event: self.events.append(event), **kwargs)

    def only(self, spool):
        return next(iter(json.loads(spool.index.read_text())["messages"].values()))


class IntakeTests(Case):
    def test_a_message_for_this_client_is_stored_privately_by_digest(self):
        spool = self.spool()
        result = spool.ingest(raw(), now=T0)
        self.assertEqual(result["status"], "accepted")
        self.assertRegex(result["id"], r"^m-[0-9a-f]{24}$")
        path = spool.messages / f"{result['id']}.eml"
        self.assertEqual(path.read_bytes(), raw())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(spool.messages.stat().st_mode), 0o700)
        self.assertEqual(spool.stats()["new"], 1)

    def test_the_index_and_ledger_never_hold_addresses_subjects_or_text(self):
        spool = self.spool()
        spool.ingest(raw(), now=T0)
        blob = spool.index.read_text() + json.dumps(self.events)
        for private in ("alex@customer", "Alex Tan", "Acacia", "4111", MAILBOX, "customer.example"):
            self.assertNotIn(private, blob)

    def test_redelivery_is_idempotent_on_the_message_id(self):
        spool = self.spool()
        first = spool.ingest(raw(), now=T0)
        again = spool.ingest(raw(body="different text, same id"), now=T0 + 60)
        self.assertEqual((again["status"], again["id"]), ("duplicate", first["id"]))
        self.assertEqual(spool.stats()["total"], 1)
        self.assertEqual(len(list(spool.messages.glob("*.eml"))), 1)

    def test_recipient_matching_is_exact_and_covers_the_delivery_headers(self):
        spool = self.spool(addresses=(MAILBOX, "help@acme.example"))
        cases = {
            "to-display-name": raw(message_id="<a@x>", to=f"Support Team <{MAILBOX.upper()}>"),
            "cc": raw(message_id="<b@x>", to="someone@else.example", extra=f"Cc: {MAILBOX}\r\n"),
            "delivered-to": raw(message_id="<c@x>", to="list@else.example", extra="Delivered-To: help@acme.example\r\n"),
            "x-original-to": raw(message_id="<d@x>", to="", extra=f"X-Original-To: {MAILBOX}\r\n"),
        }
        for name, message in cases.items():
            self.assertEqual(spool.ingest(message, now=T0)["status"], "accepted", name)

    def test_mail_for_anyone_else_is_dropped_not_stored(self):
        spool = self.spool()
        for to in ("support@other.example", "support@acme.example.evil.example", "xsupport@acme.example", "support@acme.example.evil"):
            result = spool.ingest(raw(message_id=f"<m{abs(hash(to))}@x>", to=to), now=T0)
            self.assertEqual((result["status"], result["reason"]), ("misdelivered", "not-addressed-to-this-client"), to)
        # an address named only in the body or a forged non-recipient header is not a recipient
        body_only = raw(message_id="<body@x>", to="someone@else.example", body=f"please write to {MAILBOX}", extra=f"Reply-To: {MAILBOX}\r\n")
        self.assertEqual(spool.ingest(body_only, now=T0)["status"], "misdelivered")
        self.assertEqual(spool.stats()["total"], 0)
        self.assertTrue(all(event[0] == "mail-spool.misdelivered" for event in self.events))
        self.assertNotIn("other.example", json.dumps(self.events))

    def test_unusable_messages_are_rejected_without_being_stored(self):
        spool = self.spool()
        self.assertEqual(spool.ingest(b"", now=T0)["reason"], "empty")
        self.assertEqual(spool.ingest(b"x" * (spool_module.MAX_MESSAGE_BYTES + 1), now=T0)["reason"], "too-large")
        no_id = raw().replace(b"Message-ID: <c1@customer.example>\r\n", b"")
        self.assertEqual(spool.ingest(no_id, now=T0)["reason"], "no-single-message-id")
        self.assertEqual(spool.ingest(raw(message_id="<a@x> <b@x>"), now=T0)["reason"], "no-single-message-id")
        twice = raw(message_id="<a@x>", extra="Message-ID: <b@x>\r\n")
        self.assertEqual(spool.ingest(twice, now=T0)["reason"], "no-single-message-id")
        self.assertEqual(spool.stats()["total"], 0)

    def test_a_full_spool_defers_instead_of_dropping(self):
        spool = self.spool()
        original = spool_module.MAX_MESSAGES
        spool_module.MAX_MESSAGES = 2
        self.addCleanup(setattr, spool_module, "MAX_MESSAGES", original)
        spool.ingest(raw(message_id="<1@x>"), now=T0)
        spool.ingest(raw(message_id="<2@x>"), now=T0)
        with self.assertRaises(spool_module.SpoolFull):
            spool.ingest(raw(message_id="<3@x>"), now=T0)
        self.assertEqual(spool.ingest(raw(message_id="<2@x>"), now=T0)["status"], "duplicate")  # a retry of stored mail is still fine

    def test_setup_is_validated(self):
        for kwargs in ({"tenant": "Bad Tenant"}, {"addresses": ()}, {"addresses": ("not-an-address",)}, {"addresses": ("a@b.example",) * 0},
                       {"retention_days": 0}, {"retention_days": 400}, {"lease_seconds": 5}, {"max_attempts": 0}):
            with self.assertRaises(spool_module.SpoolError, msg=str(kwargs)):
                self.spool(**kwargs)

    def test_two_clients_never_share_a_spool_or_an_index(self):
        a, b = self.spool("acme-prod"), self.spool("other-prod", addresses=("help@other.example",))
        a.ingest(raw(), now=T0)
        self.assertEqual(b.stats()["total"], 0)
        b.directory.mkdir(parents=True, exist_ok=True)
        (b.directory / "index.json").write_text(a.index.read_text())  # another client's index dropped into this one
        with self.assertRaises(spool_module.SpoolError):
            b.stats()



class SpoolTrustBoundaryTests(Case):
    """The receiving mail process must not follow local links or overwrite another tenant's files."""

    def test_symlinked_tenant_directory_is_refused_without_chmod_of_target(self):
        import os
        outside = self.root / "outside"
        outside.mkdir(mode=0o755)
        mail_root = self.root / "mail"
        mail_root.mkdir(mode=0o700)
        (mail_root / "acme-prod").symlink_to(outside, target_is_directory=True)
        before = stat.S_IMODE(outside.stat().st_mode)
        with self.assertRaises(spool_module.SpoolError):
            self.spool().stats()
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), before)

    def test_lock_and_index_links_are_refused_without_reading_or_writing_target(self):
        import os
        spool = self.spool()
        spool.ingest(raw(), now=T0)
        lock = spool.directory / ".lock"
        outside = self.root / "outside"
        outside.write_text("unchanged")
        lock.unlink()
        lock.symlink_to(outside)
        with self.assertRaises(spool_module.SpoolError):
            spool.stats()
        self.assertEqual(outside.read_text(), "unchanged")
        lock.unlink()
        lock.write_text("")
        os.chmod(lock, 0o600)
        spool.index.unlink()
        os.link(outside, spool.index)
        with self.assertRaises(spool_module.SpoolError):
            spool.stats()
        self.assertEqual(outside.read_text(), "unchanged")

    def test_predictable_index_temp_hardlink_never_truncates_other_file(self):
        import os
        spool = self.spool()
        outside = self.root / "outside"
        outside.write_text("keep this exact content")
        spool.stats()
        os.link(outside, spool.index.with_suffix(".tmp"))
        self.assertEqual(spool.ingest(raw(), now=T0)["status"], "accepted")
        self.assertEqual(outside.read_text(), "keep this exact content")
        self.assertEqual(spool.stats()["total"], 1)

    def test_linked_or_exposed_message_file_cannot_be_claimed(self):
        import os
        spool = self.spool()
        message_id = spool.ingest(raw(), now=T0)["id"]
        path = spool.messages / f"{message_id}.eml"
        outside = self.root / "outside.eml"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises(spool_module.SpoolError):
            spool.claim(now=T0)
        path.unlink()
        os.link(outside, path)
        with self.assertRaises(spool_module.SpoolError):
            spool.claim(now=T0)
        path.unlink()
        path.write_bytes(outside.read_bytes())
        os.chmod(path, 0o644)
        with self.assertRaises(spool_module.SpoolError):
            spool.claim(now=T0)


class ProcessingTests(Case):
    def test_claim_leases_oldest_first_and_never_hands_out_a_leased_message_twice(self):
        spool = self.spool()
        ids = [spool.ingest(raw(message_id=f"<{n}@x>"), now=T0 + n)["id"] for n in range(3)]
        first = spool.claim(limit=2, now=T0 + 10)
        self.assertEqual([item["id"] for item in first], ids[:2])
        self.assertEqual([item["attempt"] for item in first], [1, 1])
        self.assertEqual([item["id"] for item in spool.claim(limit=5, now=T0 + 11)], ids[2:])
        self.assertEqual(spool.claim(limit=5, now=T0 + 12), [])
        for bad in (0, 101, True, "2"):
            with self.assertRaises(spool_module.SpoolError):
                spool.claim(limit=bad)

    def test_an_expired_lease_makes_the_message_available_again(self):
        spool = self.spool(lease_seconds=60)
        spool.ingest(raw(), now=T0)
        (item,) = spool.claim(now=T0)
        self.assertEqual(spool.claim(now=T0 + 30), [])
        (again,) = spool.claim(now=T0 + 61)
        self.assertEqual((again["id"], again["attempt"]), (item["id"], 2))

    def test_a_message_that_keeps_killing_its_handler_is_quarantined_not_retried_for_ever(self):
        spool = self.spool(lease_seconds=60, max_attempts=2)
        spool.ingest(raw(), now=T0)
        now = T0
        for _ in range(2):
            self.assertEqual(len(spool.claim(now=now)), 1)
            now += 61  # the handler crashed: it never reported back
        self.assertEqual(spool.claim(now=now), [])
        entry = self.only(spool)
        self.assertEqual((entry["state"], entry["lastError"]), ("dead", "LeaseExpired"))

    def test_complete_only_works_on_a_leased_message(self):
        spool = self.spool()
        message_id = spool.ingest(raw(), now=T0)["id"]
        with self.assertRaises(spool_module.SpoolError):
            spool.complete(message_id)  # never claimed
        spool.claim(now=T0)
        self.assertEqual(spool.complete(message_id, outcome="drafted", now=T0 + 1)["state"], "done")
        with self.assertRaises(spool_module.SpoolError):
            spool.complete(message_id)
        self.assertEqual(spool.claim(now=T0 + 10_000), [])  # done is never claimed again
        self.assertEqual(self.only(spool)["outcome"], "drafted")

    def test_a_failure_backs_off_then_quarantines_and_a_person_can_requeue(self):
        spool = self.spool()
        message_id = spool.ingest(raw(), now=T0)["id"]
        now = T0
        states = []
        for _ in range(3):
            (item,) = spool.claim(now=now)
            states.append(spool.fail(item["id"], error_type="ConnectionError", now=now)["state"])
            now += 10_000
        self.assertEqual(states, ["new", "new", "dead"])
        self.assertEqual(spool.stats()["deadIds"], [message_id])
        self.assertEqual(spool.claim(now=now), [])
        with self.assertRaises(spool_module.SpoolError):
            spool.requeue("m-" + "0" * 24, by="jordan@client.example")
        spool.requeue(message_id, by="jordan@client.example", now=now)
        self.assertEqual(spool.claim(now=now)[0]["attempt"], 1)
        self.assertIn(("mail-spool.requeued", "jordan@client.example", {"id": message_id}), self.events)

    def test_the_retry_waits_for_the_backoff(self):
        spool = self.spool()
        spool.ingest(raw(), now=T0)
        (item,) = spool.claim(now=T0)
        spool.fail(item["id"], error_type="OSError", now=T0)
        self.assertEqual(spool.claim(now=T0 + 30), [])
        self.assertEqual(len(spool.claim(now=T0 + 61)), 1)

    def test_only_the_error_type_is_recorded(self):
        spool = self.spool()
        spool.ingest(raw(), now=T0)
        (item,) = spool.claim(now=T0)
        spool.fail(item["id"], error_type="provider said: card 4111 1111 1111 1111", now=T0)
        self.assertEqual(self.only(spool)["lastError"], "Error")
        self.assertNotIn("4111", json.dumps(self.events) + spool.index.read_text())

    def test_a_changed_or_missing_file_is_quarantined_not_handed_out(self):
        spool = self.spool()
        first = spool.ingest(raw(message_id="<1@x>"), now=T0)["id"]
        second = spool.ingest(raw(message_id="<2@x>"), now=T0 + 1)["id"]
        (spool.messages / f"{first}.eml").write_bytes(b"tampered")
        (spool.messages / f"{second}.eml").unlink()
        self.assertEqual(spool.claim(now=T0 + 5), [])
        errors = {entry["id"]: entry["lastError"] for entry in json.loads(spool.index.read_text())["messages"].values()}
        self.assertEqual(errors, {first: "DigestMismatch", second: "MissingFile"})

    def test_retention_deletes_files_and_records_in_every_state(self):
        spool = self.spool(retention_days=1)
        old = spool.ingest(raw(message_id="<old@x>"), now=T0)["id"]
        new = spool.ingest(raw(message_id="<new@x>"), now=T0 + 80_000)["id"]
        self.assertEqual(spool.purge(now=T0 + 86_400), {"removed": 1})
        self.assertFalse((spool.messages / f"{old}.eml").exists())
        self.assertTrue((spool.messages / f"{new}.eml").exists())
        self.assertEqual(spool.stats()["total"], 1)
        self.assertEqual(spool.purge(now=T0 + 86_400), {"removed": 0})


class SupportMailboxTests(Case):
    def mailbox(self, drafts, **overrides):
        adapter = email_channel.EmailChannelAdapter(MX, own_addresses=(MAILBOX,), reply_limiter=email_channel.ReplyLimiter(self.root / "limits", 3))

        def draft(arguments):
            drafts.append(arguments)
            return {"draftId": f"d{len(drafts)}", "externalObjectRef": f"mail:draft/{len(drafts)}"}

        options = dict(classify=lambda message: "enquiry",
                       answer=lambda message, category: {"text": "We are open 9 to 6.", "sources": ["kb:opening-hours@v3"]},
                       handled=support.MessageLog(self.root / "handled"))
        options.update(overrides)
        return support.SupportMailbox(adapter, text.PrincipalDirectory(), {"mail.draft": draft}, **options)

    def test_drain_feeds_the_support_mailbox_once_per_message_and_redelivery_drafts_nothing_new(self):
        drafts: list = []
        spool, mailbox = self.spool(), self.mailbox(drafts)
        spool.ingest(raw(), now=T0)
        spool.ingest(raw(), now=T0 + 5)  # the MTA delivered it twice
        counts = spool.drain(lambda message: mailbox.handle(message, now=T0 + 10), now=T0 + 10)
        self.assertEqual(counts, {"claimed": 1, "done": 1, "retry": 0, "dead": 0})
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["to"], ["alex@customer.example"])  # the verified sender, from the receiving server's header only
        self.assertEqual(self.only(spool)["outcome"], "drafted")
        self.assertEqual(spool.drain(lambda message: mailbox.handle(message, now=T0 + 20), now=T0 + 20)["claimed"], 0)

    def test_an_unverified_sender_gets_no_draft_through_the_spool_either(self):
        drafts: list = []
        notices: list[dict] = []
        spool, mailbox = self.spool(), self.mailbox(drafts, escalate=notices.append)
        spool.ingest(raw(auth=False, sender="Forged <ceo@acme.example>"), now=T0)
        spool.drain(lambda message: mailbox.handle(message, now=T0), now=T0)
        self.assertEqual(drafts, [])
        self.assertEqual(self.only(spool)["state"], "done")  # durable human notice, not a draft
        self.assertEqual(len(notices), 1)

    def test_both_human_routes_down_retries_spooled_mail_until_ticket_recovers(self):
        drafts: list = []
        spool = self.spool(max_attempts=3)
        spool.ingest(raw(), now=T0)

        def broken(_item):
            raise OSError("sensitive mailbox details must not enter the spool index")

        failing = self.mailbox(drafts, classify=lambda _message: "complaint",
                               escalate=broken, ticket=broken)
        self.assertEqual(spool.drain(lambda message: failing.handle(message, now=T0), now=T0)["retry"], 1)
        self.assertEqual(self.only(spool)["state"], "new")
        self.assertEqual(drafts, [])
        recovered = self.mailbox(drafts, classify=lambda _message: "complaint",
                                 escalate=broken, ticket=lambda _item: {"ticketId": "t-durable"})
        counts = spool.drain(lambda message: recovered.handle(message, now=T0 + 61), now=T0 + 61)
        self.assertEqual(counts, {"claimed": 1, "done": 1, "retry": 0, "dead": 0})
        self.assertEqual(self.only(spool)["outcome"], "needs-person")
        self.assertEqual(spool.stats()["dead"], 0)
        self.assertNotIn("sensitive mailbox details", spool.index.read_text() + json.dumps(self.events))

    def test_a_failing_handler_retries_then_quarantines_without_touching_other_mail(self):
        drafts: list = []
        spool = self.spool(max_attempts=2)
        spool.ingest(raw(message_id="<bad@x>", subject="BOOM"), now=T0)
        spool.ingest(raw(message_id="<good@x>"), now=T0 + 1)

        def handler(message):
            if b"BOOM" in message:
                raise RuntimeError("model unavailable: " + SECRET_BODY)
            return self.mailbox(drafts).handle(message, now=T0)

        first = spool.drain(handler, now=T0 + 5)
        self.assertEqual(first, {"claimed": 2, "done": 1, "retry": 1, "dead": 0})
        second = spool.drain(handler, now=T0 + 500)
        self.assertEqual(second, {"claimed": 1, "done": 0, "retry": 0, "dead": 1})
        self.assertEqual(len(drafts), 1)
        self.assertNotIn("4111", json.dumps(self.events) + spool.index.read_text())
        self.assertEqual(spool.stats()["dead"], 1)


class CommandTests(Case):
    def run_command(self, *argv, stdin=b""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = runtime.main([str(self.root / "gov" / "mail"), "--tenant", "acme-prod", "--address", MAILBOX, *argv], stdin=io.BytesIO(stdin))
        return code, (json.loads(out.getvalue()) if out.getvalue().strip() else None), err.getvalue()

    def ledger(self):
        path = self.root / "gov" / "audit.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_ingest_reads_one_message_from_standard_input(self):
        code, result, _ = self.run_command("ingest", stdin=raw())
        self.assertEqual((code, result["status"]), (0, "accepted"))
        code, result, _ = self.run_command("ingest", stdin=raw())
        self.assertEqual((code, result["status"]), (0, "duplicate"))
        code, stats, _ = self.run_command("stats")
        self.assertEqual((code, stats["total"], stats["new"]), (0, 1, 1))
        event = next(event for event in self.ledger() if event.get("action") == "mail-spool.accepted")
        self.assertEqual(event["performedBy"], "runtime:mail-spool")
        self.assertNotIn("Acacia", json.dumps(self.ledger()))

    def test_misdelivered_and_unusable_mail_exit_zero_so_the_server_does_not_bounce_to_a_forged_sender(self):
        code, result, _ = self.run_command("ingest", stdin=raw(to="someone@else.example"))
        self.assertEqual((code, result["status"]), (0, "misdelivered"))
        code, result, _ = self.run_command("ingest", stdin=b"")
        self.assertEqual((code, result["status"]), (0, "rejected"))

    def test_a_full_spool_exits_with_a_temporary_failure_so_the_server_retries(self):
        original = runtime.SPOOL.MAX_MESSAGES
        runtime.SPOOL.MAX_MESSAGES = 1
        self.addCleanup(setattr, runtime.SPOOL, "MAX_MESSAGES", original)
        self.run_command("ingest", stdin=raw(message_id="<1@x>"))
        code, result, error = self.run_command("ingest", stdin=raw(message_id="<2@x>"))
        self.assertEqual((code, result), (75, None))
        self.assertIn("deferred", error)
        self.assertNotIn("Acacia", error)

    def test_requeue_and_purge(self):
        self.run_command("ingest", stdin=raw())
        code, _, error = self.run_command("requeue", "m-" + "0" * 24, "--by", "jordan@client.example")
        self.assertEqual(code, 1)
        self.assertIn("Runtime mailbox failed", error)
        code, purged, _ = self.run_command("purge")
        self.assertEqual((code, purged), (0, {"removed": 0}))

    def test_bad_addresses_are_refused(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out), contextlib.redirect_stdout(io.StringIO()):
            code = runtime.main([str(self.root / "gov" / "mail"), "--tenant", "acme-prod", "--address", "nope", "stats"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()

"""Credential-free checks for booking-notice consent, opt-out, quiet hours, idempotency and deletion (#240)."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import test_coworker_pause as paused  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
prefs = paused._load("notification_prefs_under_test", "platform/core/notification_prefs.py")
EMAIL, PHONE = "alex@acme-customer.example", "+65 6123 4567"
NOON_SGT = 1_800_000_000 - (1_800_000_000 % 86_400) + 4 * 3600  # 04:00 UTC = 12:00 at UTC+8
NIGHT_SGT = NOON_SGT + 11 * 3600  # 23:00 at UTC+8


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.events = []
        self.store = prefs.NotificationPrefs(self.root, tenant="acme", audit=lambda action, by, details: self.events.append((action, by, details)))

    def grant(self, purpose="confirmation", channel="email", address=EMAIL):
        return self.store.record(channel, address, purpose, "granted", by="jordan@acme.example", now=NOON_SGT, verified_via="recipient-reply")


class ConsentTests(Case):
    def test_nothing_is_sent_without_verified_consent_and_the_address_is_never_stored(self):
        self.assertEqual(self.store.decide("email", EMAIL, "confirmation", now=NOON_SGT), {"send": False, "reason": "no-consent"})
        with self.assertRaisesRegex(prefs.NotificationError, "controls the destination"):
            self.store.record("email", EMAIL, "confirmation", "granted", by="jordan@acme.example", now=NOON_SGT)  # a CRM match is not evidence
        with self.assertRaises(prefs.NotificationError):
            self.store.record("email", EMAIL, "confirmation", "granted", by="jordan@acme.example", now=NOON_SGT, verified_via="crm-match")
        self.grant()
        self.assertEqual(self.store.decide("email", "ALEX@acme-customer.example", "confirmation", now=NOON_SGT), {"send": True})
        stored = self.store.path.read_text()
        for private in ("alex", "acme-customer"):
            self.assertNotIn(private, stored + json.dumps(self.events))
        self.assertEqual(oct(self.store.path.stat().st_mode & 0o777), "0o600")

    def test_a_reminder_needs_its_own_opt_in_and_a_confirmation_consent_does_not_cover_it(self):
        self.grant("confirmation")
        self.assertEqual(self.store.decide("email", EMAIL, "reminder", now=NOON_SGT, utc_offset_minutes=480), {"send": False, "reason": "no-consent"})
        self.grant("reminder")
        self.assertEqual(self.store.decide("email", EMAIL, "reminder", now=NOON_SGT, utc_offset_minutes=480), {"send": True})

    def test_an_opt_out_needs_no_evidence_wins_over_everything_and_stops_every_purpose(self):
        self.grant("confirmation")
        self.grant("reminder")
        self.store.record("email", EMAIL, "reminder", "opted-out", by="jordan@acme.example", now=NOON_SGT + 5)  # a STOP reply, no evidence needed
        for purpose in prefs.PURPOSES:
            self.assertEqual(self.store.decide("email", EMAIL, purpose, now=NOON_SGT + 10), {"send": False, "reason": "opted-out"})
        self.assertEqual(self.store.status("email", EMAIL)["consents"]["reminder"]["state"], "opted-out")
        self.grant("reminder")  # only a fresh, evidenced grant from the recipient lifts it, and that is ledgered
        self.assertEqual(self.store.decide("email", EMAIL, "reminder", now=NOON_SGT, utc_offset_minutes=480), {"send": True})
        self.assertEqual(self.events[-1][2]["state"], "granted")
        self.assertEqual(len([e for e in self.events if e[2].get("state") == "opted-out"]), 1)

    def test_quiet_hours_hold_a_reminder_in_the_recipients_own_time_but_not_a_confirmation(self):
        self.grant("confirmation")
        self.grant("reminder")
        self.assertEqual(self.store.decide("email", EMAIL, "reminder", now=NIGHT_SGT, utc_offset_minutes=480), {"send": False, "reason": "quiet-hours"})
        self.assertEqual(self.store.decide("email", EMAIL, "reminder", now=NIGHT_SGT, utc_offset_minutes=-300), {"send": True})  # 18:00 in New York
        self.assertEqual(self.store.decide("email", EMAIL, "confirmation", now=NIGHT_SGT, utc_offset_minutes=480), {"send": True})

    def test_channels_and_recipients_are_separate_and_tenants_do_not_share_consent(self):
        self.grant("confirmation", "email", EMAIL)
        self.assertFalse(self.store.decide("whatsapp", PHONE, "confirmation", now=NOON_SGT)["send"])
        self.assertFalse(self.store.decide("email", "someone.else@acme-customer.example", "confirmation", now=NOON_SGT)["send"])
        other = prefs.NotificationPrefs(self.root, tenant="beta")
        self.assertFalse(other.decide("email", EMAIL, "confirmation", now=NOON_SGT)["send"])
        for bad in (("fax", "x"), ("email", "not-an-address"), ("sms", "123"), ("email", " ")):
            with self.assertRaises(prefs.NotificationError):
                self.store.decide(bad[0], bad[1], "confirmation", now=NOON_SGT)
        with self.assertRaises(prefs.NotificationError):
            prefs.NotificationPrefs(self.root, tenant="../etc")


class ClaimAndRenderTests(Case):
    def test_a_booking_state_is_claimed_once_and_a_reschedule_or_cancellation_is_a_new_state(self):
        claim = lambda state, address=EMAIL: self.store.claim("bk-100", state, "email", address, "confirmation", now=NOON_SGT)  # noqa: E731
        self.assertTrue(claim("confirmed"))
        self.assertFalse(claim("confirmed"))  # a duplicated or retried event sends nothing twice
        self.assertTrue(claim("rescheduled"))
        self.assertTrue(claim("cancelled"))
        self.assertTrue(claim("confirmed", "bob@acme-customer.example"))  # another recipient
        self.assertTrue(self.store.claim("bk-101", "confirmed", "email", EMAIL, "confirmation", now=NOON_SGT))
        self.assertFalse(self.store.claim("bk-100", "confirmed", "email", EMAIL.upper(), "confirmation", now=NOON_SGT))
        for bad in (("", "confirmed"), ("bk 1", "confirmed"), ("bk-1", "booked")):
            with self.assertRaises(prefs.NotificationError):
                self.store.claim(bad[0], bad[1], "email", EMAIL, "confirmation", now=NOON_SGT)

    def test_claims_are_bounded_oldest_first(self):
        original = prefs.MAX_CLAIMS
        prefs.MAX_CLAIMS = 3
        try:
            for index in range(5):
                self.store.claim(f"bk-{index}", "confirmed", "email", EMAIL, "confirmation", now=NOON_SGT + index)
        finally:
            prefs.MAX_CLAIMS = original
        self.assertEqual(self.store.counts()["claims"], 3)
        self.assertTrue(self.store.claim("bk-0", "confirmed", "email", EMAIL, "confirmation", now=NOON_SGT + 9))  # the oldest was forgotten

    def test_the_notice_shows_the_booking_time_in_the_recipients_offset_with_contact_and_stop_instructions(self):
        text = prefs.render_booking_notice("confirmed", start=1_800_000_000, utc_offset_minutes=480, contact="the front desk on 6123 0000", label="your boiler service")
        self.assertIn("Friday 15 January 2027 at 16:00 (UTC+08:00)", text)
        self.assertIn("is confirmed for", text)
        self.assertIn("contact the front desk", text)
        self.assertIn("Reply STOP", text)
        self.assertIn("was cancelled; it had been set for", prefs.render_booking_notice("cancelled", start=1_800_000_000, utc_offset_minutes=-330, contact="x"))
        self.assertIn("UTC-05:30", prefs.render_booking_notice("rescheduled", start=1_800_000_000, utc_offset_minutes=-330, contact="x"))
        for bad in (dict(state="booked"), dict(utc_offset_minutes=900)):
            with self.assertRaises(prefs.NotificationError):
                prefs.render_booking_notice(**{"state": "confirmed", "start": 1_800_000_000, "utc_offset_minutes": 0, "contact": "x", **bad})


class LedgerAndDeletionTests(Case):
    def test_the_ledger_holds_digests_purposes_states_and_reasons_never_an_address(self):
        self.grant("confirmation")
        self.store.suppressed("quiet-hours", channel="email", address=EMAIL, purpose="reminder", booking_id="bk-100")
        self.assertEqual([event[0] for event in self.events], ["notification-prefs.recorded", "notification-prefs.suppressed"])
        self.assertEqual(self.events[1][2]["reason"], "quiet-hours")
        self.assertNotIn("alex", json.dumps(self.events))
        self.assertEqual(self.events[0][2]["verifiedVia"], "recipient-reply")

    def test_forget_deletes_the_recipient_and_the_next_decision_is_no_consent(self):
        self.grant("confirmation")
        self.assertEqual(self.store.forget("email", EMAIL, by="jordan@acme.example"), {"recipients": 1})
        self.assertEqual(self.store.decide("email", EMAIL, "confirmation", now=NOON_SGT)["reason"], "no-consent")
        self.assertEqual(self.store.forget("email", EMAIL, by="jordan@acme.example"), {"recipients": 0})
        self.assertEqual(self.store.status("email", EMAIL)["consents"], {})
        with self.assertRaises(prefs.NotificationError):
            self.store.forget("email", EMAIL, by=" ")


class CliTests(Case):
    def run_script(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts/runtime-notification-prefs.py"), str(self.root / "cli"), "--tenant", "acme",
                               "--audit-path", str(self.root / "audit.jsonl"), *args], capture_output=True, text=True)

    def test_the_operator_script_records_shows_and_forgets_and_refuses_consent_without_evidence(self):
        bad = self.run_script("record", "--channel", "email", "--address", EMAIL, "--purpose", "confirmation", "--state", "granted", "--by", "jordan@acme.example")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("controls the destination", bad.stderr)
        ok = self.run_script("record", "--channel", "email", "--address", EMAIL, "--purpose", "confirmation", "--state", "granted",
                             "--verified-via", "named-person", "--by", "jordan@acme.example")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        shown = json.loads(self.run_script("status", "--channel", "email", "--address", EMAIL).stdout)
        self.assertEqual(shown["consents"]["confirmation"]["state"], "granted")
        self.assertNotIn("alex", json.dumps(shown))
        self.assertEqual(json.loads(self.run_script("counts").stdout)["recipients"], 1)
        self.assertEqual(json.loads(self.run_script("forget", "--channel", "email", "--address", EMAIL, "--by", "jordan@acme.example").stdout), {"recipients": 1})
        ledger = (self.root / "audit.jsonl").read_text()
        self.assertNotIn("alex", ledger)
        self.assertIn("notification-prefs.forgotten", ledger)


if __name__ == "__main__":
    unittest.main()

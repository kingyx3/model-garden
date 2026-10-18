"""Credential-free checks for the Tier 0 built-in ticket store (#77)."""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("tickets_under_test", ROOT / "platform" / "core" / "tickets.py")
tickets = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["tickets_under_test"] = tickets
spec.loader.exec_module(tickets)

ALICE, STAFF, CUSTOMER, OTHER = "alice@acme.example", "jordan@acme.example", "unmapped:slack:cust1", "unmapped:slack:cust2"
T0 = 1_800_000_000


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = tickets.TicketStore(pathlib.Path(self._tmp.name) / "tickets", tenant="acme")

    def create(self, **overrides):
        values = dict(project="support", title="Boiler quote follow-up", description="Customer wants a call back", requested_by=CUSTOMER, agent="receptionist", now=T0)
        values.update(overrides)
        return self.store.create(**values)


class CreateTests(Case):
    def test_a_ticket_is_idempotent_and_names_where_it_lives(self):
        first = self.create()
        again = self.create(now=T0 + 60)
        self.assertEqual((first["duplicate"], again["duplicate"], again["ticketId"]), (False, True, first["ticketId"]))
        self.assertRegex(first["ticketId"], r"^t-[0-9a-f]{12}$")
        self.assertEqual(first["externalObjectRef"], f"tier0:tickets/acme/{first['ticketId']}")
        self.assertNotEqual(self.create(title="A different matter")["ticketId"], first["ticketId"])
        self.assertNotEqual(self.create(requested_by=OTHER)["ticketId"], first["ticketId"])  # two people asking the same thing are two tickets

    def test_input_is_validated_and_card_numbers_are_redacted(self):
        for bad in (dict(project="Bad Project"), dict(project="x" * 41), dict(title=" "), dict(title="x" * 121), dict(description="x" * 2001),
                    dict(requested_by="")):
            with self.assertRaises(tickets.TicketError, msg=str(bad)):
                self.create(**bad)
        made = self.create(description="Card 4111 1111 1111 1111 was declined")
        stored = self.store.get(made["ticketId"])
        self.assertNotIn("4111", stored["description"])
        self.assertIn("[REDACTED:card]", stored["description"])

    def test_the_store_is_private_and_bounded(self):
        self.create()
        self.assertEqual(oct(os.stat(self.store.root).st_mode & 0o777), "0o700")
        self.assertEqual(oct(os.stat(self.store.path).st_mode & 0o777), "0o600")
        with unittest.mock.patch.object(tickets, "MAX_TICKETS", 1):
            with self.assertRaises(tickets.TicketError):
                self.create(title="One too many")
        with self.assertRaises(tickets.TicketError):
            tickets.TicketStore(self.store.root, tenant="../escape")

    def test_an_unreadable_store_refuses_instead_of_starting_empty(self):
        self.create()
        self.store.path.write_text("{not json")
        with self.assertRaises(tickets.TicketError):
            self.create(title="Another")


class UpdateTests(Case):
    def test_only_staff_update_and_every_change_records_who(self):
        made = self.create()
        with self.assertRaises(tickets.TicketError):
            self.store.update(ticket_id=made["ticketId"], status="done", requested_by=CUSTOMER, principal_access="public")
        with self.assertRaises(tickets.TicketError):
            self.store.update(ticket_id=made["ticketId"], status="done", requested_by=STAFF, principal_access=None)
        result = self.store.update(ticket_id=made["ticketId"], status="in-progress", comment="Calling back at 3pm", requested_by=STAFF,
                                   principal_access="member", now=T0 + 120)
        self.assertEqual(result["status"], "in-progress")
        shown = self.store.get(made["ticketId"])
        self.assertEqual((shown["status"], shown["updatedAt"]), ("in-progress", T0 + 120))
        self.assertEqual(shown["comments"], [{"by": STAFF, "at": T0 + 120, "text": "Calling back at 3pm", "status": "in-progress"}])

    def test_done_and_cancelled_are_final(self):
        made = self.create()
        self.store.update(ticket_id=made["ticketId"], status="done", requested_by=STAFF, principal_access="member")
        with self.assertRaises(tickets.TicketError):
            self.store.update(ticket_id=made["ticketId"], status="open", requested_by=STAFF, principal_access="member")
        with self.assertRaises(tickets.TicketError):
            self.store.update(ticket_id=made["ticketId"], comment="late note", requested_by=STAFF, principal_access="member")

    def test_bad_updates_are_refused(self):
        made = self.create()
        member = dict(requested_by=STAFF, principal_access="member")
        for bad in (dict(ticket_id="nope"), dict(ticket_id="t-000000000000", status="done"), dict(ticket_id=made["ticketId"]),
                    dict(ticket_id=made["ticketId"], status="closed"), dict(ticket_id=made["ticketId"], comment="x" * 1001)):
            with self.assertRaises(tickets.TicketError, msg=str(bad)):
                self.store.update(**bad, **member)


class SearchTests(Case):
    def test_staff_see_everything_with_text_and_others_only_their_own(self):
        mine = self.create(title="My boiler")
        theirs = self.create(title="Their boiler", requested_by=OTHER)
        staff = self.store.search(query="boiler", requested_by=STAFF, principal_access="member")
        self.assertEqual({item["ticketId"] for item in staff["tickets"]}, {mine["ticketId"], theirs["ticketId"]})
        self.assertIn("description", staff["tickets"][0])
        own = self.store.search(query="boiler", requested_by=CUSTOMER, principal_access="public")
        self.assertEqual([item["ticketId"] for item in own["tickets"]], [mine["ticketId"]])
        self.assertNotIn("description", own["tickets"][0])  # no description or history for a requester
        self.assertEqual(self.store.search(query="boiler", requested_by="unmapped:slack:nobody", principal_access=None)["tickets"], [])
        self.assertEqual(self.store.search(query="*", requested_by="unmapped:slack:nobody", principal_access=None)["tickets"], [])

    def test_search_matches_project_id_and_status_and_is_bounded(self):
        made = self.create()
        self.create(project="sales", title="Quote for fence")
        self.store.update(ticket_id=made["ticketId"], status="done", requested_by=STAFF, principal_access="member")
        find = lambda query, **kw: [item["ticketId"] for item in self.store.search(query=query, requested_by=STAFF, principal_access="member", **kw)["tickets"]]
        self.assertEqual(find("support"), [made["ticketId"]])
        self.assertEqual(find(made["ticketId"]), [made["ticketId"]])
        self.assertEqual(len(find("*")), 2)
        self.assertEqual(find("*", status="open"), [t for t in find("*") if t != made["ticketId"]])
        for bad in (dict(query=" "), dict(query="x" * 101), dict(query="*", limit=0), dict(query="*", limit=21), dict(query="*", status="bogus")):
            with self.assertRaises(tickets.TicketError, msg=str(bad)):
                self.store.search(requested_by=STAFF, principal_access="member", **bad)

    def test_the_inbox_view_lists_open_items_without_text(self):
        made = self.create(title="Secret matter", description="Private details")
        self.create(title="Closed one")
        closed = self.store.search(query="Closed one", requested_by=STAFF, principal_access="member")["tickets"][0]["ticketId"]
        self.store.update(ticket_id=closed, status="cancelled", requested_by=STAFF, principal_access="member")
        items = self.store.open_items()
        self.assertEqual([item["ticketId"] for item in items], [made["ticketId"]])
        self.assertNotIn("Secret", json.dumps(items))
        self.assertNotIn("Private", json.dumps(items))


class OperatorCliTests(Case):
    cli = None

    @classmethod
    def setUpClass(cls) -> None:
        cli_spec = importlib.util.spec_from_file_location("runtime_tickets_cli_under_test", ROOT / "scripts" / "runtime-tickets.py")
        cls.cli = importlib.util.module_from_spec(cli_spec)
        sys.modules["runtime_tickets_cli_under_test"] = cls.cli
        cli_spec.loader.exec_module(cls.cli)

    def run_cli(self, *argv):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.cli.main(["--data-root", str(self.store.root.parent), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_list_shows_titles_show_prints_the_text_and_update_is_ledgered_without_it(self):
        made = self.create(title="Boiler quote", description="Private customer detail")
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual([(row["ticketId"], row["status"]) for row in rows], [(made["ticketId"], "open")])
        self.assertNotIn("Private", out)
        code, out, _ = self.run_cli("show", made["ticketId"])
        self.assertEqual(json.loads(out)["description"], "Private customer detail")
        code, out, _ = self.run_cli("update", made["ticketId"], "--status", "done", "--comment", "Sorted by phone", "--by", STAFF)
        self.assertEqual((code, json.loads(out)["status"]), (0, "done"))
        ledger = [json.loads(line) for line in (self.store.root.parent / "audit.jsonl").read_text().splitlines()]
        self.assertEqual([(event["action"], event["performedBy"], event["details"]["status"]) for event in ledger], [("ticket.update", STAFF, "done")])
        self.assertNotIn("Sorted by phone", json.dumps(ledger))
        self.assertEqual(self.store.get(made["ticketId"])["comments"][0]["by"], STAFF)

    def test_bad_requests_exit_nonzero_without_a_stack_trace(self):
        made = self.create()
        for argv in (("update", made["ticketId"], "--status", "done", "--by", "not-an-email"), ("show", "t-000000000000"),
                     ("list", "--status", "bogus"), ("--tenant", "other", "list")):
            code, out, err = self.run_cli(*argv)
            self.assertEqual((code, out), (1, ""), argv)
            self.assertTrue(err.startswith("Tickets failed:"), argv)


if __name__ == "__main__":
    unittest.main()

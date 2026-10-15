"""Credential-free checks for the HubSpot CRM adapter: field allow-list, no merging, duplicate-safe logging, sync error codes."""
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


hubspot = _load("hubspot_under_test", "platform/connectors/hubspot.py")
sync = _load("crm_sync_for_hubspot", "platform/core/crm_sync.py")
CATALOG = json.loads((ROOT / "contracts/v1/capabilities.json").read_text())["families"]["crm"]["tools"]
TOKEN = "pat-na1-super-secret"
ALLOWED = {"contact": ["firstname", "lastname", "email", "phone"], "company": ["name", "domain"], "deal": ["dealname", "amount"]}
WRITABLE = {"contact": ["firstname", "lastname", "email"]}
VISA = "4111 1111 1111 1111"


class FakeHubSpot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict, dict | None]] = []
        self.notes: dict[str, dict] = {}
        self.status_for_next: int | None = None

    def __call__(self, method, url, headers, payload):
        self.calls.append((method, url, headers, payload))
        if self.status_for_next is not None:
            status, self.status_for_next = self.status_for_next, None
            return status, {"message": "Contact already exists. Existing ID: 777" if status == 409 else "error detail " + TOKEN}
        path = urllib.parse.urlparse(url).path
        if method == "GET" and path.startswith("/crm/v3/objects/contacts/"):
            return 200, {"id": path.rsplit("/", 1)[1], "properties": {
                "firstname": "Alex", "lastname": "Tan", "email": "alex@customer.example", "phone": "+65 6123 4567",
                "hs_object_id": "42", "lifecyclestage": "customer", "secret_notes": "VIP; owes money", "createdate": "2026-01-01"}}
        if method == "POST" and path.endswith("/contacts/search"):
            return 200, {"results": [{"id": "42", "properties": {"firstname": "Alex", "ssn": "123-45-6789", "email": "alex@customer.example"}}]}
        if method == "PATCH":
            return 200, {"id": path.rsplit("/", 1)[1]}
        if method == "POST" and path.endswith("/notes/search"):
            token = payload["filterGroups"][0]["filters"][0]["value"]
            hits = [{"id": note_id} for note_id, note in self.notes.items() if token in note["properties"]["hs_note_body"]]
            return 200, {"results": hits}
        if method == "POST" and path.endswith("/notes"):
            note_id = str(900 + len(self.notes))
            self.notes[note_id] = payload
            return 201, {"id": note_id}
        if method == "POST" and path.endswith("/contacts"):
            return 201, {"id": "43"}
        return 404, {"message": "not found"}


def make(**overrides):
    fake = FakeHubSpot()
    values = dict(allowed_fields=ALLOWED, writable_fields=WRITABLE, transport=fake)
    values.update(overrides)
    return hubspot.HubSpotClient(TOKEN, **values), fake


LOG = {"objectId": "42", "interactionType": "call", "channel": "voice", "direction": "inbound", "timestamp": "2026-10-15T10:00:00+08:00",
       "summary": "Asked about opening hours; offered a Friday booking.", "outcome": "booked", "transcriptRef": "transcript:abc123"}


class ReadCase(unittest.TestCase):
    def test_only_allow_listed_properties_are_requested_and_returned(self):
        client, fake = make()
        result = client.read({"objectType": "contact", "objectId": "42"})
        record = result["records"][0]
        self.assertEqual(sorted(record["fields"]), ["email", "firstname", "lastname", "phone"])
        self.assertNotIn("secret_notes", json.dumps(result))  # HubSpot sent it; the model never sees it
        self.assertEqual((record["objectType"], record["objectId"], record["externalObjectRef"]), ("contact", "42", "hubspot:contact/42"))
        url = fake.calls[0][1]
        self.assertIn("properties=email,firstname,lastname,phone", url)
        self.assertNotIn("secret_notes", url)

    def test_search_filters_every_record_and_is_bounded(self):
        client, fake = make()
        result = client.read({"query": "alex"})
        self.assertEqual(sorted(result["records"][0]["fields"]), ["email", "firstname"])
        self.assertNotIn("ssn", json.dumps(result))
        self.assertEqual(fake.calls[0][3]["limit"], 10)
        self.assertEqual(fake.calls[0][3]["properties"], ["email", "firstname", "lastname", "phone"])

    def test_invalid_reads_are_refused_before_any_request(self):
        client, fake = make()
        for arguments in ({}, {"objectId": "42", "query": "x"}, {"objectId": "../x"}, {"objectId": 42}, {"objectType": "ticket", "objectId": "1"},
                          {"query": " "}, {"query": "q" * 201}, {"objectType": "lead", "query": "x"}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                client.read(arguments)
        with self.assertRaises(ValueError):
            client.read("not an object")
        self.assertEqual(fake.calls, [])

    def test_only_configured_object_types_are_reachable(self):
        client, fake = make(allowed_fields={"contact": ["email"]}, writable_fields=None)
        with self.assertRaisesRegex(ValueError, "configured types"):
            client.read({"objectType": "deal", "objectId": "1"})
        self.assertEqual(fake.calls, [])

    def test_catalog_shape_and_tools(self):
        self.assertEqual(sorted(make()[0].executors()), sorted(CATALOG))
        self.assertEqual(sorted(hubspot.SCOPES), sorted(CATALOG))


class WriteCase(unittest.TestCase):
    def test_update_writes_only_writable_properties(self):
        client, fake = make()
        result = client.update({"objectId": "42", "fields": {"firstname": "Alexander", "email": "a@customer.example"}})
        method, url, _, payload = fake.calls[-1]
        self.assertEqual((method, payload), ("PATCH", {"properties": {"firstname": "Alexander", "email": "a@customer.example"}}))
        self.assertTrue(url.endswith("/crm/v3/objects/contacts/42"))
        self.assertEqual((result["externalObjectRef"], result["updated"]), ("hubspot:contact/42", ["email", "firstname"]))

    def test_a_property_that_is_readable_but_not_writable_or_unlisted_is_refused_by_name_only(self):
        client, fake = make()
        for fields in ({"phone": "+65 0"}, {"lifecyclestage": "customer"}, {"firstname": "A", "hs_owner": "1"}):
            with self.assertRaisesRegex(ValueError, "may not be written") as raised:
                client.update({"objectId": "42", "fields": fields})
            self.assertNotIn("+65 0", str(raised.exception))  # values are never echoed
        self.assertEqual(fake.calls, [])

    def test_without_writable_fields_the_connection_cannot_update_or_create(self):
        client, fake = make(writable_fields=None)
        with self.assertRaises(ValueError):
            client.update({"objectId": "42", "fields": {"firstname": "A"}})
        with self.assertRaises(ValueError):
            client.create({"fields": {"firstname": "A"}})
        self.assertEqual(fake.calls, [])

    def test_invalid_writes_are_refused(self):
        client, fake = make()
        for arguments in ({"objectId": "42"}, {"objectId": "42", "fields": {}}, {"objectId": "42", "fields": {"firstname": {"a": 1}}},
                          {"objectId": "42", "fields": {"firstname": None}}, {"objectId": "42", "fields": {"firstname": "x" * 501}},
                          {"objectId": "x", "fields": {"firstname": "A"}}, {"objectId": "42", "fields": {f"p{n}": "v" for n in range(31)}}):
            with self.subTest(arguments=str(arguments)[:60]), self.assertRaises(ValueError):
                client.update(arguments)
        self.assertEqual(fake.calls, [])

    def test_card_numbers_are_redacted_before_they_are_written(self):
        client, fake = make()
        client.update({"objectId": "42", "fields": {"firstname": f"Paid with {VISA}"}})
        self.assertNotIn("4111", json.dumps(fake.calls[-1][3]))
        self.assertIn("[REDACTED:card]", fake.calls[-1][3]["properties"]["firstname"])

    def test_create_posts_a_new_record_and_a_conflict_is_refused_not_merged(self):
        client, fake = make()
        created = client.create({"objectType": "contact", "fields": {"firstname": "Sam", "email": "sam@customer.example"}})
        self.assertEqual((created["objectId"], created["externalObjectRef"]), ("43", "hubspot:contact/43"))
        self.assertEqual(fake.calls[-1][0], "POST")
        fake.status_for_next = 409
        with self.assertRaisesRegex(hubspot.HubSpotError, "never merged") as raised:
            client.create({"fields": {"email": "alex@customer.example"}})
        self.assertNotIn("777", str(raised.exception))  # the existing record's id is not revealed
        self.assertEqual(raised.exception.code, "rejected")
        self.assertFalse(any(call[0] in ("PATCH", "PUT") for call in fake.calls))  # no fall-through to an update


class LogInteractionCase(unittest.TestCase):
    def test_an_interaction_becomes_a_note_associated_with_the_record_holding_a_reference_not_a_transcript(self):
        client, fake = make()
        result = client.log_interaction(LOG)
        self.assertEqual((result["duplicate"], result["externalObjectRef"]), (False, f"hubspot:note/{result['interactionId']}"))
        payload = fake.calls[-1][3]
        body = payload["properties"]["hs_note_body"]
        self.assertIn("[call / inbound via voice]", body)
        self.assertIn("Transcript reference: transcript:abc123", body)
        self.assertEqual(payload["properties"]["hs_timestamp"], "2026-10-15T02:00:00Z")  # normalised to UTC
        self.assertEqual(payload["associations"][0]["to"], {"id": "42"})
        self.assertEqual(payload["associations"][0]["types"][0]["associationTypeId"], 202)

    def test_a_redelivered_interaction_finds_the_existing_note_and_writes_nothing(self):
        client, fake = make()
        first = client.log_interaction(LOG)
        writes_before = [call for call in fake.calls if call[3] and call[1].endswith("/notes")]
        second = client.log_interaction(LOG)
        writes_after = [call for call in fake.calls if call[3] and call[1].endswith("/notes")]
        self.assertEqual((second["duplicate"], second["interactionId"]), (True, first["interactionId"]))
        self.assertEqual(len(writes_before), len(writes_after))
        self.assertEqual(len(fake.notes), 1)
        different = client.log_interaction({**LOG, "summary": "A different call"})
        self.assertFalse(different["duplicate"])
        self.assertEqual(len(fake.notes), 2)

    def test_a_restart_with_a_new_client_still_finds_the_note(self):
        first, fake = make()
        first.log_interaction(LOG)
        second = hubspot.HubSpotClient(TOKEN, allowed_fields=ALLOWED, writable_fields=WRITABLE, transport=fake)
        self.assertTrue(second.log_interaction(LOG)["duplicate"])

    def test_card_numbers_in_the_summary_are_redacted(self):
        client, fake = make()
        client.log_interaction({**LOG, "summary": f"Caller read out {VISA} to pay"})
        self.assertNotIn("4111", json.dumps(fake.calls))

    def test_invalid_interactions_are_refused_before_any_request(self):
        client, fake = make()
        for overrides in ({"interactionType": "sms"}, {"direction": "sideways"}, {"channel": "bad<>"}, {"timestamp": "yesterday"},
                          {"timestamp": "2026-10-15T10:00:00"}, {"summary": " "}, {"summary": "s" * 2001}, {"outcome": "x" * 101},
                          {"transcriptRef": "the caller said hello"}, {"objectId": "abc"}, {"objectType": "ticket"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                client.log_interaction({**LOG, **overrides})
        self.assertEqual(fake.calls, [])


class ErrorCase(unittest.TestCase):
    def test_failures_map_to_sync_codes_and_never_leak_the_token(self):
        client, fake = make()
        expected = {429: "rate-limited", 500: "outage", 503: "outage", 401: "unauthorized", 403: "unauthorized", 400: "rejected", 404: "rejected", 422: "rejected"}
        for status, code in expected.items():
            fake.status_for_next = status
            with self.subTest(status=status), self.assertRaises(hubspot.HubSpotError) as raised:
                client.read({"objectId": "42"})
            self.assertEqual((raised.exception.code, raised.exception.status), (code, status))
            self.assertIn(code, sync.ERROR_CODES)
            self.assertNotIn(TOKEN, str(raised.exception))

    def test_every_request_is_authenticated_with_the_injected_token_only(self):
        client, fake = make()
        client.read({"objectId": "42"})
        self.assertEqual(fake.calls[0][2]["Authorization"], f"Bearer {TOKEN}")

    def test_construction_is_strict(self):
        for kwargs in ({"allowed_fields": {}}, {"allowed_fields": {"ticket": ["subject"]}}, {"allowed_fields": {"contact": []}},
                       {"allowed_fields": {"contact": ["Not A Property"]}},
                       {"allowed_fields": {"contact": ["email"]}, "writable_fields": {"contact": ["phone"]}},  # writable not a subset
                       {"allowed_fields": {"contact": ["email"]}, "writable_fields": {"company": ["name"]}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                make(**kwargs)
        with self.assertRaises(ValueError):
            hubspot.HubSpotClient("", allowed_fields=ALLOWED)

    def test_the_default_transport_only_reaches_the_fixed_https_host(self):
        for bad in ("http://api.hubapi.com/x", "https://evil.example/x", "https://api.hubapi.com:8443/x"):
            with self.subTest(url=bad), self.assertRaises(ValueError):
                hubspot._urllib_transport("GET", bad, {}, None)


if __name__ == "__main__":
    unittest.main()

"""Credential-free checks for the Salesforce CRM adapter: field allow-list, no merging, duplicate-safe logging, sync error codes."""
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


salesforce = _load("salesforce_under_test", "platform/connectors/salesforce.py")
sync = _load("crm_sync_for_salesforce", "platform/core/crm_sync.py")
CATALOG = json.loads((ROOT / "contracts/v1/capabilities.json").read_text())["families"]["crm"]["tools"]
REGISTRY = {item["id"]: item for item in json.loads((ROOT / "contracts/v1/integrations.json").read_text())["providers"]}
TOKEN = "00D5g000-super-secret-session-token"
INSTANCE = "https://acme.my.salesforce.com"
CONTACT = "0035g00000AbCdEAAA"
ACCOUNT = "0015g00000XyZaBAAA"
ALLOWED = {"contact": ["FirstName", "LastName", "Email", "Phone"], "company": ["Name", "Website"], "deal": ["Name", "Amount"]}
WRITABLE = {"contact": ["FirstName", "LastName", "Email"]}
VISA = "4111 1111 1111 1111"


class FakeSalesforce:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict, object]] = []
        self.tasks: dict[str, dict] = {}
        self.fail_with: list[tuple[int, object]] = []

    def __call__(self, method, url, headers, payload):
        self.calls.append((method, url, headers, payload))
        if self.fail_with:
            return self.fail_with.pop(0)
        parsed = urllib.parse.urlparse(url)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        if method == "GET" and "/sobjects/" in path and not path.endswith("/sobjects/Task"):
            return 200, {"attributes": {"type": path.split("/sobjects/")[1].split("/")[0]}, "Id": path.rsplit("/", 1)[1], "FirstName": "Alex", "LastName": "Tan",
                         "Email": "alex@customer.example", "Phone": "+65 6123 4567", "SSN__c": "123-45-6789", "Description": "VIP; owes money"}
        if method == "GET" and path.endswith("/search/"):
            return 200, {"searchRecords": [{"attributes": {"type": "Contact"}, "Id": CONTACT, "FirstName": "Alex", "SSN__c": "123-45-6789",
                                            "Email": "alex@customer.example"}]}
        if method == "GET" and path.endswith("/query"):
            soql = query["q"][0]
            hits = [{"Id": task_id} for task_id, task in self.tasks.items() if soql.split("%")[1] in task["Subject"]]
            return 200, {"records": hits, "totalSize": len(hits)}
        if method == "PATCH":
            return 204, {}
        if method == "POST" and path.endswith("/sobjects/Task"):
            task_id = f"00T5g0000{len(self.tasks):09d}"
            self.tasks[task_id] = payload
            return 201, {"id": task_id, "success": True}
        if method == "POST" and "/sobjects/" in path:
            return 201, {"id": "0035g00000NewCtAAA", "success": True}
        return 404, [{"message": "not found", "errorCode": "NOT_FOUND"}]


def make(**overrides):
    fake = FakeSalesforce()
    values = dict(instance_url=INSTANCE, allowed_fields=ALLOWED, writable_fields=WRITABLE, transport=fake)
    values.update(overrides)
    return salesforce.SalesforceClient(TOKEN, **values), fake


LOG = {"objectId": CONTACT, "interactionType": "call", "channel": "voice", "direction": "inbound", "timestamp": "2026-10-15T10:00:00+08:00",
       "summary": "Asked about opening hours; offered a Friday booking.", "outcome": "booked", "transcriptRef": "transcript:abc123"}


class ReadTests(unittest.TestCase):
    def test_only_allow_listed_fields_are_requested_and_returned(self):
        client, fake = make()
        record = client.read({"objectType": "contact", "objectId": CONTACT})["records"][0]
        self.assertEqual(record["fields"], {"FirstName": "Alex", "LastName": "Tan", "Email": "alex@customer.example", "Phone": "+65 6123 4567"})
        self.assertEqual((record["objectId"], record["externalObjectRef"]), (CONTACT, f"salesforce:contact/{CONTACT}"))
        url = fake.calls[0][1]
        self.assertTrue(url.startswith(f"{INSTANCE}/services/data/v60.0/sobjects/Contact/{CONTACT}?fields="))
        self.assertEqual(url.split("fields=")[1], "Email,FirstName,LastName,Phone")
        self.assertNotIn("SSN__c", json.dumps(record) + url)

    def test_search_uses_sosl_with_the_allow_listed_fields_and_never_returns_others(self):
        client, fake = make()
        result = client.read({"objectType": "contact", "query": "Alex Tan"})
        self.assertEqual(set(result["records"][0]["fields"]), {"FirstName", "Email"})
        sosl = urllib.parse.parse_qs(urllib.parse.urlparse(fake.calls[0][1]).query)["q"][0]
        self.assertEqual(sosl, "FIND {Alex Tan} IN ALL FIELDS RETURNING Contact(Id,Email,FirstName,LastName,Phone) LIMIT 10")

    def test_a_search_term_cannot_break_out_of_the_sosl_braces_or_the_returning_clause(self):
        client, fake = make()
        client.read({"objectType": "contact", "query": "x} RETURNING User(Id,Password) LIMIT 5 {\"'\\"})
        sosl = urllib.parse.parse_qs(urllib.parse.urlparse(fake.calls[0][1]).query)["q"][0]
        self.assertEqual(sosl.count("RETURNING"), 2)  # the attacker's word is inside the escaped term
        term = sosl[len("FIND {"):sosl.index("} IN ALL FIELDS")]
        self.assertNotIn("}", term.replace("\\}", ""))
        self.assertIn("\\}", term)
        self.assertEqual(salesforce.sosl_term("a-b+c"), "a\\-b\\+c")

    def test_the_record_id_is_validated_so_it_cannot_inject_into_a_path(self):
        client, fake = make()
        for bad in ("../../User/005", CONTACT + "' OR Name != '", "123", 42, ""):
            with self.assertRaises(ValueError, msg=str(bad)):
                client.read({"objectType": "contact", "objectId": bad})
        self.assertEqual(fake.calls, [])

    def test_read_needs_exactly_one_selector_and_a_configured_type(self):
        client, _ = make()
        for arguments in ({}, {"objectId": CONTACT, "query": "x"}, {"query": " "}, {"query": "x" * 201}, {"objectType": "lead", "objectId": CONTACT}):
            with self.assertRaises(ValueError, msg=str(arguments)):
                client.read(arguments)
        with self.assertRaises(ValueError):
            client.read("not an object")  # type: ignore[arg-type]

    def test_object_types_map_to_salesforce_objects_and_stay_within_what_is_configured(self):
        client, fake = make()
        client.read({"objectType": "company", "objectId": ACCOUNT})
        client.read({"objectType": "deal", "objectId": ACCOUNT})
        self.assertIn("/sobjects/Account/", fake.calls[0][1])
        self.assertIn("/sobjects/Opportunity/", fake.calls[1][1])
        narrow, _ = make(allowed_fields={"contact": ["Email"]}, writable_fields={})
        with self.assertRaises(ValueError):
            narrow.read({"objectType": "company", "objectId": ACCOUNT})


class WriteTests(unittest.TestCase):
    def test_a_connection_with_no_writable_fields_is_read_only(self):
        client, fake = make(writable_fields=None)
        for tool, arguments in ((client.update, {"objectId": CONTACT, "fields": {"Email": "a@b.example"}}),
                                (client.create, {"fields": {"Email": "a@b.example"}})):
            with self.assertRaisesRegex(ValueError, "may not be written"):
                tool(arguments)
        self.assertEqual(fake.calls, [])

    def test_only_writable_fields_are_sent_and_others_are_refused_before_any_request(self):
        client, fake = make()
        result = client.update({"objectId": CONTACT, "fields": {"Email": "new@customer.example", "LastName": "Lim"}})
        self.assertEqual((result["updated"], result["externalObjectRef"]), (["Email", "LastName"], f"salesforce:contact/{CONTACT}"))
        method, url, _, payload = fake.calls[0]
        self.assertEqual((method, payload), ("PATCH", {"Email": "new@customer.example", "LastName": "Lim"}))
        for fields in ({"Phone": "1"}, {"Email": "a@b.example", "Phone": "1"}, {}, {"Email": {"x": 1}}, {"Email": None}, {"Email": "x" * 501}):
            with self.assertRaises(ValueError, msg=str(fields)):
                client.update({"objectId": CONTACT, "fields": fields})
        self.assertEqual(len(fake.calls), 1)

    def test_card_numbers_in_a_written_value_are_redacted_before_they_leave(self):
        client, fake = make()
        client.update({"objectId": CONTACT, "fields": {"LastName": f"Tan {VISA}"}})
        self.assertNotIn("4111", json.dumps(fake.calls[0][3]))

    def test_create_never_merges_and_asks_salesforce_to_refuse_duplicates_without_revealing_the_existing_record(self):
        client, fake = make()
        created = client.create({"fields": {"FirstName": "Sam", "LastName": "Ng"}})
        self.assertEqual(created["externalObjectRef"], "salesforce:contact/0035g00000NewCtAAA")
        self.assertEqual(fake.calls[0][2]["Sforce-Duplicate-Rule-Header"], "allowSave=false")
        fake.fail_with = [(400, [{"message": "Use one of these records? 0035g00000SecretAAA Alex Tan", "errorCode": "DUPLICATES_DETECTED"}])]
        with self.assertRaises(salesforce.SalesforceError) as raised:
            client.create({"fields": {"FirstName": "Alex", "LastName": "Tan"}})
        self.assertEqual(raised.exception.code, "rejected")
        self.assertIn("never merged", str(raised.exception))
        self.assertNotIn("0035g00000SecretAAA", str(raised.exception))
        self.assertNotIn("Alex Tan", str(raised.exception))
        self.assertEqual([call[0] for call in fake.calls], ["POST", "POST"])  # nothing was updated instead


class LogInteractionTests(unittest.TestCase):
    def test_an_interaction_becomes_a_completed_task_on_the_contact_with_a_marker_and_a_reference_not_a_transcript(self):
        client, fake = make()
        result = client.log_interaction(LOG)
        self.assertFalse(result["duplicate"])
        self.assertTrue(result["externalObjectRef"].startswith("salesforce:task/00T5g0000"))
        (task,) = fake.tasks.values()
        self.assertEqual((task["Status"], task["TaskSubtype"], task["WhoId"], task["ActivityDate"]), ("Completed", "Call", CONTACT, "2026-10-15"))
        self.assertNotIn("WhatId", task)
        self.assertIn("Transcript reference: transcript:abc123", task["Description"])
        self.assertIn("Outcome: booked", task["Description"])
        self.assertRegex(task["Subject"], r" mgkey[0-9a-f]{32}$")
        self.assertLessEqual(len(task["Subject"]), 255)

    def test_a_company_or_deal_interaction_uses_whatid(self):
        client, fake = make()
        client.log_interaction({**LOG, "objectType": "company", "objectId": ACCOUNT})
        (task,) = fake.tasks.values()
        self.assertEqual((task["WhatId"], "WhoId" in task), (ACCOUNT, False))

    def test_a_redelivered_interaction_writes_nothing_and_a_different_one_does(self):
        client, fake = make()
        first = client.log_interaction(LOG)
        again = client.log_interaction(LOG)
        self.assertEqual((again["duplicate"], again["externalObjectRef"]), (True, first["externalObjectRef"]))
        self.assertEqual(len(fake.tasks), 1)
        client.log_interaction({**LOG, "summary": "A different call", "timestamp": "2026-10-15T11:00:00+08:00"})
        self.assertEqual(len(fake.tasks), 2)

    def test_a_long_summary_still_ends_the_subject_with_the_whole_marker_and_card_numbers_are_redacted(self):
        client, fake = make()
        client.log_interaction({**LOG, "summary": f"Card {VISA} " + "long " * 380})
        (task,) = fake.tasks.values()
        self.assertEqual(len(task["Subject"]), 255)
        self.assertRegex(task["Subject"], r" mgkey[0-9a-f]{32}$")
        self.assertNotIn("4111", json.dumps(task))

    def test_arguments_are_validated_before_any_request(self):
        client, fake = make()
        for overrides in ({"interactionType": "sms"}, {"direction": "sideways"}, {"channel": "a b!"}, {"timestamp": "yesterday"},
                          {"timestamp": "2026-10-15T10:00:00"}, {"summary": " "}, {"summary": "x" * 2001}, {"outcome": "no\nnewline"},
                          {"transcriptRef": "the caller said hello there"}, {"objectId": "42"}):
            with self.assertRaises(ValueError, msg=str(overrides)):
                client.log_interaction({**LOG, **overrides})
        self.assertEqual(fake.calls, [])


class ErrorTests(unittest.TestCase):
    def codes(self, status, body):
        client, fake = make()
        fake.fail_with = [(status, body)]
        with self.assertRaises(salesforce.SalesforceError) as raised:
            client.read({"objectType": "contact", "objectId": CONTACT})
        return raised.exception

    def test_failures_map_to_sync_codes(self):
        cases = [
            (429, {}, "rate-limited"), (403, [{"errorCode": "REQUEST_LIMIT_EXCEEDED"}], "rate-limited"),
            (401, [{"errorCode": "INVALID_SESSION_ID"}], "unauthorized"), (403, [{"errorCode": "INSUFFICIENT_ACCESS"}], "unauthorized"),
            (500, {}, "outage"), (503, {}, "outage"), (404, [{"errorCode": "NOT_FOUND"}], "rejected"), (400, [{"errorCode": "MALFORMED_QUERY"}], "rejected"),
        ]
        for status, body, expected in cases:
            error = self.codes(status, body)
            self.assertEqual((error.code, error.status), (expected, status), (status, body))
            self.assertIn(error.code, sync.ERROR_CODES)

    def test_error_text_and_the_token_are_never_echoed(self):
        error = self.codes(400, [{"message": f"bad value for {TOKEN} and alex@customer.example", "errorCode": "FIELD_INTEGRITY_EXCEPTION"}])
        self.assertNotIn(TOKEN, str(error))
        self.assertNotIn("alex@", str(error))
        self.assertIn("FIELD_INTEGRITY_EXCEPTION", str(error))
        odd = self.codes(400, [{"message": "x", "errorCode": "not a code!"}])
        self.assertNotIn("not a code", str(odd))

    def test_a_non_object_success_response_is_an_outage_not_a_crash(self):
        client, fake = make()
        fake.fail_with = [(200, {"records": "garbage"})]
        with self.assertRaises(salesforce.SalesforceError) as raised:
            client.read({"objectType": "contact", "objectId": CONTACT})
        self.assertEqual(raised.exception.code, "outage")


class ConstructionTests(unittest.TestCase):
    def test_construction_is_strict(self):
        fake = FakeSalesforce()
        bad = (
            dict(access_token=""), dict(instance_url="http://acme.my.salesforce.com"), dict(instance_url="https://evil.example.com"),
            dict(instance_url="https://acme.my.salesforce.com.evil.example"), dict(instance_url="https://acme.my.salesforce.com:8443"),
            dict(instance_url="https://acme.my.salesforce.com/path"), dict(allowed_fields={}), dict(allowed_fields={"lead": ["Email"]}),
            dict(allowed_fields={"contact": []}), dict(allowed_fields={"contact": ["Id"]}), dict(allowed_fields={"contact": ["bad field"]}),
            dict(writable_fields={"contact": ["Phone2"]}),
        )
        for overrides in bad:
            values = dict(access_token=TOKEN, instance_url=INSTANCE, allowed_fields=ALLOWED, writable_fields=WRITABLE, transport=fake)
            values.update(overrides)
            token = values.pop("access_token")
            with self.assertRaises(ValueError, msg=str(overrides)):
                salesforce.SalesforceClient(token, **values)
        salesforce.SalesforceClient(TOKEN, instance_url="https://acme.sandbox.my.salesforce.com", allowed_fields=ALLOWED, transport=fake)

    def test_every_request_goes_to_the_one_instance_with_the_token_only_in_the_authorization_header(self):
        client, fake = make()
        client.read({"objectType": "contact", "objectId": CONTACT})
        client.log_interaction(LOG)
        for method, url, headers, payload in fake.calls:
            self.assertTrue(url.startswith(INSTANCE + "/services/data/v60.0/"), url)
            self.assertEqual(headers["Authorization"], f"Bearer {TOKEN}")
            self.assertNotIn(TOKEN, url + json.dumps(payload))

    def test_the_default_transport_only_reaches_a_salesforce_org_host(self):
        client = salesforce.SalesforceClient(TOKEN, instance_url=INSTANCE, allowed_fields=ALLOWED)
        for url in ("http://acme.my.salesforce.com/x", "https://api.example.com/x", "https://acme.my.salesforce.com:444/x",
                    "https://salesforce.com/x"):
            with self.assertRaisesRegex(ValueError, "fixed HTTPS Salesforce host"):
                client._transport("GET", url, {}, None)

    def test_the_executors_cover_exactly_the_registry_tools_and_the_catalog_contract(self):
        client, _ = make()
        entry = REGISTRY["salesforce"]
        self.assertEqual(sorted(client.executors()), sorted(entry["tools"]))
        self.assertTrue(set(entry["tools"]) <= set(CATALOG))
        self.assertEqual((entry["status"], entry["adapter"]), ("in-development", "platform/connectors/salesforce.py"))
        self.assertEqual(entry["scopeByTool"], salesforce.SCOPES)
        self.assertNotIn("proof", entry)
        read = client.executors()["crm.read"]({"objectId": CONTACT})
        self.assertIn("records", read)
        for tool in ("crm.create", "crm.update", "crm.log-interaction"):
            self.assertEqual(CATALOG[tool]["risk"], "write")


if __name__ == "__main__":
    unittest.main()

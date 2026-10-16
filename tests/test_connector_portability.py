"""One Agent and one Skill must work against any provider bound to a capability family (#117).

The CRM adapters (HubSpot, Salesforce) and the file stores (Tier 0 builtin, Google Drive) are driven here with the same
catalog-shaped arguments and their results are checked against the same catalog result contract, so swapping a provider
is a binding change and never an Agent or Skill change.
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from tests import test_google_drive as drive_tests
from tests import test_builtin_files as tier_zero
from tests import test_hubspot as hubspot_tests
from tests import test_salesforce as salesforce_tests

ROOT = pathlib.Path(__file__).resolve().parents[1]
FAMILIES = json.loads((ROOT / "contracts/v1/capabilities.json").read_text())["families"]
TYPES = {"string": str, "object": dict, "array": list}


def conforms(tool: dict, result: dict) -> list[str]:
    schema = tool["result"]
    problems = [f"missing {name}" for name in schema.get("required", []) if name not in result]
    for name, spec in schema.get("properties", {}).items():
        if name in result and not isinstance(result[name], TYPES[spec["type"]]):
            problems.append(f"{name} is not {spec['type']}")
    return problems


def crm_providers():
    hub, _ = hubspot_tests.make()
    sales, _ = salesforce_tests.make()
    return {
        "hubspot": (hub.executors(), "42"),
        "salesforce": (sales.executors(), salesforce_tests.CONTACT),
    }


class CrmPortabilityTests(unittest.TestCase):
    def test_every_provider_answers_every_crm_tool_with_the_catalog_result_shape(self):
        tools = FAMILIES["crm"]["tools"]
        for provider, (executors, contact) in crm_providers().items():
            self.assertEqual(sorted(executors), sorted(tools), provider)
            calls = {
                "crm.read": {"objectType": "contact", "objectId": contact},
                "crm.update": {"objectType": "contact", "objectId": contact, "fields": {"email": "a@customer.example"} if provider == "hubspot"
                               else {"Email": "a@customer.example"}},
                "crm.create": {"objectType": "contact", "fields": {"email": "n@customer.example"} if provider == "hubspot"
                               else {"Email": "n@customer.example"}},
                "crm.log-interaction": {**hubspot_tests.LOG, "objectId": contact},
            }
            for name, arguments in calls.items():
                result = executors[name](arguments)
                self.assertEqual(conforms(tools[name], result), [], (provider, name, result))

    def test_the_portable_read_record_shape_is_the_same_for_every_provider(self):
        for provider, (executors, contact) in crm_providers().items():
            record = executors["crm.read"]({"objectType": "contact", "objectId": contact})["records"][0]
            self.assertEqual(sorted(record), ["externalObjectRef", "fields", "objectId", "objectType"], provider)
            self.assertTrue(record["externalObjectRef"].startswith(f"{provider}:contact/"), provider)

    def test_every_provider_refuses_unlisted_fields_the_same_way_before_a_request(self):
        for provider, (executors, contact) in crm_providers().items():
            with self.assertRaises(ValueError, msg=provider):
                executors["crm.update"]({"objectType": "contact", "objectId": contact, "fields": {"lifecycle": "customer"}})
            with self.assertRaises(ValueError, msg=provider):
                executors["crm.read"]({"objectType": "contact"})

    def test_a_redelivered_interaction_is_a_no_op_with_every_provider(self):
        for provider, (executors, contact) in crm_providers().items():
            first = executors["crm.log-interaction"]({**hubspot_tests.LOG, "objectId": contact})
            again = executors["crm.log-interaction"]({**hubspot_tests.LOG, "objectId": contact})
            self.assertEqual(again["interactionId"], first["interactionId"], provider)
            self.assertTrue(again.get("duplicate"), provider)


class FilesPortabilityTests(unittest.TestCase):
    def test_google_drive_answers_the_files_tools_with_the_catalog_result_shape(self):
        tools = FAMILIES["files"]["tools"]
        client, _ = drive_tests.make()
        executors = client.executors()
        self.assertTrue(set(executors) <= set(tools))
        self.assertNotIn("files.share", executors)  # privileged: stays on the governed path
        found = executors["files.search"]({"query": "price"})
        self.assertEqual(conforms(tools["files.search"], found), [])
        self.assertEqual(conforms(tools["files.read"], executors["files.read"]({"fileId": found["files"][0]["fileId"]})), [])
        written = executors["files.write"]({"name": "Summary", "content": "Booked."})
        self.assertEqual(conforms(tools["files.write"], written), [])

    def test_the_file_summary_a_coworker_sees_has_the_same_core_keys_for_drive_and_the_tier_zero_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = tier_zero.artifacts.ArtifactStore(pathlib.Path(tmp), tenant="acme-prod", audit=lambda *event: None)
            zero = tier_zero.files.Tier0Files(store, agent="receptionist", requested_by="alice@client.example")
            zero.executors()["files.write"]({"name": "Price list", "content": "Haircut $40"})
            zero_file = zero.executors()["files.search"]({"query": "price"})["files"][0]
            zero_read = zero.executors()["files.read"]({"fileId": zero_file["fileId"]})["file"]
        client, _ = drive_tests.make()
        drive_file = client.search({"query": "price"})["files"][0]
        drive_read = client.read({"fileId": drive_file["fileId"]})["file"]
        core = {"fileId", "name", "mediaType", "bytes", "externalObjectRef"}
        for summary in (zero_file, drive_file, zero_read, drive_read):
            self.assertTrue(core <= set(summary), summary)
        for read in (zero_read, drive_read):
            self.assertIn("text", read)


if __name__ == "__main__":
    unittest.main()

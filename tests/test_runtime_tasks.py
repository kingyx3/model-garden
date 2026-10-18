"""The operator command for multi-Agent task graphs: submit, validate, advance, status, timeline, cancel."""
from __future__ import annotations

import contextlib
import http.server
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import threading
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("runtime_tasks_cli", ROOT / "scripts" / "runtime-tasks.py")
cli = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(cli)

ALICE = "alice@client.example"
JORDAN = "jordan@client.example"


class Answers(http.server.BaseHTTPRequestHandler):
    seen: list[str] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        Answers.seen.append(f"{self.path}|{body['messages'][1]['content'][:12]}")
        payload = json.dumps({"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 25}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # noqa: D102
        pass


class RuntimeTasksTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.data = self.root / "data"
        authority = {"agents": {"coordinator": ["crm.read", "docs.create"], "researcher": ["crm.read"], "writer": ["docs.create"]},
                     "principals": {ALICE: ["crm.read", "docs.create"]}}
        self.authority = self.root / "authority.json"
        self.authority.write_text(json.dumps(authority), encoding="utf-8")
        self.spec = self.root / "graph.json"
        self.write_spec({})

    def write_spec(self, changes: dict, nodes: list | None = None) -> None:
        document = {
            "id": "g1", "principal": ALICE, "rootAgent": "coordinator", "goal": "Do the thing", "budget": {"tokens": 100, "steps": 10},
            "nodes": nodes or [
                {"id": "research", "agent": "researcher", "goal": "Look up", "requires": ["crm.read"]},
                {"id": "review", "type": "handoff", "assignee": JORDAN, "ask": "Check it", "dependsOn": ["research"]},
                {"id": "write", "agent": "writer", "goal": "Write", "dependsOn": ["review"], "requires": ["docs.create"]},
            ],
            **changes,
        }
        self.spec.write_text(json.dumps(document), encoding="utf-8")

    def run_cli(self, *argv: str, env: dict | None = None) -> tuple[int, object]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.dict(os.environ, env or {}):
            code = cli.main([str(self.data), *argv])
        text = out.getvalue()
        return code, (json.loads(text) if text.strip() else err.getvalue())

    def test_submit_status_list_timeline(self):
        code, result = self.run_cli("submit", "--spec", str(self.spec), "--authority", str(self.authority))
        self.assertEqual((code, result["status"]), (0, "running"))
        self.assertEqual([node["state"] for node in result["nodes"]], ["pending"] * 3)
        self.assertEqual(self.run_cli("list")[1][0]["graphId"], "g1")
        self.assertEqual(self.run_cli("status", "g1")[1]["rootAgent"], "coordinator")
        actions = [event["action"] for event in self.run_cli("timeline", "g1")[1]]
        self.assertIn("taskgraph.submit", actions)
        self.assertEqual(actions.count("delegation.hop"), 2)

    def test_rejection_and_validate_store_nothing(self):
        self.write_spec({}, nodes=[{"id": "a", "agent": "writer", "goal": "g", "requires": ["crm.read"]}])
        code, message = self.run_cli("submit", "--spec", str(self.spec), "--authority", str(self.authority))
        self.assertEqual(code, 1)
        self.assertIn("crm.read", message)
        self.assertEqual(self.run_cli("list")[1], [])
        ledger = (self.data / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("taskgraph.rejected", ledger)
        self.write_spec({})
        code, result = self.run_cli("validate", "--spec", str(self.spec), "--authority", str(self.authority))
        self.assertEqual((code, result["validated"]), (0, True))
        self.assertEqual(self.run_cli("list")[1], [])
        self.assertNotIn("taskgraph.submit", (self.data / "audit.jsonl").read_text(encoding="utf-8"))

    def test_desired_state_supplies_agent_grants(self):
        states = []
        for name, tools in (("coordinator", ["crm.read"]), ("researcher", ["crm.read"])):
            path = self.root / f"{name}.json"
            path.write_text(json.dumps({"source": {"name": name}, "agent": {"metadata": {"name": name}},
                                        "tools": [{"metadata": {"name": tool}} for tool in tools]}), encoding="utf-8")
            states += ["--desired-state", str(path)]
        permissions = self.root / "people.json"
        permissions.write_text(json.dumps({ALICE: ["crm.read"]}), encoding="utf-8")
        self.write_spec({}, nodes=[{"id": "a", "agent": "researcher", "goal": "g", "requires": ["crm.read"]}])
        code, result = self.run_cli("submit", "--spec", str(self.spec), *states, "--permissions", str(permissions))
        self.assertEqual(code, 0, result)
        self.write_spec({"id": "g2"}, nodes=[{"id": "a", "agent": "researcher", "goal": "g", "requires": ["docs.create"]}])
        self.assertEqual(self.run_cli("submit", "--spec", str(self.spec), *states, "--permissions", str(permissions))[0], 1)

    def test_advance_opens_the_human_step_then_cancel(self):
        self.run_cli("submit", "--spec", str(self.spec), "--authority", str(self.authority))
        server = http.server.HTTPServer(("localhost", 0), Answers)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        Answers.seen.clear()
        url = f"http://localhost:{server.server_port}/{{agent}}"
        env = {"MODEL_GARDEN_COWORKER_API_KEY": "k" * 20, "NO_PROXY": "localhost", "no_proxy": "localhost"}
        code, result = self.run_cli("advance", "g1", "--authority", str(self.authority), "--coworker-url-template", url, env=env)
        self.assertEqual(code, 0)
        self.assertEqual({node["id"]: node["state"] for node in result["nodes"]}, {"research": "succeeded", "review": "waiting", "write": "pending"})
        self.assertEqual(result["spent"], {"tokens": 25, "steps": 1})
        self.assertEqual(Answers.seen, ["/researcher/chat/completions|Look up"])
        pending = list((self.data / "handoffs" / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1)
        self.assertIn(JORDAN, pending[0].read_text(encoding="utf-8"))
        code, result = self.run_cli("cancel", "g1", "--by", ALICE)
        self.assertEqual((code, result["status"]), (1, "cancelled"))
        self.assertEqual(list((self.data / "handoffs" / "pending").glob("*.json")), [])

    def test_failed_graph_tells_the_owner_through_the_worker_queue(self):
        self.write_spec({}, nodes=[{"id": "a", "agent": "researcher", "goal": "g", "requires": ["crm.read"]}])
        self.run_cli("submit", "--spec", str(self.spec), "--authority", str(self.authority))
        # An unreachable coworker fails the node; the owner's notice is queued for the worker's delivery.
        code, result = self.run_cli("advance", "g1", "--authority", str(self.authority), "--coworker-url", "http://localhost:9/v1",
                                    env={"MODEL_GARDEN_COWORKER_API_KEY": "k" * 20, "NO_PROXY": "localhost", "no_proxy": "localhost"})
        self.assertEqual((code, result["status"]), (1, "failed"))
        queue = json.loads((self.data / "worker" / "worker-notices.json").read_text(encoding="utf-8"))
        notice = next(iter(queue["pending"].values()))
        self.assertEqual((notice["kind"], notice["to"]), ("task-graph-failed", ALICE))


if __name__ == "__main__":
    unittest.main()

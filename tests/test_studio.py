"""Credential-free checks for the Employee Studio: change requests (evidence gates, approval, rollback) and the gateway's /studio surface."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


compiler = _load("compile_for_studio", "scripts/compile-workspace.py")
capabilities = _load("capabilities_for_studio", "platform/core/capabilities.py")
policy = _load("policy_for_studio", "platform/core/policy.py")
promotion = _load("promotion_for_studio", "platform/core/knowledge_promotion.py")
schedules_module = _load("schedules_for_studio", "platform/core/schedules.py")
core = _load("change_requests_under_test", "platform/core/change_requests.py")
gateway = _load("gateway_for_studio", "platform/runtime/gateway.py")

NOW = 1_800_000_000
OWNER = "alice@acme.example"
VIEWER = "bob@acme.example"
BINDING = {"environment": "dev", "connectors": {
    "kb": {"provider": "text-knowledge", "tier": 0, "owner": OWNER},
    "crm": {"provider": "built-in-crm", "tier": 0},
}}
ORIGIN = "https://chat.acme.example"
ENV = {"MODEL_GARDEN_WEBCHAT_KEY": "k" * 40}


def desired_state(booking: bool = False) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        ws = pathlib.Path(tmp) / "workspace"
        shutil.copytree(ROOT / "examples/workspace", ws)
        if booking:
            config = ws / "agents/front-office/receptionist/agent.yaml"
            agent = yaml.safe_load(config.read_text())
            agent["spec"]["disabledSkills"] = []
            config.write_text(yaml.safe_dump(agent))
        document = compiler.compile_workspace(ws, "receptionist")[0]
    document["source"]["client"] = "acme"
    return document


def manifest_for(desired: dict) -> dict:
    return capabilities.build_manifest(desired, BINDING)


class Base(unittest.TestCase):
    keys = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.data = self.root / "data"
        self.desired = desired_state()
        self.manifest = manifest_for(self.desired)
        self.owner_key = policy.generate_owner_key()
        self.events = []
        self.now = NOW

    def store(self, *, owner=OWNER, keys="default", catalog=None, manifest="default"):
        if keys == "default":
            keys = {OWNER: self.owner_key["public"]}
        return core.ChangeStore(
            self.data, tenant="acme", desired_state=self.desired, manifest=self.manifest if manifest == "default" else manifest, owner=owner,
            owner_keys=keys, catalog=catalog, audit=lambda action, by, details: self.events.append((action, by, details)),
            clock=lambda: self.now, schedules=self.schedules,
        )

    def schedules(self):
        path = self.data / "schedules"
        return schedules_module.ScheduleStore(path, tenant="acme").list() if path.is_dir() else []

    def make_schedule(self, cron="0 9 * * 1-5"):
        entry = schedules_module.ScheduleStore(self.data / "schedules", tenant="acme").create(
            owner=OWNER, agent="receptionist", goal="Morning summary", cron=cron, budget={"steps": 10}, now=NOW)
        return entry["id"]

    def write_evidence(self, **overrides):
        evidence = promotion.build_evidence(self.desired, suite="receptionist", passed=True, failures=0, now=NOW)
        evidence.update(overrides)
        path = self.data / "studio" / "knowledge-evidence.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(evidence), encoding="utf-8")

    def actions(self):
        return [event[0] for event in self.events]


class ChangeRequestTests(Base):
    def test_propose_changes_nothing_and_is_ledgered(self):
        store = self.store()
        change = store.propose("skill", {"skill": "take-message", "enabled": False}, by=OWNER)
        self.assertEqual(change["state"], "proposed")
        self.assertEqual(change["diff"][0]["after"], ["book-appointment", "take-message"])
        self.assertEqual(store.overlay()["version"], 0)
        self.assertEqual(store.effective_disabled(), ["book-appointment"])
        self.assertEqual(self.actions(), ["studio.change-proposed"])
        self.assertEqual(oct((self.data / "studio" / "changes" / f"{change['id']}.json").stat().st_mode & 0o777), "0o600")

    def test_only_the_named_owner_can_act(self):
        store = self.store()
        with self.assertRaises(core.ChangeRefused) as caught:
            store.propose("skill", {"skill": "take-message", "enabled": False}, by="mallory@evil.example")
        self.assertEqual(caught.exception.reasons, ["not-the-owner"])
        change = store.propose("skill", {"skill": "take-message", "enabled": False}, by=OWNER)
        for action in (lambda: store.approve(change["id"], by=VIEWER, digest=change["digest"]), lambda: store.reject(change["id"], by=VIEWER),
                       lambda: store.rollback(change["id"], by=VIEWER), lambda: store.approve(change["id"], by="", digest=change["digest"])):
            with self.assertRaises(core.ChangeRefused):
                action()
        self.assertEqual(store.get(change["id"])["state"], "proposed")
        self.assertEqual(store.overlay()["version"], 0)

    def test_owner_name_is_matched_without_case_but_never_loosely(self):
        store = self.store()
        change = store.propose("skill", {"skill": "take-message", "enabled": False}, by=OWNER.upper())
        for lookalike in ("alice@acme.example.evil", "xalice@acme.example", "alice@acme.example "):
            if lookalike.strip() == OWNER:
                continue
            with self.assertRaises(core.ChangeRefused):
                store.approve(change["id"], by=lookalike, digest=change["digest"])

    def test_approval_needs_the_exact_digest_then_applies_and_rolls_back(self):
        store = self.store()
        change = store.propose("skill", {"skill": "take-message", "enabled": False}, by=OWNER)
        with self.assertRaises(core.ChangeRefused) as caught:
            store.approve(change["id"], by=OWNER, digest="0" * 64)
        self.assertIn("digest-mismatch", caught.exception.reasons)
        self.assertEqual(store.overlay()["version"], 0)
        applied = store.approve(change["id"], by=OWNER, digest=change["digest"])
        self.assertEqual(applied["state"], "applied")
        self.assertEqual(applied["approval"]["by"], OWNER)
        self.assertEqual(store.effective_disabled(), ["book-appointment", "take-message"])
        self.assertEqual(store.overlay()["version"], 1)
        self.assertEqual([a for a in self.actions() if a != "studio.change-proposed"], ["studio.change-blocked", "studio.change-approved", "studio.change-applied"])
        # approving twice does nothing more
        with self.assertRaises(core.ChangeRefused):
            store.approve(change["id"], by=OWNER, digest=change["digest"])
        rolled = store.rollback(change["id"], by=OWNER)
        self.assertEqual(rolled["state"], "rolled-back")
        self.assertEqual(store.effective_disabled(), ["book-appointment"])
        self.assertEqual(store.overlay()["version"], 2)
        self.assertEqual(self.actions()[-1], "studio.change-rolled-back")
        with self.assertRaises(core.ChangeRefused):
            store.rollback(change["id"], by=OWNER)

    def test_rollback_only_for_the_latest_applied_change(self):
        store = self.store()
        first = store.propose("skill", {"skill": "take-message", "enabled": False}, by=OWNER)
        store.approve(first["id"], by=OWNER, digest=first["digest"])
        second = store.propose("skill", {"skill": "log-conversation", "enabled": False}, by=OWNER)
        store.approve(second["id"], by=OWNER, digest=second["digest"])
        with self.assertRaises(core.ChangeRefused) as caught:
            store.rollback(first["id"], by=OWNER)
        self.assertEqual(caught.exception.reasons, ["later-change-applied"])
        store.rollback(second["id"], by=OWNER)
        store.rollback(first["id"], by=OWNER)
        self.assertEqual(store.effective_disabled(), ["book-appointment"])

    def test_a_change_proposed_on_an_older_base_is_stale(self):
        store = self.store()
        first = store.propose("skill", {"skill": "take-message", "enabled": False}, by=OWNER)
        second = store.propose("skill", {"skill": "log-conversation", "enabled": False}, by=OWNER)
        store.approve(first["id"], by=OWNER, digest=first["digest"])
        with self.assertRaises(core.ChangeRefused) as caught:
            store.approve(second["id"], by=OWNER, digest=second["digest"])
        self.assertIn("stale-base", caught.exception.reasons)

    def test_expired_and_tampered_proposals_are_refused(self):
        store = self.store()
        change = store.propose("skill", {"skill": "take-message", "enabled": False}, by=OWNER)
        self.now += core.PROPOSAL_TTL + 1
        with self.assertRaises(core.ChangeRefused) as caught:
            store.approve(change["id"], by=OWNER, digest=change["digest"])
        self.assertIn("proposal-expired", caught.exception.reasons)
        self.now = NOW
        path = self.data / "studio" / "changes" / f"{change['id']}.json"
        document = json.loads(path.read_text())
        document["diff"][0]["after"] = []  # someone edits the stored diff to switch everything on
        path.write_text(json.dumps(document))
        with self.assertRaises(core.ChangeRefused) as caught:
            store.approve(change["id"], by=OWNER, digest=change["digest"])
        self.assertIn("changed-since-proposed", caught.exception.reasons)
        self.assertEqual(store.overlay()["version"], 0)

    def test_an_interrupted_approval_completes_once(self):
        store = self.store()
        change = store.propose("skill", {"skill": "take-message", "enabled": False}, by=OWNER)
        with mock.patch.object(store, "_finish", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                store.approve(change["id"], by=OWNER, digest=change["digest"])
        self.assertEqual(store.get(change["id"])["state"], "applying")
        self.assertEqual(store.overlay()["version"], 0)
        done = store.approve(change["id"], by=OWNER, digest=change["digest"])
        self.assertEqual(done["state"], "applied")
        self.assertEqual(store.overlay()["version"], 1)
        self.assertEqual(self.actions().count("studio.change-approved"), 1)

    def test_change_ids_cannot_escape_the_folder(self):
        with self.assertRaises(core.ChangeError):
            self.store().get("../overlay")

    def test_no_change_is_refused_as_a_noop(self):
        store = self.store()
        with self.assertRaises(core.ChangeError):
            store.propose("skill", {"skill": "book-appointment", "enabled": False}, by=OWNER)
        with self.assertRaises(core.ChangeError):
            store.propose("skill", {"skill": "no-such-skill", "enabled": False}, by=OWNER)
        with self.assertRaises(core.ChangeError):
            store.propose("teleport", {}, by=OWNER)

    # -- evidence ---------------------------------------------------------------------------------------

    def test_enabling_a_skill_needs_readiness(self):
        store = self.store()
        change = store.propose("skill", {"skill": "book-appointment", "enabled": True}, by=OWNER)
        self.assertFalse(all(item["met"] for item in change["evidence"]))
        with self.assertRaises(core.ChangeRefused) as caught:
            store.approve(change["id"], by=OWNER, digest=change["digest"])
        self.assertIn("evidence:readiness:book-appointment", caught.exception.reasons)
        self.assertEqual(store.effective_disabled(), ["book-appointment"])
        self.assertIn("studio.change-blocked", self.actions())

    def test_unknown_readiness_fails_closed(self):
        store = self.store(manifest=None)
        change = store.propose("skill", {"skill": "book-appointment", "enabled": True}, by=OWNER)
        with self.assertRaises(core.ChangeRefused):
            store.approve(change["id"], by=OWNER, digest=change["digest"])

    def test_reenabling_a_knowledge_skill_needs_evals_for_the_exact_content(self):
        store = self.store()
        off = store.propose("skill", {"skill": "answer-faq", "enabled": False}, by=OWNER)
        store.approve(off["id"], by=OWNER, digest=off["digest"])
        on = store.propose("skill", {"skill": "answer-faq", "enabled": True}, by=OWNER)
        ids = {item["id"]: item["met"] for item in on["evidence"]}
        self.assertTrue(ids["readiness:answer-faq"])
        self.assertFalse(ids["knowledge-evals"])
        with self.assertRaises(core.ChangeRefused) as caught:
            store.approve(on["id"], by=OWNER, digest=on["digest"])
        self.assertEqual(caught.exception.reasons, ["evidence:knowledge-evals"])
        stale = promotion.build_evidence(self.desired, suite="receptionist", passed=True, failures=0, now=NOW)
        for name in stale["knowledge"]:
            stale["knowledge"][name]["sha256"] = "0" * 64  # evals ran against some other text
        self.write_evidence(knowledge=stale["knowledge"])
        with self.assertRaises(core.ChangeRefused):
            store.approve(on["id"], by=OWNER, digest=on["digest"])
        self.write_evidence(passed=False)
        with self.assertRaises(core.ChangeRefused):
            store.approve(on["id"], by=OWNER, digest=on["digest"])
        self.write_evidence()
        done = store.approve(on["id"], by=OWNER, digest=on["digest"])
        self.assertEqual(done["state"], "applied")
        self.assertEqual(store.effective_disabled(), ["book-appointment"])

    def test_promoting_knowledge_records_and_rolls_back(self):
        store = self.store()
        change = store.propose("knowledge", {"action": "promote"}, by=OWNER)
        self.assertTrue(change["diff"])
        with self.assertRaises(core.ChangeRefused):
            store.approve(change["id"], by=OWNER, digest=change["digest"])
        self.write_evidence()
        store.approve(change["id"], by=OWNER, digest=change["digest"])
        record = store.overlay()["knowledgePromotion"]
        self.assertEqual(record["promoted"]["by"], OWNER)
        self.assertEqual(promotion.verify(self.desired, record)["matches"], True)
        with self.assertRaises(core.ChangeError):
            store.propose("knowledge", {"action": "promote"}, by=OWNER)  # nothing left to promote
        store.rollback(change["id"], by=OWNER)
        self.assertIsNone(store.overlay()["knowledgePromotion"])

    def test_schedule_edit_is_validated_and_diffed(self):
        schedule_id = self.make_schedule()
        store = self.store()
        with self.assertRaises(core.ChangeError):
            store.propose("schedule", {"schedule": schedule_id, "cron": "*/5 * * * *"}, by=OWNER)
        with self.assertRaises(core.ChangeError):
            store.propose("schedule", {"schedule": schedule_id, "cron": "not a cron"}, by=OWNER)
        with self.assertRaises(core.ChangeError):
            store.propose("schedule", {"schedule": schedule_id, "budget": {"steps": 0}}, by=OWNER)
        with self.assertRaises(core.ChangeError):
            store.propose("schedule", {"schedule": "s-missing", "cron": "0 8 * * *"}, by=OWNER)
        change = store.propose("schedule", {"schedule": schedule_id, "cron": "30 8 * * 1-5", "budget": {"steps": 25}}, by=OWNER)
        self.assertEqual([d["field"].split("/", 2)[2] for d in change["diff"]], ["cron", "budget/steps"])
        self.assertTrue(all(item["met"] for item in change["evidence"]))
        store.approve(change["id"], by=OWNER, digest=change["digest"])
        self.assertEqual(store.overlay()["schedules"][schedule_id], {"cron": "30 8 * * 1-5", "budget": {"steps": 25}})
        shown = store.coworkers()[0]["schedules"][0]
        self.assertEqual((shown["cron"], shown["pendingWorkspaceChange"]), ("30 8 * * 1-5", True))
        self.assertNotIn("goal", shown)
        store.rollback(change["id"], by=OWNER)
        self.assertEqual(store.coworkers()[0]["schedules"][0]["cron"], "0 9 * * 1-5")

    def test_tightening_policy_needs_no_signoff_but_relaxing_needs_the_owners_signature(self):
        store = self.store()
        deny = store.propose("policy", {"action": "set", "rule": {"id": "deny-creates", "effect": "deny", "actions": ["*.create"]}}, by=OWNER)
        self.assertFalse(deny["params"]["relaxes"])
        self.assertNotIn("signoffSubject", deny)
        store.approve(deny["id"], by=OWNER, digest=deny["digest"])
        self.assertIn("deny-creates", [r["id"] for r in store.effective_rules()])

        rule = {"id": "auto-create", "effect": "allow", "actions": ["crm.create"]}
        relax = store.propose("policy", {"action": "set", "rule": rule}, by=OWNER)
        self.assertTrue(relax["params"]["relaxes"])
        self.assertEqual(relax["signoffDigest"], policy.rule_digest(rule))
        with self.assertRaises(core.ChangeRefused) as caught:
            store.approve(relax["id"], by=OWNER, digest=relax["digest"])
        self.assertIn("evidence:owner-signoff", caught.exception.reasons)
        # a name without a signature, a signature by another key, and a signature over different content all fail
        named = {**rule, "signoff": {"owner": OWNER, "ruleDigest": policy.rule_digest(rule)}}
        other = policy.generate_owner_key()
        wrong_key = policy.sign_rule(rule, OWNER, other["private"])
        other_content = policy.sign_rule({**rule, "actions": ["crm.create", "crm.log-interaction"]}, OWNER, self.owner_key["private"])
        wrong_owner = policy.sign_rule(rule, VIEWER, self.owner_key["private"])
        for attempt in (named, wrong_key, other_content, wrong_owner):
            with self.assertRaises(core.ChangeRefused):
                store.approve(relax["id"], by=OWNER, digest=relax["digest"], attachments={"signoff": attempt})
        self.assertNotIn("auto-create", [r["id"] for r in store.effective_rules()])
        good = policy.sign_rule(rule, OWNER, self.owner_key["private"])
        done = store.approve(relax["id"], by=OWNER, digest=relax["digest"], attachments={"signoff": good})
        self.assertEqual(done["state"], "applied")
        stored = next(r for r in store.effective_rules() if r["id"] == "auto-create")
        self.assertEqual(policy.relaxation_errors([{"spec": {"rules": [stored]}}], self.desired["tools"], [OWNER], {OWNER.lower(): self.owner_key["public"]}), [])
        self.assertEqual(store.export_patch(relax["id"])["policyRule"], stored)
        store.rollback(relax["id"], by=OWNER)
        self.assertNotIn("auto-create", [r["id"] for r in store.effective_rules()])

    def test_relaxing_without_a_configured_owner_key_fails_closed(self):
        store = self.store(keys={})
        rule = {"id": "auto-create", "effect": "allow", "actions": ["crm.create"]}
        relax = store.propose("policy", {"action": "set", "rule": rule}, by=OWNER)
        good = policy.sign_rule(rule, OWNER, self.owner_key["private"])
        with self.assertRaises(core.ChangeRefused):
            store.approve(relax["id"], by=OWNER, digest=relax["digest"], attachments={"signoff": good})

    def test_removing_a_restriction_is_a_relaxation(self):
        store = self.store()
        change = store.propose("policy", {"action": "remove", "ruleId": "approve-external-write"}, by=OWNER)
        self.assertTrue(change["params"]["relaxes"])
        with self.assertRaises(core.ChangeRefused):
            store.approve(change["id"], by=OWNER, digest=change["digest"])
        signed = policy.sign_rule(change["signoffSubject"], OWNER, self.owner_key["private"])
        store.approve(change["id"], by=OWNER, digest=change["digest"], attachments={"signoff": signed})
        self.assertNotIn("approve-external-write", [r["id"] for r in store.effective_rules()])
        self.assertIsNone(store.export_patch(change["id"])["policyRule"])
        self.assertEqual(store.export_patch(change["id"])["removeRule"], "approve-external-write")

    def test_bad_policy_rules_are_refused(self):
        store = self.store()
        for rule in ("x", {"id": "Bad Id", "effect": "allow"}, {"id": "ok", "effect": "maybe"}, {"id": "big", "effect": "deny", "actions": ["a"] * 5000}):
            with self.assertRaises(core.ChangeError):
                store.propose("policy", {"action": "set", "rule": rule}, by=OWNER)

    def test_skillset_members_are_each_checked(self):
        catalog = {"front-desk": {"1.0.0": {"name": "front-desk", "version": "1.0.0", "skills": ["take-message", "book-appointment"]}}}
        store = self.store(catalog=catalog)
        with self.assertRaises(core.ChangeError):
            store.propose("skillset", {"skillset": "missing", "version": "1.0.0", "enabled": True}, by=OWNER)
        on = store.propose("skillset", {"skillset": "front-desk", "version": "1.0.0", "enabled": True}, by=OWNER)
        self.assertEqual(on["diff"][0]["after"], [])
        with self.assertRaises(core.ChangeRefused) as caught:
            store.approve(on["id"], by=OWNER, digest=on["digest"])
        self.assertEqual(caught.exception.reasons, ["evidence:readiness:book-appointment"])  # the bundle grants nothing
        off = store.propose("skillset", {"skillset": "front-desk", "version": "1.0.0", "enabled": False}, by=OWNER)
        store.approve(off["id"], by=OWNER, digest=off["digest"])
        self.assertEqual(store.effective_disabled(), ["book-appointment", "take-message"])

    def test_new_coworker_from_template_with_onboarding_pack(self):
        store = self.store()
        with self.assertRaises(core.ChangeError):
            store.propose("new-coworker", {"name": "receptionist"}, by=OWNER)
        with self.assertRaises(core.ChangeError):
            store.propose("new-coworker", {"name": "Bad Name"}, by=OWNER)
        with self.assertRaises(core.ChangeError):
            store.propose("new-coworker", {"name": "night-desk", "template": "someone-else"}, by=OWNER)
        change = store.propose("new-coworker", {"name": "night-desk", "roleTitle": "Night receptionist"}, by=OWNER)
        self.assertIn("night-desk", change["pack"])
        self.assertIn("book-appointment", change["pack"])
        self.assertEqual(change["params"]["resource"]["metadata"]["name"], "night-desk")
        self.assertIn("book-appointment", change["params"]["resource"]["spec"]["disabledSkills"])
        self.assertTrue(all(item["met"] for item in change["evidence"]))
        self.assertEqual(store.overlay()["coworkers"], {})
        store.approve(change["id"], by=OWNER, digest=change["digest"])
        listed = store.coworkers()
        self.assertEqual([c["name"] for c in listed], ["receptionist", "night-desk"])
        self.assertFalse(listed[1]["deployed"])
        with self.assertRaises(core.ChangeError):
            store.propose("new-coworker", {"name": "night-desk"}, by=OWNER)
        patch = store.export_patch(change["id"])
        self.assertEqual(patch["agentResource"]["metadata"]["name"], "night-desk")
        self.assertEqual(patch["onboardingPack"], change["pack"])
        store.rollback(change["id"], by=OWNER)
        self.assertEqual([c["name"] for c in store.coworkers()], ["receptionist"])

    def test_a_new_coworker_starts_without_the_templates_grants(self):
        """A connected Skill is ready for the template only because of its grant; the new coworker has none, so it starts off."""
        desired = desired_state(booking=True)
        binding = {"environment": "dev", "connectors": {**BINDING["connectors"], "calendar": {"provider": "google", "tier": 1, "token_ref": "secret://calendar/dev"}}}
        real = capabilities._implemented_connector
        with mock.patch.object(capabilities, "_implemented_connector", side_effect=lambda family, provider, tier: True if family == "calendar" else real(family, provider, tier)):
            manifest = capabilities.build_manifest(desired, binding)
        self.desired = desired
        store = self.store(manifest=manifest)
        change = store.propose("new-coworker", {"name": "night-desk"}, by=OWNER)
        self.assertIn("book-appointment", change["params"]["resource"]["spec"]["disabledSkills"])
        self.assertIn("take-message", [s for s in change["params"]["resource"]["spec"]["skills"] if s not in change["params"]["resource"]["spec"]["disabledSkills"]])

    def test_view_lists_skills_tools_grants_blockers_schedules_and_policy(self):
        self.make_schedule()
        view = self.store().coworkers()[0]
        skills = {s["name"]: s for s in view["skills"]}
        self.assertTrue(skills["answer-faq"]["enabled"] and skills["answer-faq"]["ready"])
        self.assertFalse(skills["book-appointment"]["enabled"] or skills["book-appointment"]["ready"])
        self.assertIn("kb.search", view["tools"])
        self.assertIn("book-appointment", [b["skill"] for b in view["blockers"]])
        self.assertTrue(view["grants"])
        self.assertEqual(len(view["schedules"]), 1)
        self.assertIn("approve-external-write", view["policyRules"])
        self.assertNotIn("secret", json.dumps(view).lower())


class Session:
    def __init__(self, token, csrf=None):
        self.token, self.csrf = token, csrf


class StudioHttpTests(Base):
    def setUp(self):
        super().setUp()
        self.mail = []
        self.write_gateway(studio={"owner": OWNER, "ownerKeys": {OWNER: self.owner_key["public"]}})

    def write_gateway(self, studio="default", web=True):
        state = self.root / "desired-state.json"
        state.write_text(json.dumps(self.desired), encoding="utf-8")
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps(self.manifest), encoding="utf-8")
        config = {"publicOrigin": ORIGIN}
        if web:
            config["web"] = {"allowedAddresses": [OWNER, VIEWER], "mail": {"from": "chat@acme.example", "host": "smtp.acme.example"}}
        if studio:
            config["studio"] = studio
        self.gateway = gateway.Gateway(state, self.data, config, {**ENV, "MODEL_GARDEN_CAPABILITY_MANIFEST": str(manifest)},
                                       mail_sender=lambda to, subject, body: self.mail.append((to, body)), clock=lambda: self.now)

    def sign_in(self, address):
        token = self.gateway.auth.issue_login(address)
        return self.gateway.auth.exchange(token)

    def call(self, method, path, session=None, body=None, *, origin=ORIGIN, csrf="auto", content_type="application/json", raw=None):
        headers = {}
        if session:
            headers["Authorization"] = f"Bearer {session}"
        if method == "POST":
            if origin:
                headers["Origin"] = origin
            if csrf == "auto":
                csrf = self.csrf(session) if session else None
            if csrf:
                headers["X-Studio-CSRF"] = csrf
            headers["Content-Type"] = content_type
        payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
        response = self.gateway.handle(method, path, headers, payload)
        try:
            return response.status, json.loads(response.body)
        except ValueError:
            return response.status, response.body

    def csrf(self, session):
        return self.gateway.studio.csrf_token(session)

    def propose(self, session, kind="skill", params=None):
        status, document = self.call("POST", "/studio/api/changes", session, {"kind": kind, "params": params or {"skill": "take-message", "enabled": False}})
        return status, document

    def test_page_assets_have_a_strict_csp_and_no_external_code(self):
        for path, kind in (("/studio", "text/html"), ("/studio/app.js", "text/javascript"), ("/studio/app.css", "text/css")):
            response = self.gateway.handle("GET", path, {}, b"")
            self.assertEqual(response.status, 200)
            self.assertTrue(response.content_type.startswith(kind))
            self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
            self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        page = self.gateway.handle("GET", "/studio", {}, b"").body.decode()
        script = self.gateway.handle("GET", "/studio/app.js", {}, b"").body.decode()
        for text in (page, script):
            self.assertNotIn("http://", text)
            self.assertNotRegex(text, r"https?://(?!chat)")
        self.assertNotRegex(page, r"<script(?![^>]*src=)")
        self.assertNotIn(" style=", page)
        self.assertNotRegex(page, r"\son\w+=")
        for banned in ("innerHTML", "outerHTML", "eval(", "document.write", "insertAdjacentHTML", "localStorage"):
            self.assertNotIn(banned, script)
        self.assertNotIn("<script src=\"http", page)

    def test_api_needs_a_verified_session(self):
        self.assertEqual(self.call("GET", "/studio/api/state")[0], 401)
        self.assertEqual(self.call("GET", "/studio/api/state", "forged.token")[0], 401)
        self.assertEqual(self.call("POST", "/studio/api/changes", None, {"kind": "skill"}, csrf="x")[0], 401)
        self.assertEqual(self.call("GET", "/studio/api/changes/cr-000000000000")[0], 401)

    def test_everyone_on_the_list_can_read_and_only_the_owner_is_the_owner(self):
        owner, viewer = self.sign_in(OWNER), self.sign_in(VIEWER)
        status, mine = self.call("GET", "/studio/api/state", owner)
        self.assertEqual((status, mine["role"], mine["owner"]), (200, "owner", OWNER))
        status, theirs = self.call("GET", "/studio/api/state", viewer)
        self.assertEqual((status, theirs["role"]), (200, "viewer"))
        self.assertEqual(theirs["coworkers"][0]["name"], "receptionist")
        self.assertNotEqual(mine["csrf"], theirs["csrf"])
        self.assertEqual(mine["csrf"], self.csrf(owner))

    def test_viewers_cannot_change_anything_even_with_a_valid_token(self):
        viewer = self.sign_in(VIEWER)
        status, document = self.propose(viewer)
        self.assertEqual(status, 403)
        self.assertIn("Only the owner", document["error"])
        self.assertEqual(self.gateway.studio._store().list(), [])
        owner = self.sign_in(OWNER)
        _, created = self.propose(owner)
        change = created["change"]
        for action, body in (("approve", {"digest": change["digest"]}), ("reject", {}), ("rollback", {})):
            self.assertEqual(self.call("POST", f"/studio/api/changes/{change['id']}/{action}", viewer, body)[0], 403)
        self.assertEqual(self.gateway.studio._store().get(change["id"])["state"], "proposed")

    def test_viewers_can_read_a_change_request_with_its_evidence(self):
        owner, viewer = self.sign_in(OWNER), self.sign_in(VIEWER)
        _, created = self.propose(owner, "skill", {"skill": "book-appointment", "enabled": True})
        status, document = self.call("GET", f"/studio/api/changes/{created['change']['id']}", viewer)
        self.assertEqual(status, 200)
        self.assertNotIn("rollback", document["change"])
        self.assertFalse(all(i["met"] for i in document["change"]["liveEvidence"]))

    def test_csrf_and_origin_are_enforced_on_every_state_change(self):
        owner, viewer = self.sign_in(OWNER), self.sign_in(VIEWER)
        body = {"kind": "skill", "params": {"skill": "take-message", "enabled": False}}
        attempts = [
            dict(origin=None), dict(origin="https://evil.example"), dict(origin=ORIGIN + ".evil.example"), dict(csrf=None), dict(csrf="0" * 48),
            dict(csrf=self.csrf(viewer)), dict(csrf=self.csrf(owner) + "0"),
        ]
        for extra in attempts:
            self.assertEqual(self.call("POST", "/studio/api/changes", owner, body, **extra)[0], 403, extra)
        self.assertEqual(self.call("POST", "/studio/api/changes", owner, body, content_type="text/plain")[0], 415)
        self.assertEqual(self.call("POST", "/studio/api/changes", owner, raw=b"x" * (studio_max() + 1))[0], 413)
        self.assertEqual(self.call("POST", "/studio/api/changes", owner, raw=b"{not json")[0], 400)
        self.assertEqual(self.call("POST", "/studio/api/changes", owner, raw=b"[1]")[0], 400)
        self.assertEqual(self.gateway.studio._store().list(), [])
        self.assertEqual(self.call("POST", "/studio/api/changes", owner, body)[0], 200)

    def test_csrf_failures_do_not_reveal_or_touch_state_for_approve_either(self):
        owner = self.sign_in(OWNER)
        _, created = self.propose(owner)
        change = created["change"]
        status, _ = self.call("POST", f"/studio/api/changes/{change['id']}/approve", owner, {"digest": change["digest"]}, origin="https://evil.example")
        self.assertEqual(status, 403)
        self.assertEqual(self.gateway.studio._store().get(change["id"])["state"], "proposed")

    def test_owner_approves_the_exact_digest_then_rolls_back(self):
        owner = self.sign_in(OWNER)
        status, created = self.propose(owner)
        self.assertEqual(status, 200)
        change = created["change"]
        self.assertEqual(change["state"], "proposed")
        url = f"/studio/api/changes/{change['id']}/approve"
        status, document = self.call("POST", url, owner, {"digest": "f" * 64})
        self.assertEqual((status, "digest-mismatch" in document["reasons"]), (409, True))
        status, document = self.call("POST", url, owner, {})
        self.assertEqual(status, 409)
        status, document = self.call("POST", url, owner, {"digest": change["digest"]})
        self.assertEqual((status, document["change"]["state"]), (200, "applied"))
        self.assertNotIn("rollback", document["change"])
        state = self.call("GET", "/studio/api/state", owner)[1]
        skills = {s["name"]: s for s in state["coworkers"][0]["skills"]}
        self.assertFalse(skills["take-message"]["enabled"])
        actions = [a["action"] for a in state["activity"]]
        self.assertIn("studio.change-applied", actions)
        self.assertIn("studio.change-approved", actions)
        status, document = self.call("POST", f"/studio/api/changes/{change['id']}/rollback", owner, {})
        self.assertEqual((status, document["change"]["state"]), (200, "rolled-back"))
        skills = {s["name"]: s for s in self.call("GET", "/studio/api/state", owner)[1]["coworkers"][0]["skills"]}
        self.assertTrue(skills["take-message"]["enabled"])
        ledger = [json.loads(line) for line in (self.data / "audit.jsonl").read_text().splitlines()]
        studio = [e["action"] for e in ledger if str(e.get("action", "")).startswith("studio.")]
        self.assertEqual(studio, ["studio.change-proposed", "studio.change-blocked", "studio.change-blocked", "studio.change-approved", "studio.change-applied", "studio.change-rolled-back"])
        self.assertEqual(self.gateway.studio._store().overlay()["version"], 2)

    def test_evidence_blocks_approval_over_http(self):
        owner = self.sign_in(OWNER)
        _, created = self.propose(owner, "skill", {"skill": "book-appointment", "enabled": True})
        change = created["change"]
        status, document = self.call("POST", f"/studio/api/changes/{change['id']}/approve", owner, {"digest": change["digest"]})
        self.assertEqual(status, 409)
        self.assertIn("evidence:readiness:book-appointment", document["reasons"])
        self.assertEqual(self.gateway.studio._store().overlay()["version"], 0)

    def test_relaxing_policy_over_http_needs_the_signed_document(self):
        owner = self.sign_in(OWNER)
        rule = {"id": "auto-create", "effect": "allow", "actions": ["crm.create"]}
        _, created = self.propose(owner, "policy", {"action": "set", "rule": rule})
        change = created["change"]
        self.assertEqual(change["signoffDigest"], policy.rule_digest(rule))
        url = f"/studio/api/changes/{change['id']}/approve"
        self.assertEqual(self.call("POST", url, owner, {"digest": change["digest"]})[0], 409)
        self.assertEqual(self.call("POST", url, owner, {"digest": change["digest"], "signoff": {**rule, "signoff": {"owner": OWNER, "ruleDigest": policy.rule_digest(rule)}}})[0], 409)
        signed = policy.sign_rule(rule, OWNER, self.owner_key["private"])
        status, document = self.call("POST", url, owner, {"digest": change["digest"], "signoff": signed})
        self.assertEqual((status, document["change"]["state"]), (200, "applied"))

    def test_schedule_and_new_coworker_over_http(self):
        schedule_id = self.make_schedule()
        owner = self.sign_in(OWNER)
        status, created = self.propose(owner, "schedule", {"schedule": schedule_id, "cron": "0 10 * * *"})
        self.assertEqual(status, 200)
        status, document = self.propose(owner, "schedule", {"schedule": schedule_id, "cron": "* * * * *"})
        self.assertEqual(status, 400)
        status, created = self.propose(owner, "new-coworker", {"name": "night-desk", "roleTitle": "Night desk"})
        self.assertEqual(status, 200)
        self.assertIn("night-desk", created["change"]["pack"])
        self.call("POST", f"/studio/api/changes/{created['change']['id']}/approve", owner, {"digest": created["change"]["digest"]})
        names = [c["name"] for c in self.call("GET", "/studio/api/state", owner)[1]["coworkers"]]
        self.assertEqual(names, ["receptionist", "night-desk"])

    def test_unknown_and_malformed_routes(self):
        owner = self.sign_in(OWNER)
        self.assertEqual(self.call("GET", "/studio/api/changes/cr-zzzz", owner)[0], 404)
        self.assertEqual(self.call("GET", "/studio/api/changes/cr-000000000000", owner)[0], 404)
        self.assertEqual(self.call("POST", "/studio/api/changes/cr-000000000000/approve", owner, {"digest": "x"})[0], 400)
        self.assertEqual(self.call("POST", "/studio/api/changes/../../etc/approve", owner, {})[0], 404)
        self.assertEqual(self.call("GET", "/studio/api/nothing", owner)[0], 404)
        self.assertEqual(self.call("POST", "/studio/api/changes", owner, {"kind": "teleport", "params": {}})[0], 400)

    def test_the_studio_is_off_unless_configured_and_needs_web_sign_in(self):
        self.write_gateway(studio=None)
        self.assertEqual(self.gateway.handle("GET", "/studio", {}, b"").status, 404)
        self.assertEqual(self.gateway.handle("GET", "/studio/app.js", {}, b"").status, 404)
        with self.assertRaises(gateway.GatewayConfigError):
            self.write_gateway(studio={"owner": OWNER}, web=False)
        for bad in ({"owner": "not a person"}, {"owner": OWNER, "extra": 1}, {"owner": OWNER, "ownerKeys": {OWNER: "nope"}}, {"ownerKeys": {}}):
            with self.assertRaises(gateway.GatewayConfigError):
                self.write_gateway(studio=bad)

    def test_sign_in_link_can_return_to_the_studio(self):
        self.gateway.handle("POST", "/channels/web/login", {}, json.dumps({"address": OWNER, "studio": True}).encode())
        self.assertIn("https://chat.acme.example/studio#login=", self.mail[-1][1])
        self.gateway.handle("POST", "/channels/web/login", {}, json.dumps({"address": OWNER}).encode())
        self.assertIn("/chat#login=", self.mail[-1][1])


def studio_max() -> int:
    return _load("studio_limits", "platform/runtime/studio.py").MAX_BODY


class OperatorCommandTests(Base):
    def setUp(self):
        super().setUp()
        self.root.joinpath("desired-state.json").write_text(json.dumps(self.desired), encoding="utf-8")
        self.root.joinpath("manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        self.root.joinpath("gateway.json").write_text(json.dumps({"studio": {"owner": OWNER, "ownerKeys": {OWNER: self.owner_key["public"]}}}), encoding="utf-8")
        self.key_file = self.root / "owner.key"
        self.key_file.write_text(self.owner_key["private"], encoding="utf-8")

    def run_cli(self, *args, ok=True):
        done = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "studio-changes.py"), "--desired-state", str(self.root / "desired-state.json"),
             "--data-root", str(self.data), "--config", str(self.root / "gateway.json"), "--manifest", str(self.root / "manifest.json"), *args],
            capture_output=True, text=True)
        if ok:
            self.assertEqual(done.returncode, 0, done.stderr)
        else:
            self.assertEqual(done.returncode, 1, done.stdout)
        return done

    def test_list_approve_rollback_and_export_from_the_host(self):
        created = json.loads(self.run_cli("propose", "skill", "--params", '{"skill": "take-message", "enabled": false}', "--by", OWNER).stdout)
        listed = json.loads(self.run_cli("list").stdout)
        self.assertEqual([c["id"] for c in listed], [created["id"]])
        shown = json.loads(self.run_cli("show", created["id"]).stdout)
        self.assertIn("liveEvidence", shown)
        wrong_person = self.run_cli("approve", created["id"], "--by", VIEWER, "--digest", created["digest"], ok=False)
        self.assertIn("not-the-owner", wrong_person.stderr)
        wrong_digest = self.run_cli("approve", created["id"], "--by", OWNER, "--digest", "1" * 64, ok=False)
        self.assertIn("digest-mismatch", wrong_digest.stderr)
        self.run_cli("export", created["id"], ok=False)
        applied = json.loads(self.run_cli("approve", created["id"], "--by", OWNER, "--digest", created["digest"]).stdout)
        self.assertEqual(applied["state"], "applied")
        self.assertNotIn("rollback", applied)
        patch = json.loads(self.run_cli("export", created["id"]).stdout)
        self.assertEqual(patch["agentSpec"]["disabledSkills"], ["book-appointment", "take-message"])
        ledger = [json.loads(line)["action"] for line in (self.data / "audit.jsonl").read_text().splitlines()]
        self.assertEqual(ledger[-2:], ["studio.change-approved", "studio.change-applied"])
        self.run_cli("rollback", created["id"], "--by", OWNER)
        self.assertEqual(json.loads(self.run_cli("list").stdout)[0]["state"], "rolled-back")

    def test_signoff_command_makes_a_relaxing_change_approvable(self):
        rule = '{"id": "auto-create", "effect": "allow", "actions": ["crm.create"]}'
        created = json.loads(self.run_cli("propose", "policy", "--params", '{"action": "set", "rule": %s}' % rule, "--by", OWNER).stdout)
        self.run_cli("approve", created["id"], "--by", OWNER, "--digest", created["digest"], ok=False)
        signed = self.run_cli("signoff", created["id"], "--owner", OWNER, "--key-file", str(self.key_file)).stdout
        signed_file = self.root / "signed.json"
        signed_file.write_text(signed, encoding="utf-8")
        applied = json.loads(self.run_cli("approve", created["id"], "--by", OWNER, "--digest", created["digest"], "--signoff-file", str(signed_file)).stdout)
        self.assertEqual(applied["state"], "applied")
        plain = json.loads(self.run_cli("propose", "skill", "--params", '{"skill": "take-message", "enabled": false}', "--by", OWNER).stdout)
        self.run_cli("signoff", plain["id"], "--owner", OWNER, "--key-file", str(self.key_file), ok=False)

    def test_a_host_approval_is_visible_to_the_gateway(self):
        created = json.loads(self.run_cli("propose", "skill", "--params", '{"skill": "take-message", "enabled": false}', "--by", OWNER).stdout)
        self.run_cli("approve", created["id"], "--by", OWNER, "--digest", created["digest"])
        self.assertEqual(self.store().effective_disabled(), ["book-appointment", "take-message"])


if __name__ == "__main__":
    unittest.main()

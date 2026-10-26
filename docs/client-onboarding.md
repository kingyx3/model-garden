# Client onboarding

This page owns the **repository-side operator entry point** for a normal Model Garden client. It deliberately does not repeat cloud-bootstrap internals, secret-storage rules, billing integration details or business/commercial handoff steps.

- Confluence owns the engagement lifecycle and repeatable founder/operator checklist.
- [`cloud-bootstrap.md`](cloud-bootstrap.md) owns the keyless infrastructure bootstrap contract.
- [`secrets.md`](secrets.md) owns credential/account ownership and secret handling.
- [`architecture.md`](architecture.md) owns implementation boundaries.

Model Garden platform code stays in this repository. Create one separate private workspace repository per client and keep it limited to client desired state, version pins, environment configuration and evals. Do not fork Model Garden per client.

## Standard managed path

For early SMB clients, use one resumable command from a **published** Model Garden release:

```bash
python3 scripts/launch.py acme \
  --bootstrap-credential ~/Downloads/acme-bootstrap.json
```

`scripts/launch.py` is the public operator entry point. By default it derives the target GCP project from the temporary JSON's `project_id`, derives `<authenticated-github-user>/acme-ai-workspace`, and uses `--ownership model-garden`.

Override only a target that genuinely differs:

```bash
python3 scripts/launch.py acme \
  --bootstrap-credential ~/Downloads/acme-bootstrap.json \
  --github-repo client-org/acme-ai-workspace \
  --gcp-project client-target-project \
  --ownership client
```

Ownership changes who controls the account/project and break-glass administration; it does not change the workspace/runtime contract.

## What the launch flow owns

The standard launch flow must derive or automate repeatable technical work rather than turn it into a per-client checklist. It creates/reuses the private client workspace and repository, configures protected DEV/PROD runtime secret maps, establishes keyless GCP trust/state from one temporary local credential, installs the keyless client workflow, reconciles and verifies DEV, records bootstrap verification, revokes/deletes the temporary key where local tooling permits, and runs environment doctor checks.

The flow is resumable. Reruns reuse completed workspace, secret, cloud and deployment state and fail closed if the existing client/repository/cloud identity conflicts with the requested target.

Exact bootstrap resources, Terraform ownership and GitHub OIDC mechanics live in [`cloud-bootstrap.md`](cloud-bootstrap.md), not here.

## Unavoidable operator/client inputs

Only information Model Garden cannot safely invent or derive should require input:

- client slug and the bounded employee/workflow being deployed;
- one temporary bootstrap service-account JSON for the target GCP project on first bootstrap;
- a repository/project/ownership override only when the inferred default is not the intended target;
- runtime/integration credentials actually referenced by the selected environment, plus provider authorization and a sustainable renewal/revocation path;
- client-owned OAuth/account consent for client business systems (the owner signs in and consents at the provider through the gateway's `/connections/oauth/start` link; the operator opens it with `runtime-connection.py oauth open`, see [`secrets.md`](secrets.md#provider-oauth-consent-google-microsoft-oidcoauth); not yet live-proven with a real tenant);
- business-approved Agent/Skill/Knowledge/eval content and human handoff rules;
- production business approval before live traffic.

Credential ownership follows the target account/data/commercial relationship. For the managed no-IT path, Model Garden may own the cloud/model/voice provider account; production authorization into Google Workspace, Microsoft 365, CRM, calendar, booking or another client business system remains client-authorized. See [`secrets.md`](secrets.md).

## Connect the client's business stack

- **Google Workspace or Microsoft 365 client:** use a verified third-party/vendor-maintained MCP server or integration platform, approved for that client's exact calendar/mail/files operations. Model Garden deliberately does not maintain native Google/Microsoft API clients. The governed upstream route and owner OAuth consent are built ([`secrets.md`](secrets.md#provider-oauth-consent-google-microsoft-oidcoauth)) but not yet proven with a real Google or Microsoft tenant (#157–#159). Do not activate Calendar Skills until that evidence exists. Do not send credentials into Agent/Skill content.

The upstream-only integration status and limitations live in [`governed-tools.md`](governed-tools.md). Broader business/provider intent is owned by the Google Workspace and Microsoft 365 Confluence integration guides.

## Business configuration is separate from deployment

The one-command bootstrap removes technical choreography; it does not guess business behaviour. Configure the initial Agent's role/instructions, selected curated Skills, approved Tools, authoritative Knowledge references, human handoff rules and representative evals from discovery.

Receptionist is the reference wedge, not a mandatory platform shape. A different bounded role normally changes only the client workspace:

```bash
python3 scripts/launch.py acme \
  --bootstrap-credential ~/Downloads/acme-bootstrap.json \
  --agent operations-coordinator \
  --role-title "Operations Coordinator"
```

Change Model Garden core only when a reusable Skill, provider-neutral governance capability or runtime seam is genuinely missing. Integrations must use maintained upstream providers, not new bespoke SaaS clients.

## Business-owner Skill selection and activation gate

Before activating or modifying a Skill, run `python3 scripts/client-operator.py skills <private-client-workspace> --environment dev --agent <name>`. Show the owner the dependency state and next `connect <family>` step. Keep a Skill listed in the Agent's `spec.skills` but inactive with `spec.disabledSkills: [skill-name]` while its permission or data dependency is missing. Re-enable only after connection and explicit DEV validation; it cannot compile with an unbound required capability. The coworker's capability manifest reflects this state, including a runtime failure/revocation of a previously working connection. A shared `Skillset` groups versioned Skills but does not grant access; enable members independently and review each member's readiness. See [#128](https://github.com/kingyx3/model-garden/issues/128). A minimal, evidence-gated Employee Studio now exists (see [Employee Studio and change requests](#employee-studio-and-change-requests)); it is not yet a full no-code Agent builder ([#130](https://github.com/kingyx3/model-garden/issues/130)).

Tier 0 knowledge readiness is based on **readable, nonempty, parsed business-rule content**, not merely an approved `KnowledgeSource` label or a `kb` provider binding.

The operator `skills`/onboarding report marks a missing, empty or invalid approved file as blocked and directs the owner to repair or upload the source before enabling the dependent Skill. The compiled capability manifest rechecks the materialized content digest and section count; an absent or mismatched payload does not count as approved knowledge. These are configuration/readiness checks, **not** a live customer-answer or source-truth attestation.

For a customer-facing Skill, declare `spec.knowledgeAccess: public` (as the reference `answer-faq` does). Readiness then requires at least one approved, effective, materialized source the Tier 0 retriever can serve to a public caller (`public` or `internal` classification); confidential team-only material cannot make that Skill ready. `member` is the compatibility default for internal Skills, and the runtime still independently enforces caller identity, Agent audience and document classification. This is a conservative activation signal, **not** Skill-scoped retrieval ACL, provider-side document permission proof, or a live customer-answer test. The original #111/#116 live and Skill-specific audience gates remain open.

**Grant-registry recovery:** once an operator initializes a per-coworker provider-grant registry, its private lock file witnesses that setup. If the grant data file is deleted, replaced by a directory, or becomes an unsafe link, runtime Skill readiness now blocks connected capabilities rather than silently reverting to legacy environment-level access. Restore a verified tenant-matched grant registry through the authorized operator recovery process; never delete the lock witness to bypass this safety check. This is a host-local readiness safeguard, not provider-side identity or revocation proof. A legacy workspace that never initialized grants remains compatible. See [#111](https://github.com/kingyx3/model-garden/issues/111) and [#129](https://github.com/kingyx3/model-garden/issues/129).

Before enabling a Skill that requires a provider, verify the integration's status
in the canonical registry. Merely listing `connectors.crm.provider: hubspot`
or `connectors.kb.provider: confluence-knowledge` is not sufficient until the approved upstream route
is implemented; the readiness and deployment gates reject such claims. The Tier 0
`connectors.kb: {provider: text-knowledge, tier: 0}` and
`connectors.crm: {provider: built-in-crm, tier: 0}` bindings are shipped and need no
credentials: an approved business-rules file (below) and a person who reviews the
contact log (`client-operator.py contacts review`; it prints personal data, so run it on
the client host and agree who may). Voice calls reach that log when someone runs
`client-operator.py file-calls --project-name <env>` (see [channels](channels.md#filing-finished-conversations));
it is not automatic yet.
The reference receptionist now depends on these two Tier 0 bindings; a client with no
calendar keeps `book-appointment` under `disabledSkills` and needs nothing else (see
[contracts](contracts.md#reference-receptionist-on-a-zero-stack-client)).
Clients can continue to list those Skills under `disabledSkills` until their
dependencies are implemented and authorized. A planned upstream Calendar route requires client-owner consent, separately scoped coworker credentials, approved gateway execution and real-tenant acceptance before activation.

### Hand-offs and business rules

Name who performs actions the coworker cannot (set `MODEL_GARDEN_HANDOFF_ASSIGNEE` for the governed sidecar, or give a `human-only` policy rule its own `assignee`) and show them `python3 scripts/client-operator.py handoffs --project-name <env>`; they close each item with `handoff-done` or `handoff-decline --by <their identity>`. For a client with no knowledge system, have the owner write one `.md` or `.txt` of business rules, run `python3 scripts/knowledge-check.py rules.md --query "<a real customer question>"`, fix the warnings, and only then set the source's `status: approved`, select the `kb.search` Tool for the Agent and bind `connectors.kb` to `text-knowledge` at tier 0 (see [Tier 0 business-rules knowledge](contracts.md#tier-0-business-rules-knowledge)). `python3 scripts/client-operator.py connect kb --provider text-knowledge --tools kb.search --owner <owner> --operator <you>` prints the non-secret plan. No live deployment has exercised this path yet.

### Approval policy walkthrough

Run `python3 scripts/compile-workspace.py <private-client-workspace> --agent <name> --approvals` and walk the client owner through which actions run automatically, which need approval and which are denied for callers or senders who are not staff. Any change that relaxes an approval is an `allow` rule that needs the named client owner's sign-off digest before it compiles (see [Client approval policy](contracts.md#client-approval-policy)); record the owner's agreement outside the repository as well, and keep the sign-off lines under the client's `CODEOWNERS`.

## Client integration discovery and permission preflight

Use the canonical [integration registry and guide](contracts.md#integration-registry-and-readiness-to-connect-boundary) to show a client which CRM, mailbox, calendar and knowledge providers are implemented versus planned. `python3 scripts/client-operator.py connect <family>` lists provider choices by tier and maturity. Add `--provider`, `--tools`, `--owner` and `--operator` for a non-secret permission preview; it does not grant access or change credentials. For incomplete connectors and expired authorization, fail closed and keep live client provisioning manual until the corresponding implementation and real-provider proof gates are met. Do not create a second AI coworker against the same external service by copying the first one's grant. Current connection configuration is environment-scoped; independently approved, revocable **per-coworker** provider authentication is a prerequisite to safe multi-Agent autonomous operation ([#129](https://github.com/kingyx3/model-garden/issues/129), [#86](https://github.com/kingyx3/model-garden/issues/86)).

**Customer-added integrations:** An operator can now stage and review a tenant-owned remote MCP connection or narrow custom REST operation, issue an owner-only credential enrollment link, configure a provider-neutral OAuth consent route and activate exact approved Tools for one coworker. See [governed execution](governed-tools.md), [secret and consent procedures](secrets.md), and the [client experience target](https://modelgarden.atlassian.net/wiki/spaces/BA/pages/327715). This is **operator-assisted, credential-free-tested plumbing**, not a customer-facing Add Custom MCP/API wizard or a proven live SaaS integration; #159, #129 and #86 remain open. Never request tokens or API keys in chat, issues, Knowledge, workspace Git or prompts. Keep a Tool disabled until its actual grant, consent, upstream health and tenant proof have been checked.

## Normal delivery after bootstrap

Once bootstrap is verified, GitHub is the engineering control plane:

```text
pull request
  -> validate only

merge to dev
  -> keyless DEV deploy
  -> evals / pilot / approval

promote tested revision to main
  -> keyless PROD deploy
  -> production smoke / acceptance
```

No long-lived cloud deployment JSON is needed after bootstrap. Platform releases do not silently mutate clients. Upgrade a client by changing `platform.lock.yaml` in a pull request, validating/evaluating in DEV, then promoting the same tested revision to PROD.

## Lower-level paths are implementation details

`scripts/launch.py` delegates to `scripts/launch-client.py`, which composes `scripts/client-operator.py` and `scripts/bootstrap-cloud.py`. Run those lower-level commands directly only for diagnostics, automation internals or an intentional non-default environment.

Keyless cloud deployment is the only supported deployment path; there is no self-hosted runner option.

## Client/platform ownership boundary

Platform-owned here: contracts, compiler, Hermes adapter, governed actions, reusable Skills, provider-neutral Tool governance and channel adapters, keyless cloud bootstrap, Docker target adapter, usage metering and the Lago billing sync, operator helpers and shared CI.

Client-specific desired state in the separate private workspace: `modelgarden.yaml`, `platform.lock.yaml`, `agents/`, genuinely client-specific `skills/`, Knowledge references, `evals/`, and environment-specific non-secret configuration.

For the business-side lifecycle, deployment checklist and production handover, use the canonical Confluence Model Garden documentation rather than recreating those procedures here.

## Owner-readable onboarding pack

`python3 scripts/client-operator.py onboarding-pack <private-client-workspace> --environment dev --agent <name> [--output pack.md]` writes the one page a business owner can read, keep and hand to their consultant (#111, #114). It rewords two reports the compiler already produces: which Skills work, which need one thing from the owner first (what is missing, the single action that unblocks it, the `connect <family>` command and the provider options, with a planned provider shown as not usable yet) and which are switched off on purpose; and, for each action, whether it runs by itself, waits for approval, runs by itself only inside the signed limits, is handed to a named person or is never done, for team members and for public callers. It lists names, families, tiers, risk labels and commands only: never a secret reference, connection id, scope, account or policy rule, and it refuses to render anything that looks like a credential. It describes what the *configuration* allows; it does not show that any outside account is connected or that a live call has worked. The guided connect step it points to (#110) and Skillset activation (#128) are still separate work.

## Previewing a connection before it is made

`python3 scripts/client-operator.py readiness-change <private-client-workspace> <family> --provider <id> --tier <n> [--scopes a,b] [--owner <person>] [--replace] [--rescope] --agent <name>` shows what a connection would do to each Skill before anything is written (#110, #111): which Skills start working, which stay blocked with the one next step, which stay switched off until the owner enables them, and, first, a warning for anything that works today and would stop. It merges the proposed provider into an in-memory copy of the environment binding with the same rules as the guided connect step (replacing a provider and widening scopes or changing the owner must be explicit) and re-runs the readiness rules, so a provider that is not shipped, or a coworker without its own grant, correctly leaves a Skill blocked. It exits non-zero if the change would break something. It writes no binding, stores no credential and does not connect anything.

## Employee Studio and change requests

The channel gateway serves a small Studio at `/studio` when the EnvironmentBinding has `gateway.studio: {owner: "<owner's web sign-in address>", owner_keys: {"<owner>": "<base64 Ed25519 public key>"}, people: {"<colleague>": ["author"]}}` (rendered into the gateway configuration as `studio: {owner, ownerKeys, people}`; it needs `web` sign-in and `public_origin`; every value is public, and `owner_keys` and `people` are optional). The deploy ships the Studio code with the runtime image, so this is all an operator configures. Anyone on the web allow-list can **read** it after the usual magic-link sign-in (`/studio` offers the same form; the link returns there): the coworker's Skills (on/off, ready/blocked and the one next step), Tools, the access (grants) each connection gives it, readiness blockers, schedules, recent ledger activity and every change request. Only the **named owner** can approve anything, and only the owner or a colleague holding the `author` role can propose a change.

**Colleagues and roles.** The owner gives a signed-in colleague roles, or takes them away, with an `access` change request (`{person, action: grant|revoke, roles}`), approved by digest and rolled back like any other: `author` (propose changes; never approve, reject or roll back, and never an `access` change), `approver` (approve the coworker's actions in chat), `provisioner` (start provider connections), `data-viewer` and `promoter`. The owner holds every role and their own access is never changed here; a role is only given to someone on the web sign-in list, and a role never carries a credential. Only `author` is enforced by the Studio itself today; the others are recorded and exported in the workspace patch (`access`: person, roles and whether `approver` belongs in the gateway directory) for the operator to merge. The roles in place at start come from `studio.people`; the Studio lists everyone who holds one. Code: `platform/runtime/studio.py` (HTTP), `platform/core/change_requests.py` (decisions), `scripts/studio-changes.py` (host).

**Changes are change requests, never direct edits.** The owner (or an author) proposes one of: switch a Skill or a Skillset's members on or off, edit a timed schedule's cron or budget, set or remove an approval Policy rule, promote changed business knowledge, create a new coworker from the deployed one, or (owner only) change a colleague's roles. That writes a JSON document under `<data root>/studio/changes` with the exact diff, a digest of it and an **evidence checklist**, and ledgers `studio.change-proposed`. Nothing changes until the owner makes an explicit approve call naming that digest, and every required item is met *at that moment* (it is recomputed, not trusted from the proposal):

| Change | Evidence required |
| --- | --- |
| Enable a Skill / Skillset member | its readiness is met, including the coworker's own grant (`grant_readiness`); a Skillset grants nothing, so each member is checked; a knowledge-reading Skill also needs `knowledge_promotion` evals that ran against the exact text (`<data root>/studio/knowledge-evidence.json`, written by `eval-receptionist.py --evidence`) |
| Switch off | none; it only reduces access |
| Schedule edit | the cron fires and respects the 15-minute floor, the budget is well formed, and the base is unchanged |
| Policy rule that tightens | the resulting Policy passes the compiler's rules |
| Policy rule that relaxes (an `allow`, or removing a restriction) | the owner's Ed25519 signature over the exact rule, verified against `studio.ownerKeys`; with no key configured, or no `cryptography` package in the gateway image, it fails closed. Sign on the host: `python3 scripts/studio-changes.py signoff <id> --owner <owner> --key-file owner.key > signed.json`, then paste it in the Studio or pass `--signoff-file` |
| Promote knowledge | evals passed, recent and bound to the exact content (`knowledge-promotion.py` rules) |
| New coworker | the template exists, the name is free, every Skill that needs the new coworker's own access starts switched off, and the owner-readable onboarding pack is part of the request (approved by digest) |
| Colleague access | the person is not the owner, still holds the roles the change was written against, and (grant) is on the web sign-in list; the sign-in check is advisory on the host, which has no list |

Approving writes the **Studio overlay** and a rollback snapshot, and ledgers `studio.change-approved` then `studio.change-applied`; a blocked attempt is ledgered as `studio.change-blocked`. Rollback (owner only) restores the exact earlier overlay and is allowed for the latest applied change only. A proposal expires after seven days and is refused if anything it was built on changed.

**An Agent can prepare a change; only a person approves it.** In a team (`platform/runtime/team_runtime.py`), `propose_change` lets an Agent propose a Studio change for the person whose task it is. It is proposed *as that person*, so it works only if they could have proposed it themselves (the owner, or a colleague holding `author`), it never covers an `access` change, and it takes one step from the task's budget. The request shows "through the *coordinator* coworker (task …)" in the Studio and in the ledger (`studio.change-proposed` on the task timeline). Approving, rejecting and rolling back stay with the owner, so an Agent can never apply its own proposal. The model reaches it through the `studio_propose_change` Tool, offered only when the team is given a change store: the person and task come from the attested task, never from the model's arguments, and the answer says nothing has changed until the owner approves. Outside a task, or for someone without `author`, it answers `refused`. As with the rest of the task context, this holds for a host that runs the Agents in one process; sidecars launched as separate containers do not carry the task yet.

**What "applied" does on the live coworker.** The governed sidecar, the worker and the gateway share the data volume, so two kinds of approved change take effect without a redeploy (`platform/core/studio_overlay.py`). A Policy rule that is a *new* `deny`, `human-only` or `require-approval` rule, or replaces a rule with one at least as strict, applies to the very next request (the sidecar rereads the overlay when its file changes; each decision records the rule and the policy version). The worker's `changes` job (every 60 seconds, `intervals.changes`) writes an approved schedule cron or per-run budget onto that schedule, checks it again with the store's own rules and ledgers it as `schedule.update`; a schedule the workspace manages keeps the edit across the worker's restart reconcile. Anything that would **widen** what the coworker may do is held: an `allow` rule, a removed rule or a weaker replacement stays in the overlay and the patch and the sidecar ignores it, because the sidecar holds no owner key to verify the owner's signature. A Skill on/off change is compiled into the Hermes profile, so it waits for the next deploy. Nothing edits the client's workspace repository: the operator turns an applied change into the real change with `python3 scripts/studio-changes.py export <id>` (the new `disabledSkills`, schedule values, signed Policy rule, promotion record, or new Agent resource plus pack) and commits it through the normal pull-request, DEV, promote flow, which is also how a held rule goes live. A new coworker is "planned" until that merge and until it has its own access grants ([#129](https://github.com/kingyx3/model-garden/issues/129)).

**Host operation.** `python3 scripts/studio-changes.py list|show|propose|approve|reject|rollback|signoff|export` uses the same data root, desired state, capability manifest and `gateway.json` as the gateway and the same gates and ledger events. `approve` needs `--by <owner>` and `--digest`; the identity is as asserted on the command line (not authenticated), which is why a relaxing Policy change always needs the owner's signature.

**Web safety.** The session is the web chat's bearer token, never a cookie. Every state-changing call must be a same-origin `application/json` POST with `Origin` equal to `publicOrigin` and an `X-Studio-CSRF` token derived from the caller's session; bodies are capped at 32 KB; the role comes from the verified session, not the request. The page ships its own script and style under the same strict CSP as the chat page (no inline code, no CDN) and writes server text with `textContent`. The API returns names, digests, readiness text and non-secret grant summaries only. Not live-proven in a browser against a real deployment.

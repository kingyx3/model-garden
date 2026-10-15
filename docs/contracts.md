# Platform contracts

`contracts/v1/` is the compatibility boundary between the Model Garden platform, business-owned AI content and stable operational integrations.

Supported v1 business resources:

- `Agent` — composition of model profile, instructions, skills, tools and knowledge.
- `Skill` — reusable reasoning/instruction package.
- `ModelProfile` — logical model requirement mapped to approved candidates.
- `Tool` — executable enterprise capability with an explicit risk class.
- `KnowledgeSource` — approved source and data classification.
- `Policy` — client-owned approval rules, enforced by the governed boundary (see [Client approval policy](#client-approval-policy)); a dedicated policy engine remains out of scope.

**Target, not shipped:** reusable `Skillset` catalogue packages will reference versioned Skills, but do not grant Tools or force every member active. Today an Agent selects `spec.skills` and may disable individual members with `spec.disabledSkills`. First-class Skillset selection, deduplication and bundle resolution remain [#128](https://github.com/kingyx3/model-garden/issues/128); business behavior is defined in the [target architecture](https://modelgarden.atlassian.net/wiki/spaces/BA/pages/11796484).

## Capability contracts

`contracts/v1/capabilities.json` (validated by `capability-catalog.schema.json`) is the platform-owned catalog of provider-neutral Tool families: `chat`, `mail`, `calendar`, `files`, `docs`, `tickets`, `crm`, `code` and `kb`. Each portable Tool (for example `mail.send`, `tickets.create`) has argument and result schemas, a risk class (`read`, `write`, `privileged`, `external-communication`) and the argument fields that are safe to keep verbatim in the action ledger.

- `Agent` and `Skill` resources may declare `requiredCapabilities` and `optionalCapabilities`, naming a family (`calendar`) or a portable Tool (`calendar.book`). A required declaration on any selected Skill makes the capability required for the Agent.
- A `Tool` may opt in with `spec.capability: <family>`; its name must then be a portable Tool of that family and its risk must match the catalog. The reference `calendar.availability`, `calendar.book` and `crm.read` Tools do this with no behaviour change.
- Only the EnvironmentBinding chooses a provider, as `connectors.<family>.provider` (the legacy `connectors.google_calendar` / `microsoft_calendar` shapes map to `calendar`). Optional non-secret `tier` (0–4, default 1), `scopes` and `owner` describe the connection. **Implementation limit:** this is an environment-level binding, not a separately authenticated grant for each AI coworker. Distinct downstream provider identities, Agent-attested connection lookup and independently revocable scopes are required before multiple coworkers act autonomously in the same client system ([#129](https://github.com/kingyx3/model-garden/issues/129), [#95](https://github.com/kingyx3/model-garden/issues/95)). Never reuse a shared provider token as a substitute.
- `compile-workspace.py --environment-binding <file>` resolves declarations: a required capability with no provider fails closed with a message naming the Agent, capability and environment; an unbound optional one compiles and is reported under `capabilities.degraded` with its fallback tier (Tier 4, human hand-off, until built-in Tier 0 storage exists; the `handoff_request` platform Tool prepares a write action for a named person, see [Human hand-off](governed-tools.md#human-hand-off-tier-4)). Docker deployment re-runs the same check against the real binding.
- The fail-closed message also names the Skill(s) that require each missing capability. With a binding, the compiled output carries `capabilities.skills`: one entry per selected Skill (plus the Agent when it declares capabilities itself) with `ready` and each dependency as `bound` (provider, tier), `missing` or `degraded` (fallback tier). It copies no token references, calendar IDs, scopes or owners.
- `compile-workspace.py --environment-binding <file> --readiness` prints that per-Skill table instead of compiling and exits non-zero if any enabled Skill is blocked. Disabled Skills remain visible for planning but are not executable.
- The coworker's runtime `platform.capabilities` Skill view is recomputed from its **actual deployment binding** rather than trusting a potentially absent or stale compiler snapshot. To keep disabled Skills explainable, compiled desired state may carry `disabledSkillDeclarations`: only non-executable capability names, never Skill instructions or Tool grants. Legacy disabled Skills with no recorded dependency metadata are explicitly blocked pending recompilation. Health changes (including revocation) update readiness without granting disabled Tools.

The `crm` family also carries `crm.create` (risk `write`) and `crm.log-interaction` (risk `write`: channel, direction, timestamp, summary, outcome and a transcript or message *reference*). Their ledger fields are object type/ID and interaction type only, never summary or transcript text. Only the Tier 0 built-in log (below) implements these Tools; HubSpot/Salesforce/Zoho (#93) do not yet and the integration registry does not list them against those providers. Making the internal-record `crm.log-interaction` automatic while `crm.create`/`crm.update` stay approval-gated is a client decision: leave `crm.log-interaction` without `approvalRequired` on its Tool, or add a signed `allow` rule (below).

All of this is additive to `modelgarden.ai/v1`: existing workspaces without declarations validate and compile unchanged.

## Tier 0 business-rules knowledge

A `KnowledgeSource` with `spec.type: text-file` points at one `.txt` or `.md` file of approved business rules (`spec.file`, relative to the resource, at most 256 KiB, UTF-8, confined to the workspace or library root) so a small client needs no knowledge system. Optional `status` (`draft` default, `approved`, `retired`), `version`, `effectiveDate` and `audiences` (Agents allowed to ground answers) travel with it; these fields are rejected on other source types.

- Compiling an Agent that selects the source parses the file as an upload test and materializes it into desired state under `knowledgeContent` (content, SHA-256, section count). An empty, unreadable, oversized or escaping file fails compilation. `python3 scripts/knowledge-check.py rules.md [--query ...]` runs the same parse/test on an upload and shows what a search would cite.
- `platform/core/knowledge.py` splits the file into stable numbered sections (Markdown headings, recognisable plain-text headings, or paragraph chunks) and searches them. `search_selected` returns the `kb.search` result shape: each document carries its section citation (`knowledge:<source>#<section-id>`), source SHA-256 and version, and the result is flagged `dataOnly` (retrieved text is reference data, never instructions). Only content that is `approved`, effective today and addressed to this Agent is returned; otherwise there are no documents and a reason (`source-draft`, `source-retired`, `source-not-yet-effective`, `source-not-for-this-agent`, `no-matching-section`) so the caller clarifies or escalates.
- Each client/environment searches only the content compiled into its own desired state. `read_receipt` turns a result into section IDs and content digests for the existing governed read event (`record_knowledge_read` / `execute_action(read_receipt=...)`), so the ledger never holds the text.

**Runtime and activation.** The governed sidecar exposes `kb_search` (the `kb.search` Tool) over this module. It runs only when the Agent selected the `kb.search` Tool (see `examples/workspace/tools/kb-search.yaml`) **and** the capability manifest shows `kb` bound to `text-knowledge` at Tier 0; otherwise it refuses with "not connected here". Each search is a governed read: the ledger records provider `text-knowledge`, `tier: 0` and the section IDs and content digests, never the text. A client binds it with `connectors.kb: {provider: text-knowledge, tier: 0}` in the EnvironmentBinding; the registry lists it as `built-credential-free` (adapter `platform/core/knowledge.py`), so a Skill requiring `kb` is ready and the readiness report shows `kb: text-knowledge tier 0`. A deployment that selects `kb.search` gets the governed sidecar even with no calendar bound, and that sidecar holds no provider credential. This is credential-free build evidence only: no live deployment has exercised it.

**Not yet delivered:** document-level permissions, external knowledge providers, freshness/refresh policy and conflict detection (#116, #77). The reference receptionist declares `kb` and `crm` and its evals exercise both Tier 0 paths (see [Reference receptionist on a zero-stack client](#reference-receptionist-on-a-zero-stack-client)).

## Tier 0 contact and interaction log

With `connectors.crm: {provider: built-in-crm, tier: 0}` bound, the governed sidecar serves `crm.create`, `crm.log-interaction` and `crm.read` from a private per-client store (`platform/core/contacts.py`, on the sidecar-only volume at `/opt/data/.modelgarden/contacts`). Nothing is refused less strictly than a connector would be: each tool runs only when the manifest shows `crm` bound to `built-in-crm` at tier 0, is ledgered at `tier: 0` with an external object reference such as `built-in-crm:interaction/i-...`, and never writes names, numbers or summaries to the ledger (arguments are redacted, results are digests, reads record section-style record IDs and content digests).

- **No merging on an assertion.** `crm.create` always makes a new record whose identity evidence is `asserted`. A colliding phone or email adds the pair to a human review queue; the response never says so, so a caller cannot learn that a number belongs to a customer.
- **No guessing a contact.** `crm.log-interaction` files against a record ID the coworker holds, or `unmatched` for a person to assign; an unknown ID is an error. Interaction IDs are derived from their content (contact, type, channel, direction, timestamp, and the transcript reference or summary digest), so a retry, redelivered event or restarted worker returns the same ID with `duplicate: true` and writes nothing.
- **No read-back to unverified principals.** `crm.read` returns records only when the governed boundary reports a verified `member` principal. A phone caller (the voice channel still uses the static runtime identity), an unmapped sender or any other principal gets `requires-verified-staff-principal` and no data.
- **Bounded and private.** At most 5,000 contacts and 20,000 interactions, bounded field lengths, payment card numbers redacted from notes and summaries, files mode 0600 in a 0700 directory. Records are plaintext at rest on the sidecar volume, like the ledger and approval store; encryption at rest and retention schedules are deployment concerns.
- **Policy.** The example `crm.create` Tool is approval-required (and the default policy's `*.create` rule agrees) and the example `crm.log-interaction` Tool is not, matching "an internal record". Public principals can do neither (the platform floor).
- **People review and erase.** `python3 scripts/client-operator.py contacts review|assign|dismiss|erase-contact|erase-interaction --project-name <env> [ids] --by <person>` runs `scripts/runtime-contacts.py` on the sidecar. `review` prints personal data for a person to act on. Every change writes a chained `operator-action` event naming the person and the IDs, never the record contents; the identity is as asserted on the command line.

`crm.update` and object types other than `contact` are not implemented at Tier 0; selecting `crm.update` fails closed at provisioning. The `examples/workspace` `log-conversation` Skill describes the intended behaviour (file after each call or email, `unmatched` when unsure, never read back, short factual summaries); no example Agent selects it yet. **Not delivered:** wiring the voice transcript or support mailbox into filing (#107, #109), cross-channel timelines and CRM-outage reconciliation (#115), third-party CRMs (#93), retention schedules, and any live proof.

## Client approval policy

`Agent.spec.policies` names `Policy` resources whose ordered rules the governed boundary enforces for that Agent. A rule has an `effect` (`allow`, `deny`, `require-approval`) and optionally `actions` (Tool names or globs such as `calendar.book`, `*.send`, `crm.*`), `dataClassifications`, `channels` and `principals` (`member`, `public`).

A fourth effect, `human-only`, forbids automation: the action is prepared for the rule's required `assignee` (optional `deadlineHours`) as a Tier 4 hand-off instead of being executed (see [Human hand-off](governed-tools.md#human-hand-off-tier-4)). It is restrictive, so it covers every principal and an unknown channel or classification, and removing it counts as relaxing control in a policy diff.

Precedence is fail-closed:

1. a matching `deny` rule wins, then a matching `human-only` rule (nothing is executed, whatever else matches);
2. the Agent's `approvals.externalActions: required` and any matching `require-approval` rule always win over an `allow` rule (the stricter source wins);
3. `privileged` Tools are never auto-approved, and a public principal can never reach `privileged` or `external-communication` Tools;
4. a Tool's own `approvalRequired` default is waived only by a matching `allow` rule that carries a valid client-owner sign-off.

With no policy, Agent and Tool settings behave exactly as before (plus the `privileged` floor in point 3). Restrictive rules cover every principal and an unknown channel or classification; an `allow` rule covers members only unless it names `public`, and never matches an unknown channel or classification. A public principal may be given one named `write` Tool (for example booking inside published availability) only by an explicit `allow` rule with `principals: [public]` and a sign-off.

**Owner sign-off, checked at compile and deploy time.** An `allow` rule that relaxes an approval requirement (it matches a non-read Tool, or names `public`) must carry `signoff: {owner, ruleDigest}`. `ruleDigest` is the SHA-256 of the rule's canonical JSON without `signoff`, so editing the rule invalidates the sign-off and compilation fails, printing the digest to re-sign, until the named owner approves the new content. The runtime re-checks the digest before it honours an `allow`. Protect the sign-off lines with `CODEOWNERS` in the client repository so only the named client owner can change them; the platform checks that a sign-off exists and matches the rule, not who typed it. An environment can narrow who may claim to be the owner by naming them in its EnvironmentBinding (`governance: {owners: [owner@client.example]}`): compilation then also refuses a relaxing rule whose `signoff.owner` is not on that list (matched ignoring case and spacing), and an empty list refuses every relaxing rule. The list lives in the binding so it can be protected separately from the policy file (for example under different `CODEOWNERS`); it limits which names may sign but still does not authenticate the person, and it is checked at compile time, not by the running sidecar.

**Ledger.** Every decision records the deciding rule ID as `policyRuleId` (or the built-in reason when no rule decided) and the rule-set version `policy:<digest>` as `policyVersion`. `govern-action.py`'s `record_policy_change(path, before, after, changed_by=...)` appends a chained `policy-change` event with the author and the rule-level diff, flagging rules that loosen control; the deploy path that calls it is not wired yet.

**Preview.** `python3 scripts/compile-workspace.py <workspace> --approvals` prints, per Skill, which Tool actions run automatically, need approval or are denied for members and for public callers.

**Bounded conditions.** An `allow` rule may carry `conditions` that can only *narrow* it (`conditions` on any other effect is a compile error):

```yaml
- id: faq-replies
  effect: allow
  actions: [mail.send]
  conditions:
    arguments: {category: [faq]}   # every named Tool argument must be present and equal to a listed value
    maxPerDay: 20                  # applies only while fewer than 20 successful executions of the matched Tool are in today's (UTC) local ledger
  signoff: {owner: owner@client.example, ruleDigest: <sha256>}
```

When a condition is unmet, or cannot be checked (a missing or non-scalar argument, no readable ledger), the rule does not apply and the stricter default stands, so a limited rule reached its limit shows `policy-limit-reached` with the rule ID in the ledger and the call needs approval. Values are compared by type (`1` does not match `true`). The conditions are part of the rule content, so editing them invalidates the owner's sign-off until it is re-signed. `--approvals` shows such a Tool as `conditional`. Counts come from the sidecar's local chained ledger; they are not a distributed rate limiter (concurrent calls can overshoot by the number in flight) and a client who deletes the ledger resets them, so use `deny`/`human-only` for hard limits.

Not yet supported: classification of free-text content (for example deciding that a message *is* an FAQ), per-principal counters, verifying that the named owner really signed (a sign-off proves content integrity, not identity), and consultant/owner role separation beyond the sign-off above.

## Delegation between Agents (task envelopes)

`platform/core/delegation.py` holds the rules for a coordinator Agent handing work to a specialist without creating a way to do more than a person could have asked for. It is a pure module with tests, **not wired into a runtime**: the governed sidecar still serves one Agent per environment (#95), so nothing here yet decides a live call.

- **Envelope.** goal, constraints, deadline, budget (`tokens`, `costCents`, `steps`), the Tools the task may use, the originating principal, the parent task and the chain of Agents so far. The root envelope's authority is the requesting person's permissions narrowed to what the first Agent holds; a child cannot change the originating principal.
- **Attenuation.** A delegate's effective authority is its own grants ∩ the delegator's authority for this task ∩ the originating principal's permissions, re-checked on every hop. A delegate asked to use a Tool outside that set is refused rather than silently narrowed, and authority never grows along a chain.
- **Bounds.** Depth (default 3), fan-out (default 5) and loops (an Agent already in the chain) are refused; a child's budget and deadline can only be tighter than what the parent has left, and a parent whose budget is exhausted cannot delegate (a task with an exhausted bound must halt and its owner be told, which nothing sends yet).
- **Governance and ledger.** `governance_context` yields the `originatingPrincipal`, `taskId` and `delegationChain` fields the governed boundary already binds into every `requestId`; `permits` is the check a runtime must make before policy evaluation; `record_hop` ledgers each hop as a `delegation.hop` event holding identifiers, counts and a digest of the goal, never the goal.

Open: the runtime-native handoff or A2A decision, a multi-Agent runtime that calls these rules, owner notification on budget exhaustion and live evidence (#96, #95, #71).

## Coworker memory

`platform/core/memory.py` is the Model Garden contract for what a coworker remembers, a credential-free store with its own tests; it is **not connected** to Hermes's memory or to chat commands, and whether a runtime's native memory sits behind it is undecided (#100).

- **Scopes:** `client`, `agent:<name>` and `conversation:<id>`. A reader names the scopes it may see; anything else is invisible, so one conversation's memory never reaches another. A store belongs to one tenant, in its own file, and refuses another tenant's file.
- **Writes** redact card numbers, refuse `restricted` classification unless the deployment's policy allows it, are bounded (500 characters, 2,000 entries) and are idempotent per scope. Each write, delete, export and purge calls the audit hook with identifiers and a digest of the text, never the text.
- **People and administrators.** A person may list and delete entries they own; an administrator may export and purge per scope or for the whole client: `client-operator.py memory list|export|delete|purge --project-name <env>` (client data, so run it on the client host; the actor is as asserted on the command line, like approvals).

## Scheduled coworker tasks

`platform/core/schedules.py` is the contract for recurring work with a named owner ("send me a pipeline summary every Monday"). It is a credential-free store and cron arithmetic with tests, **not a scheduler**: nothing wakes a coworker, and event triggers (new email, ticket, chat message) and chat commands are not part of it (#99).

- **Named owner and budget.** Every schedule belongs to one principal and must carry a per-run budget (`tokens`, `costCents` and/or `steps`, each at least 1). A run's task envelope comes from `delegation.root_envelope` with the owner as the originating principal, so its authority is the Agent's grants narrowed to the owner's permissions *at run time*; when the owner loses a permission the next run is refused (`ScheduleError`) instead of widened.
- **Cron-like, UTC.** Five fields with `*`, lists, ranges and `/step`; when both day fields are restricted either may match, as in standard cron. Fires closer than 15 minutes apart are refused. After downtime a schedule fires once for the slots it missed, and resuming after a pause never replays old slots.
- **Owner-only control.** Only the owner or an administrator pauses, resumes or deletes a schedule; at most 20 schedules per owner and 500 per tenant. One private file per tenant (another tenant's file is refused). Every change calls the audit hook with identifiers, the budget and a digest of the goal, never the goal.

Open: a scheduler that fires runs as tasks with their own ledger timeline, owner notification on failure, event triggers, chat create/list/pause/delete and live evidence (#99, #73).

## Human step in a task

`platform/core/human_step.py` lets a task step target a named person using the Tier 4 hand-off store, with no storage of its own: the pending hand-off is the durable pause, so a restart loses nothing, and the inbox, notices and `runtime-handoff.py complete` are how the person is told and answers. It is **not wired into a runtime** (no multi-Agent task runner yet, #95) (#97).

- **The step carries the exact ask** and a short context; notices carry only the Tool name and a reference, never the ask.
- **`resolution`** reports `waiting`, `resumed` (done, with the person's note and identity), `declined`, `timed-out` or `unknown`. A decline or timeout never resumes: it returns the configured fallback (`fail`, `skip` or `escalate` to a named person). Reopening the same step returns the same hand-off and does not reset its clock.
- **No widening.** The answer is data for the task; it never changes the envelope's authority.
- **Ledger.** `task.human-step.opened` (under the Agent), `task.human-step.resolved` (under the person) and `task.human-step.timed-out` (under the task) hold identifiers, the outcome, lateness and digests of the ask and note.

Open: delivery to the person's own channel and acknowledgement from it, a runtime that pauses and resumes a task on this, and live evidence (#97, #88).

## CRM sync states

`platform/core/crm_sync.py` is a provider-neutral delivery ledger for filing interactions to a CRM; it contains no provider code and does not call one (#115).

- **Key.** A stable idempotency key from tenant, channel, external call or message id and interaction type, so a redelivered webhook, a retry and a restarted worker reach the same entry and never create a second record; a provider that supports idempotent writes is given the same key.
- **States.** `pending`, `synced`, `failed`, `blocked`, `reconciled`. `synced` and `reconciled` need the provider's reference, so nothing claims a filing the provider did not acknowledge. Transient errors (`rate-limited`, `outage`) retry after 30 s, doubling to 1 h; `rejected`, `unauthorized` or eight attempts block until a named person retries. `reconcile` links a record found at the provider without writing it again.
- **Readable.** `describe` returns the state, the provider reference when there is one, and a plain repair action; transitions are ledgered with the key, state, attempt count and error code, never interaction content.

Open: cross-channel conversation timelines, verified identity linkage, wiring into the voice and email filing paths and connectors, safe provider swap with a re-link plan, retention/erasure across providers and live evidence (#115, #108).

## Deliverables and the Tier 0 artifact store

`platform/core/deliverables.py` builds real DOCX and XLSX files (and CSV) from structured Agent output with the standard library only, and `platform/core/artifacts.py` is the Tier 0 store they land in when the client has no file system bound. Both are credential-free modules with tests; they are **not yet behind** `docs.create`/`files.*` on the governed sidecar, not driven by a Skill, and they post no link into a channel (#101, #77).

- **Builders.** A report is a title plus sections of headings and paragraphs; a workbook is sheets of rows. Output is deterministic (same input, same bytes and digest). Spreadsheet strings are written as literal inline strings and CSV cells that start with `= + - @` are quoted, so text from an email cannot run as a formula; XML is escaped and control characters removed; sizes are bounded. PPTX, PDF and native Google/Microsoft formats are not generated.
- **Store.** One directory per tenant (0700, files 0600, an index for another tenant is refused). Files are named by artifact id, never by the display name. Only office, PDF, CSV, Markdown and text types, at most 10 MB and 2,000 artifacts; `restricted` data is refused unless the deployment's policy allows it. A read recomputes the SHA-256 and refuses a file that changed on disk.
- **Accountable.** Each artifact records who asked, which Agent produced it, a bounded list of source identifiers (for example `kb:rules-of-engagement#hours`) and its `tier0:<tenant>/<id>` location. `artifact.create`, `artifact.read`, `artifact.delete` and `artifact.expire` are ledgered with identifiers, size, digest and sources, never content or names.
- **People and retention.** The requester or an administrator reads or deletes; artifacts expire after the client's retention period (default 365 days) and `purge_expired` removes the files. `client-operator.py artifacts list|delete|purge-expired --project-name <env>` is the operator view (client data: run it on the client host; the actor is as asserted on the command line, like approvals).

Open: PPTX/PDF and native formats, `docs.create`/`files.*` backing on the sidecar, links in the requesting channel, Skill-driven generation, evals through the runtime and live evidence (#101, #77).

## Skillsets

`platform/core/skillsets.py` is the credential-free first contract for reusable Skill bundles. A Skillset is a versioned list of Skill **references**, not a security principal, an Agent, an all-or-nothing unit or a place for credentials. It is **not yet read by `compile-workspace.py` or a registry**; it produces the `skills` and `disabledSkills` values the compiler already understands (#128).

- **Validation** is strict: unknown keys are refused, and so is any key that looks like a secret (token, password, API key, credential).
- **Expansion is deterministic and fails closed.** Direct Skills, selected Skillsets and `includes` expand to one sorted, deduplicated list with the sources that brought each Skill. A missing Skillset or Skill, a pin that differs from the available version, conflicting pins, a cycle or nesting deeper than 4, and an include with several versions each raise an error and expand nothing.
- **Selection is not activation.** `activation` gives every selected Skill a coworker-specific state, all off unless named; `exposed_tools` returns Tools only through enabled Skills, so enabling one member never adds another member's Tools. The compiler's own readiness still blocks an enabled Skill whose dependency is missing.
- **Independent per coworker.** The same bundle resolves against each coworker's own available Skills and enabled set; the bundle holds no coworker state.

Open: reading Skillsets in the compiler or a registry, knowledge-aware prerequisites, per-coworker identities and grants (#129, #95), runtime re-evaluation at invocation, the reviewed PR → DEV/evals → business approval → PROD change flow with rollback, and live evidence (#128).

## Per-coworker provider grants

`platform/core/provider_grants.py` is the credential-free contract for giving each AI coworker its own authorization to a connected system (#129). It holds **references**, never secrets, and is **not wired into the governed sidecar**: today the sidecar still resolves provider access from the environment-level binding, so no deployment has independently authenticated coworkers.

- **One grant per coworker and connection:** immutable tenant (client and environment), Agent, family, provider and connection id, the provider-side acting identity or isolated delegated grant, a `secret://` reference, granted Tools, an owner, health, optional expiry and a revocation state. Operators see the reference only as a digest.
- **No shared credentials.** A secret reference or acting identity held by another coworker is refused, as is a `shared-login` grant next to a second coworker on the same family (a shared login is allowed for a lone coworker but reported `multiCoworkerSupported: false`).
- **Attested caller only.** `resolve` takes the runtime-attested Agent name and no argument through which a model, coordinator or delegate could name another coworker or hand over a token. Rights are the originating principal's permissions ∩ the coworker's granted scopes ∩ the Tools enabled for it; revoked, expired or failed grants refuse instead of falling back.
- **Independent lifecycle and secure upgrade.** Revoking, rotating or degrading one grant never changes another. `upgrade_from_environment` proposes a grant from an environment binding only for a workspace with exactly one Agent and refuses several, so an old shared token is never silently reused. `receipt_fields` gives the attribution a call must carry (Agent, initiating principal, grant, connection, provider-side identity, scopes, request id).

Open: provisioning real per-coworker identities or grants, sidecar resolution from attested caller context, provider-side audit verification, real tenant proof per authentication mode (#129, #110, #86, #95).

## Team of Agents

`platform/core/team.py` checks the compiled desired states of several Agents (the compiler already emits them when no `--agent` is given) before a runtime may advertise more than one coworker. It changes nothing and is not wired into a runtime; the governed sidecar still serves one Agent per environment (#95).

- **Tool isolation.** `authorize_call` allows a call only if the Tool is one of the *calling* Agent's own compiled Tools, using the attested Agent name; another Agent's Tool is refused. The action request identity already includes the Agent, so one Agent's approval cannot be replayed by another.
- **Own provider access.** With several Agents, each Agent whose Tools belong to an external (tier 1+) family needs a usable grant of its own covering them (`plan` reports `blocked` with a plain reason); Tier 0 families need none; an unbound family blocks. Duplicate Agent names are refused.
- **Single-Agent workspaces are unchanged:** one Agent and no registry is `ready` through the environment binding.

Open: a multi-Agent sidecar and deployment, per-Agent usage attribution and ledger separation at runtime, and cross-Agent token isolation proof against a real provider (#95, #129).

## Governed MCP passthrough

`platform/core/mcp_passthrough.py` defines what the governed sidecar may expose from a vendor or client MCP server. It contains **no MCP client** and is not wired into the sidecar; Hermes would still see only the sidecar's own Tools (#87).

- **Declaration.** Name, https URL (no credentials, query or fragment), `secret://` credential reference and an explicit allow-list; every allowed tool has an explicit risk class (`read`, `write`, `privileged`, `external-communication`) and may map to a portable capability Tool (for example `crm.read`). There is no allow-all; unclassified entries are refused.
- **Exposure.** Unlisted upstream tools are never exposed (they are only counted); a listed tool the server no longer offers is reported missing. Exposed tools become `modelgarden.ai/v1` Tool resources of type `mcp`, with `approvalRequired: true` for anything but `read` (a signed client policy can relax it, as for native Tools).
- **Schema pinning.** An operator approves a tool by fingerprint over its name, description and input schema. A changed definition (including a rewritten description) hides the tool and `call_gate` fails closed until it is approved again; approvals are ledgered as fingerprints, never schemas.
- **Ledger.** `ledger_fields` supplies `tier: 2`, `provider: mcp:<server>`, the server and upstream tool name, never arguments or results.

Open: an MCP client in the sidecar with the credential stored there, binding declarations in the EnvironmentBinding and compiler, approval workflow commands, and live evidence with a real upstream server (#87, #89).

## Mail, HubSpot and built-in files adapters (in development)

These adapters implement portable Tools behind an injected transport with credential-free tests only. They are registered `in-development`: the governed sidecar does not serve them, no client consent or per-coworker identity (#129) exists for them, and none has live proof, so a binding that names them is refused and nothing here is marked supported.

- **Mail (#90).** `platform/connectors/gmail.py` and `platform/connectors/microsoft_mail.py` implement `mail.search`, `mail.read`, `mail.draft` and `mail.send` with identical arguments and result shapes (a contract test runs the same calls against both). The connection is bound to one configured mailbox and no argument can name another mailbox or sender; there is no tenant-wide access. Sending is draft-first: `mail.send` takes only the id of an existing draft (Gmail's `drafts/send`; Graph confirms the message is still a draft first). `mail.send` stays `external-communication` risk and needs exact-action approval by default. Recipients are plain addresses (no cc/bcc), header injection and control characters are refused, bodies are bounded, HTML-only mail is returned as an empty body rather than markup, and results carry `externalObjectRef` values so the ledger records references, never bodies. Graph does not return the sent item's id, so `mail.send` reports the draft id with `sentFromDraft`.
- **HubSpot (#93).** `platform/connectors/hubspot.py` implements `crm.read`, `crm.update`, `crm.create` and `crm.log-interaction` for contacts, companies and deals. A per-object `allowed_fields` list is the only thing returned to the model, `writable_fields` (a subset; none by default, so read-only) is the only thing it can change, `crm.create` never merges and reports a conflict without revealing the existing record, and `crm.log-interaction` writes a note with a transcript *reference* after searching for its content-derived marker, so a re-delivered interaction writes nothing. Failures raise `HubSpotError` with a `code` that matches `crm_sync` (`rate-limited`, `outage`, `unauthorized`, `rejected`). HubSpot's default note association type ids still need confirming in a live test.
- **Built-in files and docs (#91, #77).** `platform/core/builtin_files.py` maps `files.search`, `files.read`, `files.write`, `docs.create` and `docs.read` onto the Tier 0 artifact store when no file system is bound. Executors are built for one verified requester, so a person sees only their own artifacts; there are no folders (a `folderId` is refused); text is returned to the model and binary files (DOCX, XLSX) as metadata only; creation is ledgered by the store with its `tier0:<tenant>/<id>` location, size and digest, never content or title.

Open: serving any of these from the sidecar, OAuth consent and token lifecycle (#86, #110), per-coworker identities (#129), Drive and SharePoint/OneDrive connectors (#91), Salesforce and Zoho (#93), automatic filing from calls and mail (#108, #109), and live proof on each provider.

## Owner notices for connections and provider grants

`platform/core/connection_notices.py` works out who should be told when a connection or a per-coworker provider grant needs a person (#86). It sends nothing: a deployment delivers each notice through a channel or the inbox and then records it, as with hand-off notices. It is credential-free, not wired into the sidecar or a channel, and no owner delivery exists yet.

- **Conditions.** A capability connection that is `degraded`, `failed` or `revoked`, and a grant (#129) that is revoked, failed, degraded, expired or expiring within a warning window (default 7 days).
- **Who.** The connection's named `owner` from the EnvironmentBinding, or the grant's owner; with none, the escalation contact; with neither, the notice is still returned with `to: null` and `unowned: true`, so it cannot be silently dropped.
- **Once per state, at least once.** A notice has a stable key (connection or grant, condition, when the state began), is offered until delivery is recorded, and is not repeated for an unchanged state. A cleared condition is forgotten so a recurrence is told again. Only a notice the store offered can be marked delivered; `connection.notice-delivered` is ledgered with the key, kind and channel.
- **Non-secret.** Notices name the family or the grant's connection, the condition and a recovery step, never a reason string, scope, account, secret reference or the provider-side identity.

Open: delivering notices through Slack, Teams, email or the inbox, escalation after a notice goes unacknowledged, OAuth consent, token refresh and provider-side revocation (#86, #110).

## Filing interactions through the sync ledger

`platform/core/crm_filing.py` runs the filing that `crm_sync.py` tracks: it takes the portable `crm.log-interaction` arguments and an executor (the Tier 0 contact log or the HubSpot adapter, which share an `arguments -> result` shape) and records every attempt in the provider-neutral sync ledger. It contains no provider code, is credential-free, and is **not yet wired** to the voice or mail paths (#108, #115).

- **One entry per call or message.** The ledger key comes from the channel, the call or message id and the interaction type, so redelivery, a retry and a restart reach the same entry and nothing is filed twice. Tests replay the same interaction through the built-in log and the HubSpot adapter and get the same states.
- **Success only on the provider's word.** A result without an `externalObjectRef` is a rejection. A result flagged `duplicate` means the record was already there (for example the first attempt landed and its answer was lost), so the entry is *reconciled* to the existing reference rather than written again.
- **Failures are sorted.** An error carrying a sync `code` keeps it; a `PermissionError` is `unauthorized`; a `ValueError` (bad arguments, an unknown contact) is `rejected` and waits for a person; anything else is a transient `outage` that backs off and, after the ceiling, blocks.
- **No content in the ledger.** `retry_due` takes a `payloads` callable for the interaction to retry (a deployment reads it from its transcript or message store); an entry whose payload is unavailable is skipped and counted, never invented.

Open: wiring this to the voice transcript and support-mailbox paths, a payload store, real CRMs, cross-channel timelines and identity linkage, and live proof (#108, #115).

## Guided connect step

`platform/core/connect.py` is the credential-free core of connecting a CRM, mailbox or calendar for one named coworker (#110). The operator provisions the provider-side identity and secret elsewhere and passes only the `secret://` reference; this module runs no owner OAuth consent, stores no secret, makes no provider call itself (the read-only test is injected) and is not wired to the interactive CLI.

- **Plan.** For a named Agent, client owner and operator, the plan takes its scopes from the registry's reviewed mapping for the selected portable Tools and refuses a request for more. It carries a digest of everything the owner consents to; `apply` needs that digest back, so consent to one plan cannot be reused for another. Providers that are not shipped, and Tier 0 providers (which need no credentials), are not connectable and the plan says why. A mailbox is `shared` or `delegated`; a `personal` one needs an explicit recorded owner exception.
- **Apply.** The read-only connection test runs first. If it fails nothing is recorded and the failure is ledgered; on success the coworker's own grant is recorded (`provider_grants.py`, so a second coworker never inherits the first one's credential) and the binding fragment (provider, tier, scopes, owner, never a secret) is returned for `merge_binding`, which leaves the input untouched, keeps a live `token_ref` for the same provider and makes replacing a provider explicit.
- **Ledger.** `connection.connect`, `connection.connect-failed` and `connection.disconnect` name the operator and the client owner with identifiers, never a credential, reference or account. `disconnect` revokes one coworker's grant and affects no other.

Open: owner OAuth consent and token acquisition, credential storage, a live test, re-scoping, re-running Skill readiness after a change, and the interactive CLI (#110, #86, #129).

Stable operational contract:

- `usage-attribution.schema.json` — canonical non-secret client/Agent/environment/provider and optional cost/trace/subscription/shared-pool dimensions used to correlate runtime consumption with showback and billing. This is platform-generated operational metadata, not client-authored Agent/Skill desired state.

Business teams should depend on the business resource contracts rather than infrastructure details or physical model IDs. Runtime/commercial integrations should depend on the explicit operational contracts rather than inventing provider-specific attribution shapes. Breaking schema changes require a new API version, e.g. `modelgarden.ai/v2`.

## Integration registry and readiness-to-connect boundary

`contracts/v1/integrations.json` is the **single technical inventory** of providers by capability family, connection method/tier, maturity, portable Tools, tracking issue and (for implemented providers) adapter source. Its schema, Tool coverage, adapter existence and live-proof requirements are exercised in tests. **`built-credential-free` means an adapter and automated tests exist; it does not mean a real client tenant, refreshable authorization, or production acceptance has been proven.** No provider is currently marked `live`.

Render the provider directory for review and Confluence sync without maintaining a second manually authored list:

```bash
python3 scripts/integration-directory.py --format markdown
python3 scripts/integration-directory.py --family crm --format json
```

An operator can inspect a family, then request an **explicit, non-secret scope preview** before seeking the client owner's authorization:

```bash
python3 scripts/client-operator.py connect calendar
python3 scripts/client-operator.py connect calendar --provider google-calendar \
  --tools calendar.availability --owner client-owner@example.com --operator consultant@example.com
python3 scripts/compile-workspace.py examples/workspace --agent receptionist \
  --environment-binding environments/dev.yaml --readiness
```

The CLI does **not** obtain OAuth tokens, provision a tenant, save a secret, test a live provider or change an EnvironmentBinding. Planned providers fail the preview. An implemented provider returns `readyToConnect: false` with the remaining live consent/health gate, rather than implying connectivity. The normal private GitHub Environment secret map and reviewed PR → DEV → PROD workflow remain the actual deployment interface. The Skill readiness report links missing capability families back to the provider options; the coworker's `platform.capabilities` manifest also includes selected per-Skill readiness. Full guided provisioning and real OAuth lifecycle remain tracked under [#110](https://github.com/kingyx3/model-garden/issues/110) and [#86](https://github.com/kingyx3/model-garden/issues/86).

## Shipped-provider activation gate

An EnvironmentBinding naming a provider is not evidence that its connector exists. The
compiler, `client-operator skills` readiness report, non-secret coworker
`platform.capabilities` manifest and Docker deployment now consult
`contracts/v1/integrations.json` to determine **implemented** support. Only
`built-credential-free` or `live` providers with a matching family, correct tier
and a real local adapter satisfy a dependency. Historical `google_calendar`,
`microsoft_calendar`, `google` and `microsoft` aliases continue to work.

A planned HubSpot/Jira/Zoho/SharePoint/Drive provider, an unregistered provider,
or a Tier 0 provider bound at the wrong tier or under the wrong family (for example
`built-in-crm` at tier 1 or under `kb`) **cannot activate a Skill**. The Tier 0
`text-knowledge` (`connectors.kb: {provider: text-knowledge, tier: 0}`) and `built-in-crm`
(`connectors.crm: {provider: built-in-crm, tier: 0}`) providers are shipped credential-free
and do satisfy `kb` and `crm`; they hold no client credential, so they still need approved
content (an approved knowledge file; a person to review the contact log) and have no live proof.
Required capabilities remain blocked, optional ones degrade to the declared
fallback, and production deployment repeats the check even when it receives a
previously compiled desired-state document. A missing or invalid registry
fails closed. This gate verifies shipped adapter presence, **not** client
consent, a current access token, live provider health or completed acceptance
proof. Real OAuth and live proof are still tracked by
[#66](https://github.com/kingyx3/model-garden/issues/66) and
[#86](https://github.com/kingyx3/model-garden/issues/86).

## Enable or disable individual Skills safely

An Agent may list a Skill in `spec.skills` while keeping it off using `spec.disabledSkills`. Example:

```yaml
spec:
  skills: [answer-faq, take-message, book-appointment]
  disabledSkills: [book-appointment]
```

Disabled Skills remain in the non-secret readiness report but are **not** materialized into the Hermes runtime, do not contribute required capabilities, and suppress Tools used exclusively by disabled Skills. Tools shared with enabled Skills remain selected. Removing a Skill from `disabledSkills` is an activation: the ordinary required-capability validation then **fails closed** if its provider is missing. Keep direct Agent instructions aligned with what is enabled; disabling a Skill is not a grant/revocation of an underlying client provider credential.

### Knowledge freshness

A `text-file` KnowledgeSource may carry `reviewBy` (a `YYYY-MM-DD` date). The content may ground answers through that day; afterwards the source is `stale`: `kb.search` returns no documents with reason `source-stale` (it fails closed, the same as a draft), the dependent Skill reports `no-content` with the source's state, and its `remediation` asks the owner to confirm the content and set a later `reviewBy` before recompiling. An impossible date is treated as invalid and refused the same way; omitting the field means the source never goes stale. This checks the declared date, not whether the content is actually still true, and external documents and document-level permissions remain #116 work.

### Knowledge content readiness

A Skill that requires `kb` is ready only when the provider is bound **and** the Agent selects at least one usable KnowledgeSource: `status: approved`, an `effectiveDate` that has arrived, and an `audiences` list (if any) that names the Agent. Otherwise the readiness report, `client-operator.py skills`, the compiled `capabilities.skills` and the coworker's `platform.capabilities` show the dependency as `no-content` with each source's state (`draft`, `retired`, `not-yet-effective`, `effective-date-invalid`, `not-for-this-agent`, `not-indexed`) and the Skill is not ready (`canEnable: false`, `skills` exits non-zero). Compilation still succeeds so a draft can be authored and reviewed, and an *optional* `kb` dependency reports the states without blocking. States are non-secret names only; source text never appears. This checks declarations, not document-level permissions or freshness (#116).

**Remediation.** Every blocked Skill also carries one non-secret `remediation`: the action that unblocks it (connect the missing family, approve business knowledge for this coworker, or have the operator reauthorize a failed or revoked connection). It appears in `--readiness`/`client-operator skills`, the compiled `capabilities.skills` and the coworker's `platform.capabilities`, and it names families and commands only, never a secret, scope, account or source content. A Skill that is ready has none.

### Status consistency

The integration registry and versioned tests are the authority for implementation status. `python3 scripts/check-status-consistency.py` (run by CI and `make contracts`) fails when a doc calls a built provider planned or unimplemented, calls an unproven provider live, denies capabilities the reference receptionist does declare, or when the Tier 0 providers stop being `built-credential-free` at tier 0 with no live proof. It cannot directly validate Confluence pages. When the registry changes, run `python3 scripts/integration-directory.py --format markdown` and reconcile the [Confluence Integration Directory](https://modelgarden.atlassian.net/wiki/spaces/BA/pages/12189707) against its generated statuses; review the [target architecture](https://modelgarden.atlassian.net/wiki/spaces/BA/pages/11796484) and [receptionist reference](https://modelgarden.atlassian.net/wiki/spaces/BA/pages/1114125) only for misleading implementation claims. Never change `built-credential-free` to `live` without retained client/provider proof. The remaining automation work is tracked in [#131](https://github.com/kingyx3/model-garden/issues/131).

### Reference receptionist on a zero-stack client

The shipped receptionist declares `kb.search`, `crm.create` and `crm.log-interaction` (Tier 0, no third-party account) next to the calendar Tools. A client with no calendar sets `disabledSkills: [book-appointment]` and binds only `connectors.kb` (`text-knowledge`, tier 0) and `connectors.crm` (`built-in-crm`, tier 0); the calendar Tools are then suppressed and the bundle renders with no calendar provider. A client that later adds a calendar removes the Skill from `disabledSkills` and adds `connectors.calendar` (`google` or `microsoft`, tier 1); the Agent and the other Skills are unchanged. Unshipped providers (for example `hubspot`), forged tiers, or a failed connection keep the dependent Skill blocked. `tests/test_reference_compatibility.py` pins both scenarios, and `python3 scripts/eval-receptionist.py` also checks that each scenario's question retrieves the expected approved-knowledge section. The Tier 0 kb/crm path still has no live deployment evidence.

```bash
python3 scripts/client-operator.py skills <private-client-workspace> --environment dev --agent receptionist
```

This prints `ready`, `DISABLED` and `BLOCKED`, dependencies, and registry-backed `connect <family>` guidance without exposing tokens. An intentionally disabled Skill does not block the current deployment; enabling it without its required capability does. The `platform.capabilities` coworker response includes the same per-Skill `enabled`, `ready`, `canEnable` and dependency facts. When a connector is failed or revoked, live runtime health overlays mark dependent required Skills unavailable until a named operator reauthorizes the connection.

Tier 0 `kb` (a business-rules text file) and Tier 0 `crm` (the contact and interaction log) are implemented credential-free; full consent/provisioning (#110) and third-party CRM providers (#93) remain **unimplemented**. The reference receptionist declares both as dependencies and its Skills are ready on the Tier 0 bindings. Do not represent these capability checks as real external provider proof.

## Validate a workspace

```bash
python3 -m pip install -r requirements-dev.txt
python3 scripts/validate-workspace.py examples/workspace
```

The examples are intentionally small and should become the seed for a separate customer workspace template later.

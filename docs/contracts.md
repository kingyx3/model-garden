# Platform contracts

`contracts/v1/` is the compatibility boundary between the Model Garden platform, business-owned AI content and stable operational integrations.

Supported v1 business resources:

- `Agent` — composition of model profile, instructions, skills, tools and knowledge.
- `Skill` — reusable reasoning/instruction package.
- `ModelProfile` — logical model requirement mapped to approved candidates.
- `Tool` — executable enterprise capability with an explicit risk class.
- `KnowledgeSource` — approved source and data classification.
- `Policy` — client-owned approval rules, enforced by the governed boundary (see [Client approval policy](#client-approval-policy)); a dedicated policy engine remains out of scope.

**Shipped contract:** reusable `Skillset` catalogue packages group versioned Skills without granting Tool permissions or forcing activation. Each coworker explicitly selects enabled members; the compiler resolves dependencies and enforces independent readiness (details below). Business intent is defined in the [target architecture](https://modelgarden.atlassian.net/wiki/spaces/BA/pages/11796484).

## Capability contracts

`contracts/v1/capabilities.json` (validated by `capability-catalog.schema.json`) is the platform-owned catalog of provider-neutral Tool families: `chat`, `mail`, `calendar`, `files`, `docs`, `tickets`, `crm`, `code` and `kb`. Each portable Tool (for example `mail.send`, `tickets.create`) has argument and result schemas, a risk class (`read`, `write`, `privileged`, `external-communication`) and the argument fields that are safe to keep verbatim in the action ledger.

- `Agent` and `Skill` resources may declare `requiredCapabilities` and `optionalCapabilities`, naming a family (`calendar`) or a portable Tool (`calendar.book`). A required declaration on any selected Skill makes the capability required for the Agent.
- A `Tool` may opt in with `spec.capability: <family>`; its name must then be a portable Tool of that family and its risk must match the catalog. The reference `calendar.availability`, `calendar.book` and `crm.read` Tools do this with no behaviour change.
- Only the EnvironmentBinding chooses a provider, as `connectors.<family>.provider` (the legacy `connectors.google_calendar` / `microsoft_calendar` shapes map to `calendar`). Optional non-secret `tier` (0–4, default 1), `scopes` and `owner` describe the connection. **Implementation limit:** this is an environment-level binding, not a separately authenticated grant for each AI coworker. Distinct downstream provider identities, Agent-attested connection lookup and independently revocable scopes are required before multiple coworkers act autonomously in the same client system ([#129](https://github.com/kingyx3/model-garden/issues/129), [#95](https://github.com/kingyx3/model-garden/issues/95)). Never reuse a shared provider token as a substitute.
- `compile-workspace.py --environment-binding <file>` resolves declarations: a required capability with no provider fails closed with a message naming the Agent, capability and environment; an unbound optional one compiles and is reported under `capabilities.degraded` with its fallback tier (Tier 4, human hand-off, until built-in Tier 0 storage exists; the `handoff_request` platform Tool prepares a write action for a named person, see [Human hand-off](governed-tools.md#human-hand-off-tier-4)). Docker deployment re-runs the same check against the real binding.
- The fail-closed message also names the Skill(s) that require each missing capability. With a binding, the compiled output carries `capabilities.skills`: one entry per selected Skill (plus the Agent when it declares capabilities itself) with `ready` and each dependency as `bound` (provider, tier), `missing` or `degraded` (fallback tier). It copies no token references, calendar IDs, scopes or owners.
- `compile-workspace.py --environment-binding <file> --readiness` prints that per-Skill table instead of compiling and exits non-zero if any enabled Skill is blocked. Disabled Skills remain visible for planning but are not executable.
- The coworker's runtime `platform.capabilities` Skill view is recomputed from its **actual deployment binding** rather than trusting a potentially absent or stale compiler snapshot. To keep disabled Skills explainable, compiled desired state may carry `disabledSkillDeclarations`: only non-executable capability names, never Skill instructions or Tool grants. Legacy disabled Skills with no recorded dependency metadata are explicitly blocked pending recompilation. Health changes (including revocation) update readiness without granting disabled Tools.

The `crm` family also carries `crm.create` (risk `write`) and `crm.log-interaction` (risk `write`: channel, direction, timestamp, summary, outcome and a transcript or message *reference*). Their ledger fields are object type/ID and interaction type only, never summary or transcript text. Only the Tier 0 built-in log (below) implements these Tools; HubSpot, Salesforce and Zoho have no native Model Garden adapters; all external CRM routes are planned upstream integrations under #93. Making the internal-record `crm.log-interaction` automatic while `crm.create`/`crm.update` stay approval-gated is a client decision: leave `crm.log-interaction` without `approvalRequired` on its Tool, or add a signed `allow` rule (below).

All of this is additive to `modelgarden.ai/v1`: existing workspaces without declarations validate and compile unchanged.

## Tier 0 business-rules knowledge

A `KnowledgeSource` with `spec.type: text-file` points at one `.txt` or `.md` file of approved business rules (`spec.file`, relative to the resource, at most 256 KiB, UTF-8, confined to the workspace or library root) so a small client needs no knowledge system. Optional `status` (`draft` default, `approved`, `retired`), `version`, `effectiveDate` and `audiences` (Agents allowed to ground answers) travel with it; these fields are rejected on other source types.

- Compiling an Agent that selects the source parses the file as an upload test and materializes it into desired state under `knowledgeContent` (content, SHA-256, section count). An empty, unreadable, oversized or escaping file fails compilation. `python3 scripts/knowledge-check.py rules.md [--query ...]` runs the same parse/test on an upload and shows what a search would cite.
- `platform/core/knowledge.py` splits the file into stable numbered sections (Markdown headings, recognisable plain-text headings, or paragraph chunks) and searches them. `search_selected` returns the `kb.search` result shape: each document carries its section citation (`knowledge:<source>#<section-id>`), source SHA-256 and version, and the result is flagged `dataOnly` (retrieved text is reference data, never instructions). Only content that is `approved`, effective today and addressed to this Agent is returned; otherwise there are no documents and a reason (`source-draft`, `source-retired`, `source-not-yet-effective`, `source-not-for-this-agent`, `no-matching-section`) so the caller clarifies or escalates.
- Each client/environment searches only the content compiled into its own desired state. `read_receipt` turns a result into section IDs and content digests for the existing governed read event (`record_knowledge_read` / `execute_action(read_receipt=...)`), so the ledger never holds the text.

**Runtime and activation.** The governed sidecar exposes `kb_search` (the `kb.search` Tool) over this module. It runs only when the Agent selected the `kb.search` Tool (see `examples/workspace/tools/kb-search.yaml`) **and** the capability manifest shows `kb` bound to `text-knowledge` at Tier 0; otherwise it refuses with "not connected here". Each search is a governed read: the ledger records provider `text-knowledge`, `tier: 0` and the section IDs and content digests, never the text. A client binds it with `connectors.kb: {provider: text-knowledge, tier: 0}` in the EnvironmentBinding; the registry lists it as `built-credential-free` (adapter `platform/core/knowledge.py`), so a Skill requiring `kb` is ready and the readiness report shows `kb: text-knowledge tier 0`. A deployment that selects `kb.search` gets the governed sidecar even with no calendar bound, and that sidecar holds no provider credential. This is credential-free build evidence only: no live deployment has exercised it.

**Conflicts and instruction-like text.** When approved sections on the same topic state different prices, amounts or clock times (across sources or inside one), `search_selected` marks each document `conflict: true`, lists the differing values under `conflicts` and returns guidance not to choose one but to say the rules need confirming and escalate; wording differences with the same facts, other topics and sections with no amount or time never conflict, and a draft, retired, stale or other-Agent source cannot create one. `check_source` warns the owner about the same conflicts at upload. Text that reads like an instruction to its reader ("ignore your previous instructions", "reveal your system prompt", "new instructions:") gets `contentWarnings: ["instruction-like-text"]` and an upload warning; it is flagged, never obeyed, and the result stays `dataOnly`. These are heuristics for the owner's attention, not a security boundary, and not a substitute for the governed boundary that already refuses actions the coworker is not authorised to take.

**Not yet delivered:** document-level permissions, external knowledge providers, freshness/refresh policy beyond `reviewBy`, and semantic (non-numeric) conflict detection (#116, #77). The reference receptionist declares `kb` and `crm` and its evals exercise both Tier 0 paths (see [Reference receptionist on a zero-stack client](#reference-receptionist-on-a-zero-stack-client)).

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

**Ledger.** Every decision records the deciding rule ID as `policyRuleId` (or the built-in reason when no rule decided) and the rule-set version `policy:<digest>` as `policyVersion`. `govern-action.py`'s `record_policy_change(path, before, after, changed_by=...)` appends a chained `policy-change` event with the author and the rule-level diff, flagging rules that loosen control. The governed sidecar now calls it as it starts (`platform/core/policy_deploy.py`, `ledger_policy_deployment`): it compares the Policy rules in the compiled desired state it was deployed with against the rules it last applied (a private `policy-applied.json` beside the ledger) and appends one `policy-change` event when they differ, before it serves any request; an unchanged restart records nothing, and if the ledger cannot be written the sidecar does not start. The first start with no earlier record counts every current rule as added, so the trail begins with the rules the runtime started with. The identity recorded is `MODEL_GARDEN_DEPLOYED_BY` when the deploy sets it (a named person or workflow) and `runtime:deployment` otherwise; it is asserted by whoever runs the deploy and is not authenticated, so it names the person in the trail without proving who they are.

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

`scheduler.py`'s `BudgetMeter` halts a run that charges past its budget, ledgers `task.budget-exhausted` and notifies the owner (see "Running scheduled and triggered tasks").

### One budget across a delegated task tree

`delegation.delegate` bounds a child by what its parent has left, but alone it would let siblings each take all of it. `platform/core/task_budget.py` (`TaskBudgetTree`) makes the budget real across hops (#96). It is pure, credential-free and **not wired to a runtime** (#95).

- **Delegating reserves.** A parent's available budget is `budget - used - reserved`; each child's budget is reserved from it, so children of a task can never add up to more than it had. A child delegated without a stated budget for a key takes everything left for that key, so state the budget for every key to share it.
- **Charged where it happens, counted once.** Usage lands on the task that did the work; when a child finishes or halts its reservation is released and what it used is rolled into its parent, so `total_spent` is the cost of the whole tree.
- **Exhaustion halts the task and its descendants and tells the owner once.** Charging past a task's available budget records the overspend, ledgers `task.budget-exhausted` (task, chain, exceeded keys, budget, spend; never the goal), halts the task and everything under it, and notifies the originating principal (`task-budget-exhausted`). A sibling branch keeps running (what to do about a failed branch is #98's policy), but a branch whose overspend pushes its parent over halts the parent too. Spending exactly the budget is allowed. `timeline` shows state, budget, spend, reserved and available per branch, and `snapshot`/`restore` round-trip the tree.

Open: the runtime-native handoff or A2A decision, a multi-Agent runtime that calls these rules, and live evidence (#96, #95, #71).

## Coworker memory

`platform/core/memory.py` is the Model Garden contract for what a coworker remembers, a credential-free store with its own tests; it is **not connected** to Hermes's memory or to chat commands, and whether a runtime's native memory sits behind it is undecided (#100).

- **Scopes:** `client`, `agent:<name>` and `conversation:<id>`. A reader names the scopes it may see; anything else is invisible, so one conversation's memory never reaches another. A store belongs to one tenant, in its own file, and refuses another tenant's file.
- **Writes** redact card numbers, refuse `restricted` classification unless the deployment's policy allows it, are bounded (500 characters, 2,000 entries) and are idempotent per scope. Each write, delete, export and purge calls the audit hook with identifiers and a digest of the text, never the text.
- **People and administrators.** A person may list and delete entries they own; an administrator may export and purge per scope or for the whole client: `client-operator.py memory list|export|delete|purge --project-name <env>` (client data, so run it on the client host; the actor is as asserted on the command line, like approvals).

## Scheduled coworker tasks

`platform/core/schedules.py` is the contract for recurring work with a named owner ("send me a pipeline summary every Monday"). It is a credential-free store and cron arithmetic with tests, **not a scheduler**: nothing here wakes a coworker (the runner is `platform/core/scheduler.py`, below); chat commands are in `schedule_commands.py` (below) (#99). It also holds **event triggers**: an entry with an event kind (`mail.received`, `ticket.created` or `chat.message`) instead of a cron, with the same owner, budget and owner-only controls, a per-hour run limit (default 6, at most 60) and a digest of each handled event reference so an event never runs a trigger twice. `claim_due` and `claim_event` take a slot or an event atomically under the store's lock, `mark_outcome` counts consecutive failures (three suspend a schedule with `pausedReason: repeated-failures`) and a bounded, durable record of each run is kept for the runner.

- **Named owner and budget.** Every schedule belongs to one principal and must carry a per-run budget (`tokens`, `costCents` and/or `steps`, each at least 1). A run's task envelope comes from `delegation.root_envelope` with the owner as the originating principal, so its authority is the Agent's grants narrowed to the owner's permissions *at run time*; when the owner loses a permission the next run is refused (`ScheduleError`) instead of widened.
- **Cron-like, UTC.** Five fields with `*`, lists, ranges and `/step`; when both day fields are restricted either may match, as in standard cron. Fires closer than 15 minutes apart are refused. After downtime a schedule fires once for the slots it missed, and resuming after a pause never replays old slots.
- **Owner-only control.** Only the owner or an administrator pauses, resumes or deletes a schedule; at most 20 schedules per owner and 500 per tenant. One private file per tenant (another tenant's file is refused). Every change calls the audit hook with identifiers, the budget and a digest of the goal, never the goal.

### Running scheduled and triggered tasks

`platform/core/scheduler.py` makes each firing a task. It is credential-free and calls no provider (the coworker is an injected `runner`), and it is **not wired** to the sidecar, a clock or an event source: a deployment calls `run_due` on a timer and `run_event` when something arrives.

- **A task with a timeline.** A run is `<schedule id>:run-<n>`; it writes `task.start` and `task.finish` ledger events (identifiers, the goal's digest, budget, spend and outcome; never the goal, an event body or an error message) plus the human-step events below, and `timeline` reads one task's events back in order. Authority is built with `run_envelope` from the owner's *current* permissions, so an owner who lost a permission stops the run (`authority-refused`). An event reaches the runner only as its kind and a digest of its reference: it is data, never authority.
- **At most once.** The slot or event is claimed before the run starts, so two schedulers never run the same one and a restart never repeats it. A process that dies mid-run leaves the run `running` in the store (visible, not silently retried): a repeated side effect is worse than a late run. Events the coworker itself produced (`self_originated`) never trigger.
- **Budget halts and tells the owner.** The runner receives a `BudgetMeter`; charging past the per-run budget raises `BudgetExhausted` after recording the overspend, the run ends `failed` (`budget-exhausted`), `task.budget-exhausted` is ledgered and the owner gets a notice. Usage the runner reports at the end is charged the same way; spending exactly the budget is allowed.
- **Failures notify, repeated ones suspend.** A refused authority, runner error or exhausted budget produces one owner notice (`{key, kind, to, scheduleId, taskId, reason, text}` with no goal or error text, delivered through an injected `notify`; a failed delivery is returned as `delivered: false`, never lost) and counts toward three consecutive failures, after which the schedule is paused with a `schedule-suspended` notice.
- **Pausing on a person.** A runner may return `{"status": "waiting", "step", "assignee", "ask", ...}`; the run opens a durable Tier 4 hand-off (`human_step.py`) and ends the call. `resume_waiting` reads each waiting run's hand-off: the person's answer resumes the runner with their identity and note (data only; the envelope's authority does not change), a decline or timeout follows the fallback (`fail`, `skip`, or `escalate` once to a named person), a timed-out hand-off is closed so it does not linger in the inbox, and the opening, the person's answer and a timeout are ledgered in the task's timeline (the note only as a digest).

### Owner chat commands and workspace-managed schedules

`platform/core/schedule_commands.py` is the command layer a channel calls with the principal it verified (#99). One explicit syntax, no natural-language parsing: `schedules`; `schedule create <cron> | <budget> | <goal>`; `trigger create <event> | <budget> | <goal>`; `schedule pause|resume|delete <id>`. Anything else returns `None` and goes to the coworker. The owner is always the verified sender (the message cannot name another), the goal after the last `|` is data and never re-parsed as a command, an unverified sender gets nothing, and another person's id gets the same answer as an id that does not exist. Creating a schedule grants nothing: every run is still narrowed to the owner's permissions when it runs. It is not wired to a channel.

`ScheduleStore.reconcile` makes the store match a list of named definitions (`name`, `owner`, `agent`, `goal`, `cron` or `event`, `budget`, optional `requiredCapabilities` and `maxPerHour`), the client's desired state kept next to the workspace: a missing one is created, a changed one is updated in place (same id, same run history, an owner's own pause is kept), and a managed one no longer listed is **paused** with `pausedReason: removed-from-workspace`, never deleted, and comes back if listed again. Schedules made from chat are not managed and are never touched. Nothing is applied unless every definition is valid; a timed schedule cannot silently become an event trigger. `export_desired` writes the managed schedules back in the same shape. Changes are ledgered (`schedule.create`, `schedule.update`, `schedule.retire`) as identifiers and digests with who made them. A managed schedule an owner deletes is recreated by the next reconcile; the workspace wins. There is no workspace compiler or CLI step for this yet.

Open: delivery of owner notices and human steps to the person's own channel (`notice_delivery.py` is the runner; no real channel), wiring chat commands to a channel, a workspace file format read by the compiler or an operator command, a real event source, a runner backed by the governed boundary and live evidence (#99, #96, #97, #73).

## Human step in a task

`platform/core/human_step.py` lets a task step target a named person using the Tier 4 hand-off store, with no storage of its own: the pending hand-off is the durable pause, so a restart loses nothing, and the inbox, notices and `runtime-handoff.py complete` are how the person is told and answers. It is **not wired into a runtime** (no multi-Agent task runner yet, #95) (#97).

- **The step carries the exact ask** and a short context; notices carry only the Tool name and a reference, never the ask.
- **`resolution`** reports `waiting`, `resumed` (done, with the person's note and identity), `declined`, `timed-out` or `unknown`. A decline or timeout never resumes: it returns the configured fallback (`fail`, `skip` or `escalate` to a named person). Reopening the same step returns the same hand-off and does not reset its clock.
- **No widening.** The answer is data for the task; it never changes the envelope's authority.
- **Ledger.** `task.human-step.opened` (under the Agent), `task.human-step.resolved` (under the person) and `task.human-step.timed-out` (under the task) hold identifiers, the outcome, lateness and digests of the ask and note.

`scheduler.py` is the first runner that pauses a task on this step and resumes it (see "Running scheduled and triggered tasks"). Open: delivery to the person's own channel and acknowledgement from it, a multi-Agent runtime that uses it, and live evidence (#97, #88).

## CRM sync states

`platform/core/crm_sync.py` is a provider-neutral delivery ledger for filing interactions to a CRM; it contains no provider code and does not call one (#115).

- **Key.** A stable idempotency key from tenant, channel, external call or message id and interaction type, so a redelivered webhook, a retry and a restarted worker reach the same entry and never create a second record; a provider that supports idempotent writes is given the same key.
- **States.** `pending`, `synced`, `failed`, `blocked`, `reconciled`. `synced` and `reconciled` need the provider's reference, so nothing claims a filing the provider did not acknowledge. Transient errors (`rate-limited`, `outage`) retry after 30 s, doubling to 1 h; `rejected`, `unauthorized` or eight attempts block until a named person retries. `reconcile` links a record found at the provider without writing it again.
- **Readable.** `describe` returns the state, the provider reference when there is one, and a plain repair action; transitions are ledgered with the key, state, attempt count and error code, never interaction content.

Open: cross-channel conversation timelines, verified identity linkage, wiring into the voice and email filing paths and connectors, safe provider swap with a re-link plan, retention/erasure across providers and live evidence (#115, #108).

## Deliverables and the Tier 0 artifact store

`platform/core/deliverables.py` builds real DOCX, XLSX, PPTX and PDF files (and CSV) from structured Agent output with the standard library only, and `platform/core/artifacts.py` is the Tier 0 store they land in when the client has no file system bound. Both are credential-free modules with tests; they are **not yet behind** `docs.create`/`files.*` on the governed sidecar, not driven by a Skill, and they post no link into a channel (#101, #77).

- **Builders.** A report (DOCX or PDF) is a title plus sections of headings and paragraphs; a workbook is sheets of rows; a deck is a title slide plus slides of a title and bullets (text only, 16:9). Output is deterministic (same input, same bytes and digest). Spreadsheet strings are written as literal inline strings and CSV cells that start with `= + - @` are quoted, so text from an email cannot run as a formula; XML is escaped and control characters removed; sizes are bounded. The PPTX holds only text shapes and the PDF only text operators (Helvetica, A4, wrapped and paginated, non-Latin-1 characters become `?`): no hyperlinks, actions, JavaScript, embedded files or macros, and no dates or producer, so output stays deterministic. Tests parse the package and the PDF cross-reference table; the files were also opened in LibreOffice and poppler once by hand, which is not evidence from PowerPoint, Word or a client's PDF reader. Native Google/Microsoft formats are not generated.
- **Store.** One directory per tenant (0700, files 0600, an index for another tenant is refused). Files are named by artifact id, never by the display name. Only office, PDF, CSV, Markdown and text types, at most 10 MB and 2,000 artifacts; `restricted` data is refused unless the deployment's policy allows it. A read recomputes the SHA-256 and refuses a file that changed on disk.
- **Accountable.** Each artifact records who asked, which Agent produced it, a bounded list of source identifiers (for example `kb:rules-of-engagement#hours`) and its `tier0:<tenant>/<id>` location. `artifact.create`, `artifact.read`, `artifact.delete` and `artifact.expire` are ledgered with identifiers, size, digest and sources, never content or names.
- **People and retention.** The requester or an administrator reads or deletes; artifacts expire after the client's retention period (default 365 days) and `purge_expired` removes the files. `client-operator.py artifacts list|delete|purge-expired --project-name <env>` is the operator view (client data: run it on the client host; the actor is as asserted on the command line, like approvals).

Open: native Google/Microsoft formats, charts/images/styling, `docs.create`/`files.*` backing on the sidecar, links in the requesting channel, Skill-driven generation, evals through the runtime and live evidence (#101, #77).

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

`revoke_all` offboards one coworker (or the whole tenant) in one step, ledgering each revoked grant and returning the secret references the person offboarding must delete; see "Per-coworker offboarding" in the governed Tools document. Open: provisioning real per-coworker identities or grants, sidecar resolution from attested caller context, provider-side audit verification, real tenant proof per authentication mode (#129, #110, #86, #95).

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

**Not yet shipped — customer self-service custom integration (#159).** The existing descriptor/gating model is not a remote MCP connection wizard or generic REST/OpenAPI integration executor. The [client target](https://modelgarden.atlassian.net/wiki/spaces/BA/pages/327715) requires permissioned endpoint and operation review, constrained egress/SSRF checks, secure vault-backed OAuth/API-key enrollment, tenant/coworker-grant isolation, runtime approval/ledger and revoke/rotate. #87 and #89 own execution mechanics, #158 the common gateway, #110 guided onboarding and #159 the client-defined MCP/API experience. No caller may expose arbitrary HTTP requests as a Model Garden Tool, and no secret should enter a prompt or client workspace.

### Customer-managed MCP and custom API safety contracts

The independent endpoint/operation validation groundwork merged from #165 is preserved: `platform/core/endpoint_guard.py` rejects private, local, metadata and malformed HTTPS destinations and requires DNS answer validation/pinning; `platform/core/mcp_tool_review.py` blocks obvious tool-description/schema poisoning prior to operator approval; `platform/core/custom_api.py` defines explicit, versioned OpenAPI/REST operations with path/body bounds, method-derived risk and schema review. `mcp_passthrough.py` integrates endpoint and metadata checks into approval/exposure. These are **platform governance contracts** and intentionally contain **no customer-vendor HTTP client, arbitrary URL requester or runtime integration executor**. They support externally maintained MCP and client-owned APIs without resurrecting bespoke SaaS adapters. Network execution, per-coworker credential selection and live tenant evidence remain open.

## Upstream-only provider integrations (v0.8.0)

Model Garden does **not** implement or maintain vendor-specific REST clients. The former native Google/Microsoft Calendar, Gmail, Outlook, HubSpot, Salesforce and Google Drive modules and their dedicated transport/test helpers were retired in v0.8.0. The registry keeps those provider names **planned** with method `governed-mcp` and tier 2 so clients know the intended coverage; it does not claim that any external server is approved, connected or live.

- **Use maintained ecosystems instead of parallel connector implementations.** Discover candidates through the [official MCP Registry](https://registry.modelcontextprotocol.io/v0.1/servers?limit=100) and vendor-maintained catalogs. Evaluate official MCP servers first, then Activepieces or other maintained open-source/managed integrations for actual required operations. Do not create a second locally maintained catalog, sync job or OAuth client framework.
- **Reuse the existing governance boundary.** `platform/core/mcp_passthrough.py` already validates upstream HTTPS declarations, explicit Tool allow-lists, risk classifications, fingerprints and audit fields. It is **not** connected to live upstream execution; #158/#87 must wire one small governed MCP client via the standard SDK, per-coworker grant selection and owner OAuth, not duplicate this validation.
- **Gate activation.** Discovery alone grants no Tools. Pin reviewed operation schemas and compatible portable `mail.*`, `crm.*`, `calendar.*`, `files.*` mappings. Enforce per-coworker scopes/grants, approval, provider health, egress restrictions and runtime audit. A changed schema or missing operation fails closed. A provider becomes live only with retained real-tenant proof.
- **Custom client integrations.** Private MCP endpoints and OpenAPI/manual REST operations use the same protected gateway (#159) and never become a global public catalog entry.
- **Deliberate compatibility break.** Existing native Calendar deployments must keep their earlier pinned release until an independently validated upstream route is ready. This release does not silently reuse previous OAuth tokens or substitute a shared administrator credential. The compiler and deployer block unsupported Calendar skills.

See #157 (upstream candidate selection), #158 (execution), #159 (customer-defined connections), #129 (per-coworker identity), #86 (secrets and lifecycle), and #117 (real acceptance proof). The **only** built credential-free providers in the canonical registry are `text-knowledge` and `built-in-crm`; those are Model Garden Tier 0 features, not vendor connectors.

## Owner notices for connections and provider grants

`platform/core/connection_notices.py` works out who should be told when a connection or a per-coworker provider grant needs a person (#86). It sends nothing: a deployment delivers each notice through a channel or the inbox and then records it, as with hand-off notices. It is credential-free, not wired into the sidecar or a channel, and no owner delivery exists yet.

- **Conditions.** A capability connection that is `degraded`, `failed` or `revoked`, and a grant (#129) that is revoked, failed, degraded, expired or expiring within a warning window (default 7 days).
- **Who.** The connection's named `owner` from the EnvironmentBinding, or the grant's owner; with none, the escalation contact; with neither, the notice is still returned with `to: null` and `unowned: true`, so it cannot be silently dropped.
- **Once per state, at least once.** A notice has a stable key (connection or grant, condition, when the state began), is offered until delivery is recorded, and is not repeated for an unchanged state. A cleared condition is forgotten so a recurrence is told again. Only a notice the store offered can be marked delivered; `connection.notice-delivered` is ledgered with the key, kind and channel.
- **Non-secret.** Notices name the family or the grant's connection, the condition and a recovery step, never a reason string, scope, account, secret reference or the provider-side identity.

### Delivering notices to the named person's channel

`platform/core/notice_delivery.py` is the credential-free runner between these notices (and the Tier 4 hand-off notices, which carry human task steps) and a channel (#88, #97, #86). A small people directory maps a named person to one channel and one address (for a chat channel, the id of the conversation with that person; the directory refuses credentials and unknown keys). Each channel is an injected `send(address, text)`; `adapter_sender` wraps a channel adapter's `send_reply`. It holds no credential and is **not wired into the sidecar or a scheduler**; no real channel delivery has live proof.

- **The person's channel.** A person with no directory entry, or a channel with no registered sender, is reported `no-channel` and the notice stays due; `inbox` counts as a channel only when the person's entry says so, so a notice is never marked delivered because it was merely visible.
- **At least once.** Delivery is recorded after the send succeeds. A failed send records only the error type, leaves the notice due and does not stop the rest of the batch; a send whose recording then fails is reported `unmarked` and sent again next run rather than lost.
- **Unowned stays loud.** A notice with no recipient is reported `unowned` and ledgered every run.
- **Safe text.** Notices carry the Tool, deadline and reference, never the action's arguments. The ledger (`notice.delivered`, `notice.delivery-failed`, `notice.undeliverable`) records source, kind, reference, channel and error type, never the message text or the address.
- **Operator command.** `runtime-handoff.py <store> deliver --people people.json` runs it for hand-offs with the `inbox` channel only (it carries no transport); everyone else is reported `no-channel`.

Open: wiring this into the sidecar or a scheduler, real Slack, Teams and email transports, acknowledgement-based escalation, OAuth consent, token refresh and provider-side revocation (#86, #88, #97, #110).

## Cross-channel conversations and customer linkage

`platform/core/conversations.py` is the credential-free core of one coherent history across voice, email, chat and hand-off (#115). It is not a CRM, holds no message text and is **not wired to the voice or mail paths**.

- **Ids and references.** A conversation id (`cv-…`) is derived from the tenant, channel and the channel's own call or thread id, so redelivery and restarts reach the same one; an interaction's key comes from its kind and external id, so it is stored once. Interactions are *references* (`transcript:…`, `message:…`, a ledger `requestId`, a content digest, a short outcome label), never text, and carry the Agent, Agent version, task and trace ids. Late arrivals sort by when they happened, then by arrival.
- **Evidence, then a deliberate link.** Evidence (`email`, `phone`, `sso`, `name`) is kept as a digest of the normalised value with `verified` and a provenance; a name can never be verified. `resolve` asks an injected directory which customers hold each *verified* digest: exactly one makes a `CustomerLink`; two or more is `conflicting-evidence` and goes to the review queue; evidence that is only asserted but matches someone goes to the queue as `asserted-match` and never links itself; anything else stays unlinked. The state is not to be reflected to the caller. A named person confirms or dismisses a queued item and is ledgered.
- **One timeline per customer.** `customer_timeline` joins every conversation linked to a customer across channels in one ordered list; two different customers never share a timeline, and an unlinked conversation appears in none.
- **Private and erasable.** One file per tenant (0600 in a 0700 directory; another tenant's file is refused), customer erasure removes every linked conversation and its review items, conversations can be erased singly or purged by retention, and audit events hold identifiers only.

Open: wiring to the call transcript and mail paths and a real directory (the Tier 0 contact store or a CRM), `Conversation` ids in the CRM sync ledger, role-based access and redaction across exports, CRM replacement with a re-link plan, and live evidence (#115).

## Filing interactions through the sync ledger

`platform/core/crm_filing.py` runs the filing that `crm_sync.py` tracks: it takes the portable `crm.log-interaction` arguments and an executor (the Tier 0 contact log or an approved upstream executor, which share an `arguments -> result` shape) and records every attempt in the provider-neutral sync ledger. It contains no provider code, is credential-free, and is **not yet wired** to the voice or mail paths (#108, #115).

- **One entry per call or message.** The ledger key comes from the channel, the call or message id and the interaction type, so redelivery, a retry and a restart reach the same entry and nothing is filed twice. Tests replay the same interaction through the built-in log and an injected provider-neutral upstream executor and get the same states.
- **Success only on the provider's word.** A result without an `externalObjectRef` is a rejection. A result flagged `duplicate` means the record was already there (for example the first attempt landed and its answer was lost), so the entry is *reconciled* to the existing reference rather than written again.
- **Failures are sorted.** An error carrying a sync `code` keeps it; a `PermissionError` is `unauthorized`; a `ValueError` (bad arguments, an unknown contact) is `rejected` and waits for a person; anything else is a transient `outage` that backs off and, after the ceiling, blocks.
- **No content in the ledger.** `retry_due` takes a `payloads` callable for the interaction to retry (a deployment reads it from its transcript or message store); an entry whose payload is unavailable is skipped and counted, never invented.

Open: wiring this to the voice transcript and support-mailbox paths, a payload store, real CRMs, cross-channel timelines and identity linkage, and live proof (#108, #115).

## Guided connect step

`platform/core/connect.py` is the credential-free core of connecting a CRM, mailbox or calendar for one named coworker (#110). The operator provisions the provider-side identity and secret elsewhere and passes only the `secret://` reference; this module runs no owner OAuth consent, stores no secret, makes no provider call itself (the read-only test is injected) and is not wired to the interactive CLI.

- **Plan.** For a named Agent, client owner and operator, the plan takes its scopes from the registry's reviewed mapping for the selected portable Tools and refuses a request for more. It carries a digest of everything the owner consents to; `apply` needs that digest back, so consent to one plan cannot be reused for another. Providers that are not shipped, and Tier 0 providers (which need no credentials), are not connectable and the plan says why. A mailbox is `shared` or `delegated`; a `personal` one needs an explicit recorded owner exception.
- **Apply.** The read-only connection test runs first. If it fails nothing is recorded and the failure is ledgered; on success the coworker's own grant is recorded (`provider_grants.py`, so a second coworker never inherits the first one's credential) and the binding fragment (provider, tier, scopes, owner, never a secret) is returned for `merge_binding`, which leaves the input untouched, keeps a live `token_ref` for the same provider and makes replacing a provider explicit.
- **Ledger.** `connection.connect`, `connection.connect-failed` and `connection.disconnect` name the operator and the client owner with identifiers, never a credential, reference or account. `disconnect` revokes one coworker's grant and affects no other.

- **Changing a working connection.** `merge_binding` refuses, unless the caller passes `rescope`, to widen the scopes of a binding that already lists scopes or to change its owner, in addition to the existing explicit `replace` for a provider; `describe_change` shows exactly what a fragment would change. A legacy binding with no `scopes` key is narrowed, not widened, by naming scopes. `replace` moves a coworker to a new grant without leaving it with none: the test and the new grant first (a failure changes nothing), then the injected binding write, then revoking the previous grant. If the write fails the new grant is revoked, the previous grant stays in force and `connection.rolled-back` is ledgered; success ledgers `connection.replaced` with both grant ids and says if revoking the old grant failed (`previousRevoked: false`).

Open: owner OAuth consent and token acquisition, credential storage, a live test, re-running Skill readiness after a change, and the interactive CLI (#110, #86, #129).

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
and an implemented Tier 0 local backend (or, in future, a separately vetted, authorized upstream executor) satisfy a dependency. Currently **only** `text-knowledge` and `built-in-crm` qualify; historical Calendar aliases do not activate retired integrations.

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

## Provider evidence manifest and go-live gate

Whether a provider may be called live is shown, not inferred. `python3 scripts/provider-evidence.py` (`--format markdown` for review) emits one entry per registry provider: family, tier, method and status; the adapter path and its SHA-256; the automated test files that load that adapter and how many tests they hold (a count of tests in those files, so a broad file is over-counted); the reviewed scope set; the retained live tenant proof and its state; the limitations that still apply; and `goLive`, whether a go-live claim is allowed and every blocker. A go-live claim needs a shipped adapter (`built-credential-free` or `live`), automated adapter tests and, for any provider that reaches a third-party tenant (tier 1 and above), a **valid, unexpired proof for the current adapter** with scopes equal to the reviewed set, and the registry must itself say `live`. A Tier 0 provider reaches no third-party tenant, so it needs tests but no tenant proof and keeps the limitation that no deployment evidence is retained. `provider-evidence.py --go-live <provider>` exits non-zero with the blockers.

Live proof is a retained, redacted file `assurance/provider-proof/<provider id>.json` (the registry's `proof` field names it for a `live` provider) with `schemaVersion`, `provider`, `provedAt`, `expiresAt`, `adapterSha256`, `scopes`, `recordedBy`, `evidence` (run ids or artefact names, never content), `redacted: true` and optional `limitations`. Any other key (a tenant identifier, token or content) makes the proof invalid. A proof is bound to the adapter it proved: changing the adapter file invalidates it until the provider is proven again, and an expired proof blocks go-live without breaking unrelated work. `provider-evidence.py --check` (run by `make contracts` and CI) fails only when a registry claim is untrue now: a `live` provider with no valid proof file, a malformed proof, or a proof for an unknown provider. No provider is live today and no proof file is retained, so every tier 1+ provider is blocked. Producing a proof needs a real tenant and a person; the manifest does not call a provider.

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

### Readiness that checks the coworker's own grant

With a per-coworker grant registry present for the environment (`<governance root>/provider-grants/<client>-<environment>.grants.json`, see "Per-coworker provider grants"), `platform/core/grant_readiness.py` overlays it on the sidecar's `platform.capabilities` so a *connected* Skill is ready only when its own Agent holds a usable grant for the bound provider that covers the Tools it selected. A bound Tier 0 dependency is untouched. A required dependency is otherwise `no-grant` (this Agent holds no grant for that provider; another coworker's grant never counts), `grant-unavailable` (every such grant is revoked, expired or failed) or `grant-scope` (the grant lacks selected Tools, which are named), and the Skill is not ready with a one-line `remediation` (provision this coworker's own grant, renew it, or re-scope it); an optional dependency degrades to its fallback tier instead. A healthy grant adds `grant: {state: active|degraded, isolation}` and nothing else: no identity, connection id or secret reference. Scopes are checked per family because the manifest records the Tools the Agent selected per family, not per Skill, so the check errs toward blocking. With no registry the sidecar behaves as before; a registry that exists but cannot be read (or belongs to another tenant) blocks connected Skills rather than being ignored. This is readiness only: Tool calls still use the environment binding until the sidecar resolves grants from attested caller context (#129), and there is no live proof.

### Knowledge change trail

The governed sidecar fingerprints the KnowledgeSources in its compiled desired state (declared fields and the content digest, never the text) and, when it differs from the fingerprint it last applied (a private `knowledge-applied.json` beside the ledger), appends one chained `knowledge.change` operator event before serving: which sources were added, removed or changed and in which fields, the version and first 16 characters of the digest before and after, who deployed it (`MODEL_GARDEN_DEPLOYED_BY`, asserted not authenticated), and the critical changes by name (`newlyApproved`, `approvedContentChanged`, `audienceWidened`, `retired`). A draft being edited is recorded but not critical. An unchanged restart records nothing, the event is written before the new fingerprint is remembered, and a ledger failure stops the sidecar from starting. When the EnvironmentBinding names `governance.owners`, compilation also refuses an `approved` text source whose `metadata.owner` is not on the list (an empty list refuses every approved source); that narrows who may claim ownership of business rules but does not authenticate them. Evals re-run before promotion and rollback are not done (#116).

### Status consistency

The integration registry and versioned tests are the authority for implementation status. `python3 scripts/check-status-consistency.py` (run by CI and `make contracts`) fails when a doc calls a built provider planned or unimplemented, calls an unproven provider live, denies capabilities the reference receptionist does declare, or when the Tier 0 providers stop being `built-credential-free` at tier 0 with no live proof. It cannot directly validate Confluence pages. When the registry changes, run `python3 scripts/integration-directory.py --format markdown` and reconcile the [Confluence Integration Directory](https://modelgarden.atlassian.net/wiki/spaces/BA/pages/12189707) against its generated statuses; review the [target architecture](https://modelgarden.atlassian.net/wiki/spaces/BA/pages/11796484) and [receptionist reference](https://modelgarden.atlassian.net/wiki/spaces/BA/pages/1114125) only for misleading implementation claims. Never change `built-credential-free` to `live` without retained client/provider proof. Any Confluence status review should stay lightweight and use the authoritative registry, not a second integration catalog or sync job. The remaining status consistency work is tracked in [#131](https://github.com/kingyx3/model-garden/issues/131).

### Reference receptionist on a zero-stack client

The reference receptionist declares `kb.search`, `crm.create` and `crm.log-interaction` (Tier 0, no third-party account) alongside optional booking definitions; its booking Skill must remain disabled until an upstream route is approved. A client with no calendar sets `disabledSkills: [book-appointment]` and binds only `connectors.kb` (`text-knowledge`, tier 0) and `connectors.crm` (`built-in-crm`, tier 0); the calendar Tools are then suppressed and the bundle renders with no calendar provider. A client can later remove the Skill from `disabledSkills` only after an approved upstream calendar route is proven and bound (planned Tier 2); the Agent and other Skills need no rewrite. Unshipped providers (for example `hubspot`), forged tiers, or a failed connection keep the dependent Skill blocked. `tests/test_reference_compatibility.py` pins both scenarios, and `python3 scripts/eval-receptionist.py` also checks that each scenario's question retrieves the expected approved-knowledge section. The Tier 0 kb/crm path still has no live deployment evidence.

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

## First-class Skillsets

A `Skillset` is a versioned, credential-free catalogue resource (`kind: Skillset`, `metadata.name/owner/version`, `spec.skills` and optional `spec.includes`/`spec.provenance`). Agents pin it under `spec.skillsets: [{name: front-office, version: 1.0.0}]`. Selection **does not** enable any bundled Skill: `spec.enabledSkills` explicitly names active bundled members. Legacy direct `spec.skills` remain enabled unless in `disabledSkills`. Each compiled Agent expands bundles independently and exports a non-secret `skillSelection` summary. Missing/pin-conflicting/cyclic bundle references are errors; disabled members have no executable instructions or exclusive Tools. Bundles cannot introduce connector credentials, Agent-level Tools, or permissions. Changes follow the normal reviewed workspace PR, evaluation and deployment approval path; a prior revision can be redeployed to roll back.

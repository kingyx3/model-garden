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

**Owner sign-off, checked at compile and deploy time.** An `allow` rule that relaxes an approval requirement (it matches a non-read Tool, or names `public`) must carry `signoff: {owner, ruleDigest}`. `ruleDigest` is the SHA-256 of the rule's canonical JSON without `signoff`, so editing the rule invalidates the sign-off and compilation fails, printing the digest to re-sign, until the named owner approves the new content. The runtime re-checks the digest before it honours an `allow`. Protect the sign-off lines with `CODEOWNERS` in the client repository so only the named client owner can change them; the platform checks that a sign-off exists and matches the rule, not who typed it.

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

### Knowledge content readiness

A Skill that requires `kb` is ready only when the provider is bound **and** the Agent selects at least one usable KnowledgeSource: `status: approved`, an `effectiveDate` that has arrived, and an `audiences` list (if any) that names the Agent. Otherwise the readiness report, `client-operator.py skills`, the compiled `capabilities.skills` and the coworker's `platform.capabilities` show the dependency as `no-content` with each source's state (`draft`, `retired`, `not-yet-effective`, `effective-date-invalid`, `not-for-this-agent`, `not-indexed`) and the Skill is not ready (`canEnable: false`, `skills` exits non-zero). Compilation still succeeds so a draft can be authored and reviewed, and an *optional* `kb` dependency reports the states without blocking. States are non-secret names only; source text never appears. This checks declarations, not document-level permissions or freshness (#116).

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

# Governed Tools runtime

Model Garden exposes supported business Tools to Hermes through standard MCP while keeping authorization, connector credentials and audit outside the model process.

## Runtime shape

```text
Hermes / model
  model credential only
  hermes-data only
       |
       | MCP (internal Docker network)
       v
governed-tools sidecar
  selected connector credential only
  governance-data only
       |
       +-> Model Garden allow / approval / deny
       +-> versioned tamper-evident JSONL audit
       +-> approved connector
```

The sidecar is not a control plane and is not exposed on a host port. It is part of the same isolated client Docker deployment and consumes the same compiled single-Agent desired state. Persistent approval and audit state is mounted only into the governed Tool sidecar; Hermes does not mount that governance volume and therefore cannot create approval evidence by writing directly to the file-backed operator store.

Hermes only receives MCP Tool names corresponding to Tools explicitly selected by that Agent. Provisioning fails closed if a selected Tool has no implemented governed runtime binding.

## Client-stack-neutral Calendar integration (Google Workspace / Microsoft 365)

The *Agent* always selects the same portable Tools:

- `calendar.availability`: read free/busy state.
- `calendar.book`: create an event only after the configured exact-action approval.

Only the **EnvironmentBinding** chooses the provider. Do not fork Agents or Skills by cloud/office suite. Each isolated client/environment can use Google or Microsoft independently while keeping the same audit and approval contract.

Preferred Google Workspace binding:

```yaml
environment: dev
runtime:
  profile: receptionist
  model_credential_ref: secret://model/dev
connectors:
  calendar:
    provider: google
    token_ref: secret://google/dev-calendar
    calendar_id: primary
```

Preferred Microsoft 365 binding:

```yaml
environment: dev
runtime:
  profile: receptionist
  model_credential_ref: secret://model/dev
connectors:
  calendar:
    provider: microsoft
    token_ref: secret://microsoft/dev-calendar
    mailbox: receptionist@client.example
```

The older `connectors.google_calendar` shape remains accepted for existing clients. Do not define multiple calendar provider bindings: deployment rejects ambiguity or unknown providers. The `MODEL_GARDEN_RUNTIME_SECRETS_JSON` for the protected `dev`/`prod` GitHub Environment contains **exactly** the referenced logical keys and operator-supplied runtime tokens. Model credentials are injected into Hermes only; Google/Microsoft Calendar credentials go only to the private governed Tool sidecar. Neither API keys nor tokens belong in Agent, Skill or workspace YAML.

### Authorization responsibility and current support

| Concern | Google Workspace | Microsoft 365 |
| --- | --- | --- |
| Implemented API | Google Calendar freeBusy + event creation | Microsoft Graph v1.0 getSchedule + event creation |
| Mailbox/calendar target | Configured calendar ID | Explicitly approved mailbox email |
| Authority | Scoped Google OAuth access token | Scoped Entra/Graph OAuth access token |
| Consent | Client owns account/consent; Model Garden handles setup | Client owns account/consent; Model Garden handles setup, Entra admin where needed |
| Read permission | Google Calendar free/busy scope or approved equivalent | Graph `Calendars.ReadBasic` (when supported by selected flow) or approved equivalent |
| Write permission | Calendar event-write permission | Graph `Calendars.ReadWrite` |
| Connector boundary | Fixed Google API host | Fixed `graph.microsoft.com` Graph v1.0 host |

**Credential boundary (v0.6.1):** Both provider adapters still accept the existing single bearer-token secret for backwards compatibility. An operator may instead place a **JSON-encoded OAuth refresh-credential bundle as the value of the same environment-scoped** `token_ref`, never in Git or Agent/Skill content. Format (placeholders only; do not commit or paste values into docs):
```json
{"type":"oauth2_refresh","provider":"google","client_id":"<approved-oauth-client-id>","client_secret":"<secret>","refresh_token":"<client-authorized-refresh-token>"}
```
For Microsoft use `"provider":"microsoft"` and add `"tenant_id":"<specific-client-tenant-GUID>"`. The governed sidecar exchanges that credential only against Google's fixed OAuth token endpoint or the specified Microsoft Entra tenant token endpoint. Short-lived access tokens are cached in the sidecar's memory and refreshed before expiry; neither refresh credentials nor access tokens are included in Agent state or audit. Legacy static bearer tokens still expire and are **not production-stable**.

**Still a P0 go-live limitation:** The platform does not conduct OAuth user consent, create provider applications, securely persist **rotated refresh tokens**, perform tenant-side token revocation, guarantee long-lived delegated grants, or provide owner reauthorization/alert delivery. Provider credentials must be provisioned and scoped by the client/consultant; integrate a durable secret/refresh-token rotation and reauthorization lifecycle before unattended live use. See [#66](https://github.com/kingyx3/model-garden/issues/66) and [#86](https://github.com/kingyx3/model-garden/issues/86). Never equate token-refresh unit tests with live authorization proof.

Microsoft Graph sends invitations when creating events with attendees; approval policy and client consent must cover that external side effect. The adapter normalizes timezone-aware RFC3339 arguments to UTC for Graph. Tentative, out-of-office and unknown schedule statuses block availability.

Other Google/Microsoft capabilities described in Confluence (Gmail/Outlook, Drive/SharePoint, Teams, OneDrive) are **target architecture only**, not implemented governed connectors. New providers should be narrow, audited and tested instead of granting an Agent raw Graph/Google API authority.

## Capability manifest

Deployment writes a non-secret capability manifest per environment (`profile/.modelgarden/capability-manifest.json` in the Docker bundle). For every catalog family it states the status (`available`, `not-granted`, `unbound`, or `unavailable` once a connection has failed or been revoked), provider, tier, granted scopes, connection owner, health and fallback. It carries no token references, calendar IDs, mailboxes or tenant identifiers beyond the client and environment name.

The governed sidecar exposes it to the coworker as the read-only `platform.capabilities` MCP Tool (`platform_capabilities`), so "what can you do here?" is answered from what is actually connected. It is a platform built-in rather than a business Tool, so it bypasses Tool selection and is not ledgered; Hermes receives it whenever the governed sidecar is present. The sidecar is present when the Agent selects a calendar Tool (with its calendar binding) or a Tier 0 Tool (`kb.search`, `crm.read`, `crm.create`, `crm.log-interaction`); a deployment with none of them has no sidecar.

Connection health is live, not baked into the deployment. The sidecar records `healthy` after a successful connector call, `failed` after a provider 401/403 and `degraded` after other provider errors. Operators can record a revocation or recovery without redeploying Agents:

```bash
python3 scripts/client-operator.py connection calendar revoked \
  --project-name acme-prod \
  --actor operator@example.com --reason "consent withdrawn"
python3 scripts/client-operator.py connections --project-name acme-prod
```

The next `platform.capabilities` read reports the change. **Runtime enforcement:** a connection recorded as `failed` or `revoked` also denies subsequent governed Calendar reads and writes, including those with an earlier approval, and records a redacted denial in the chained action ledger. Only an authorized operator can mark a reauthorized connection healthy; in-flight API responses cannot clear a revocation. Marking a connection `revoked` is a **local execution kill switch**, not a substitute for revoking the underlying client OAuth grant/provider credential or clearing any pending human approvals. The operator must also revoke the upstream grant and rotate/delete the protected secret under the client offboarding runbook. Do not equate a local status update with external revocation. The Receptionist eval suite checks that an unbound capability is reported with its human hand-off fallback and that the instructions tell the coworker to decline and offer that fallback instead of claiming success.

## Exact-action approval

An approval-required Tool call returns `pending-approval` and persists the exact request material on the sidecar-only `governance-data` volume. The request ID binds Agent, Tool, exact arguments, initiating identity and bounded first-party governance context.

List pending requests from the Docker host:

```bash
python3 scripts/client-operator.py approvals \
  --project-name acme-prod
```

Approve one exact request:

```bash
python3 scripts/client-operator.py approve <request-id> \
  --project-name acme-prod \
  --approver operator@example.com
```

Or reject it:

```bash
python3 scripts/client-operator.py reject <request-id> \
  --project-name acme-prod \
  --approver operator@example.com
```

Whether a call needs approval is the stricter of the Agent switch, the Tool's `approvalRequired` and the client's `Policy` rules; see [Client approval policy](contracts.md#client-approval-policy) for precedence, the public-principal floors and owner sign-off. Channel approvals already require a mapped `approver`, so a public caller cannot approve their own request.

Changing the Tool arguments or bounded governance context creates a different request ID, so an approval cannot authorize a materially different action. Approval decisions are also single-use: once an approved or rejected decision is applied, it is consumed. A later identical material action must pause for a fresh human decision rather than replaying an earlier approval.

This file-backed approval mechanism is intentionally an MVP operator surface. Do not build an approval portal or workflow engine until repeated client evidence justifies one.

## Human hand-off (Tier 4)

When an action cannot run here, the coworker prepares it for a named person instead of failing or pretending. Two triggers:

- **Unbound or unavailable capability.** The platform Tool `handoff_request` takes the exact portable Tool name and arguments of a **write** Tool the Agent selected. It is accepted only when `platform_capabilities` shows that family as `unbound` or `unavailable` with fallback `human-handoff`; otherwise the call is refused ("call the Tool directly"). Reads cannot be handed off. A revoked or failed connection still denies direct Tool calls exactly as before; `handoff_request` is the explicit alternative.
- **Policy `human-only`.** A matching `human-only` rule (which must name an `assignee`, optionally `deadlineHours`) forbids automation: the direct Tool call returns `handed-off` and nothing executes, and no approval can change that. Deny rules still win; public principals cannot reach a write Tool this way.

The hand-off holds the same exact request material that approvals bind to (so it is as sensitive as a pending approval), is stored on the sidecar-only volume at `/opt/data/.modelgarden/handoffs`, and is created idempotently per exact request. The assignee is `MODEL_GARDEN_HANDOFF_ASSIGNEE` or the policy rule's `assignee`; with neither, the call fails closed.

```bash
python3 scripts/client-operator.py handoffs --project-name acme-prod
python3 scripts/client-operator.py handoff-done <id> --project-name acme-prod --by jordan@client.example
python3 scripts/client-operator.py handoff-decline <id> --project-name acme-prod --by jordan@client.example --note "not our policy"
```

Creating and closing a hand-off each write a chained `handoff` ledger event with `tier: 4` and `provider: human`; closing records the person's identity and `late` when the deadline passed. As with approvals, that identity is what the operator command asserts (`--by`); the platform does not verify that the person is the assignee or independently confirm that they performed the action. The coworker learns the outcome only through `handoff_status`, whose message says the action has **not** been done while pending, names the person who confirmed it when done (and that the coworker did not do it itself), and says it was **not** done when declined. The receptionist instructions and the `unavailable-action-handoff` eval require that wording.

Delivery today is the built-in inbox (the operator command above). Delivery to Slack, Teams or email, reminders/escalation when a deadline passes, and a person's confirmation arriving through their own channel are not implemented; the operator records the outcome on the person's behalf.

## Enterprise audit contract

Governed material actions emit versioned JSONL records with `schemaVersion: 3`. Depending on the stage, records capture:

- timestamp and event type (`authorization`, `approval`, `execution`);
- decision/outcome;
- immutable governed-action `requestId`;
- Agent and Tool;
- initiating user/workload when available;
- bounded governance context such as tenant, environment, deployment, session/conversation, runtime/model provenance and correlation ID;
- optional commercial attribution context such as cost center, billing subscription and shared consumption pool;
- approver identity for approved actions;
- result type plus a digest of the result rather than the full connector response;
- failure type without copying exception messages that may contain sensitive provider data;
- monotonically increasing sequence, previous-event hash and current-event SHA-256 hash.

Schema v3 adds the fields needed to answer "what did the AI do and why" without trusting the AI's narrative:

- **Arguments redacted plus digest.** `arguments` keeps only fields the capability catalog marks safe (for Calendar: `start`, `end`, `timezone`); everything else, including summaries, descriptions and attendees, is `"[redacted]"`. `argumentsDigest` is the SHA-256 of the exact canonical arguments, so an incident reviewer holding the original request can prove a match. Tools outside the catalog have every argument redacted.
- **Authority fields in `governance`** (bound into the `requestId`, so an approval cannot be replayed under a different authority): `originatingPrincipal`, `channel`, `triggerRef`, `taskId`, `parentRequestId` and `delegationChain[]` (1–16 hops).
- **Descriptive fields in `ledger`** (not bound into the `requestId`, so a reworded justification or a new release never invalidates a pending approval): `agentVersion` (digest of the compiled desired state), `workspaceRevision` (`MODEL_GARDEN_WORKSPACE_REVISION`), `platformRelease` (`VERSION`), `policyRuleId` + `policyVersion` (the governance rule that decided), `justification` (the model's stated reason, whitespace-collapsed and capped at 500 characters), `capability`, `provider`, `tier` and `externalObjectRef` (for example `google-calendar:event/<id>` on a successful booking).

Every new field is allow-listed and length-bounded; anything else (prompts, transcripts, credentials, chain-of-thought) is rejected. `verify_audit_chain()` verifies v1 prefixes and v2/v3 chained records together without rewriting history, and rejects a chain whose schema version moves backwards.

### Read events

Successful `read`-class Tool calls and `kb.search`-style knowledge reads (`record_knowledge_read()`) emit an extra `read` event recording what the coworker knew: source ID, classification, record/document IDs and per-record content digests, never content. Read events carry the same `governance` block (`taskId`, `correlationId`) as the writes that follow, so a timeline can join them.

Volume is configurable per client with `audit.reads` in the EnvironmentBinding (passed to the sidecar as `MODEL_GARDEN_READ_AUDIT`):

- `full` (**default**): one read event per read call, with up to 100 record IDs (`recordsTruncated` marks the cap) plus `recordCount` and a digest over the full list;
- `aggregate`: record IDs are omitted; `recordCount` and the list digest remain;
- `sampled:<N>`: read events for about 1 in N calls, chosen deterministically from the `requestId`.

Sources classified `restricted` are always recorded with every record ID, whatever the mode. The `execution` event for every call is recorded regardless of read sampling.

Audit appends use an exclusive filesystem lock, flush plus `fsync`, a restrictive `0600` file mode and a `0700` parent directory. `verify_audit_chain()` checks ordering and the SHA-256 chain and supports a legacy schema-v1 prefix without rewriting historical records. Modification, deletion from the middle, reordering or insertion of chained records is therefore detectable from the retained chain/head hash.

This is **tamper-evidence, not immutable storage**. An administrator with host/storage authority could still replace or delete the complete local log and its head. Enterprise deployments that require independent non-repudiation or longer retention should continuously export governed-action records/head hashes to a separately administered immutable log/SIEM/object store according to the client retention policy.

The bounded context intentionally rejects arbitrary fields so prompts, raw credentials and uncontrolled application payloads cannot be smuggled into the governance envelope.

### Ledger export and verification

`scripts/ledger-export.py` (library: `platform/core/ledger_export.py`) copies the chained records and head manifests to a sink that the runtime identity should not be able to rewrite:

```bash
python3 scripts/ledger-export.py export --ledger /opt/data/.modelgarden/audit.jsonl \
  --state /var/lib/model-garden/export-state.json --sink-dir /mnt/ledger-sink
python3 scripts/ledger-export.py export ... --webhook https://siem.example/ingest --token-env SIEM_TOKEN
python3 scripts/ledger-export.py verify --ledger /opt/data/.modelgarden/audit.jsonl --sink-dir /mnt/ledger-sink
```

- **Delivery:** at-least-once. The record ID is the event's own chain hash, so a duplicate delivery is harmless and a different record under an existing ID is an error. The cursor only advances after the sink acknowledges a batch.
- **Sinks:** an append-only directory layout (files created exclusively, never rewritten; mount it where a different identity controls writes) and a generic HTTPS collector with an `Idempotency-Key` per batch. The collector is write-only, so `verify` needs the directory sink. A retention-locked object-store sink is not implemented.
- **Verification:** recomputes the chain from the exported records alone and reports gaps in the exported sequence, a local log that is shorter than the export (truncation) or that differs from it (replacement with another valid chain), tampered exported records and a head manifest that disagrees with its record. It prints `ALERT:` lines and exits 2; routing that to monitoring is deployment-specific.
- **Not proven:** nothing is scheduled or deployed by default, and the separate export identity and retention lock (Terraform) are not provisioned. Until a client environment runs this against a separately administered target, the ledger remains tamper-evident, not independently immutable.

### Audit-to-cost correlation

Audit and billing remain separate systems with a stable join key. A usage event may include:

- `action_request_id` = the governed Tool `requestId`;
- `trace_id` = the same safe runtime/call correlation identifier used in governance context;
- client, Agent, environment, provider, cost center, billing subscription and shared-pool dimensions where applicable.

This enables enterprise showback and incident reconstruction without placing pricing logic in the Agent runtime or copying prompts/transcripts into the billing system. See `docs/metering.md` and `contracts/v1/usage-attribution.schema.json`.

A shared consumption pool is only a commercial/accounting grouping. It never weakens per-Agent action audit, approval rules or client/environment isolation.

## Current evidence boundary

Credential-free tests cover capability resolution and the manifest, ledger v3 bounds and redaction, read-event digests, channel principal mapping, Tool selection, MCP materialization, governance, versioned action audit, audit-chain tamper detection, approval identity, single-use approval behavior, bounded cost-attribution context, governance-volume isolation, Calendar connector behavior and Docker credential isolation. Metering tests cover canonical client/Agent/environment/provider attribution, shared-pool/subscription dimensions and rejection of obvious sensitive billing properties.

A real Google Calendar credential-backed end-to-end booking, external audit export/retention where required, and production reconciliation from provider usage -> Lago -> customer subscription remain live acceptance gates and must not be claimed complete until exercised in a deployed client environment.

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

**Operational limitation:** The shipped adapter consumes an authorized access token supplied at deployment. It does not perform OAuth user consent, token renewal, service-principal credential rotation, Entra app registration, or Google OAuth app creation. Normal production operation therefore requires a separately managed valid token lifecycle or a subsequent reviewed token-provider integration. Do **not** treat a manually supplied short-lived OAuth access token as an unattended, production-stable connection. Confirm real tenant authorization, refresh/revocation and live read/write actions before go-live.

Microsoft Graph sends invitations when creating events with attendees; approval policy and client consent must cover that external side effect. The adapter normalizes timezone-aware RFC3339 arguments to UTC for Graph. Tentative, out-of-office and unknown schedule statuses block availability.

Other Google/Microsoft capabilities described in Confluence (Gmail/Outlook, Drive/SharePoint, Teams, OneDrive) are **target architecture only**, not implemented governed connectors. New providers should be narrow, audited and tested instead of granting an Agent raw Graph/Google API authority.

## Capability manifest

Deployment writes a non-secret capability manifest per environment (`profile/.modelgarden/capability-manifest.json` in the Docker bundle). For every catalog family it states the status (`available`, `not-granted`, `unbound`, or `unavailable` once a connection has failed or been revoked), provider, tier, granted scopes, connection owner, health and fallback. It carries no token references, calendar IDs, mailboxes or tenant identifiers beyond the client and environment name.

The governed sidecar exposes it to the coworker as the read-only `platform.capabilities` MCP Tool (`platform_capabilities`), so "what can you do here?" is answered from what is actually connected. It is a platform built-in rather than a business Tool, so it bypasses Tool selection and is not ledgered; Hermes receives it whenever the governed sidecar is present.

Connection health is live, not baked into the deployment. The sidecar records `healthy` after a successful connector call, `failed` after a provider 401/403 and `degraded` after other provider errors. Operators can record a revocation or recovery without redeploying Agents:

```bash
python3 scripts/client-operator.py connection calendar revoked \
  --project-name acme-prod \
  --actor operator@example.com --reason "consent withdrawn"
python3 scripts/client-operator.py connections --project-name acme-prod
```

The next `platform.capabilities` read reports the change. The Receptionist eval suite checks that an unbound capability is reported with its human hand-off fallback and that the instructions tell the coworker to decline and offer that fallback instead of claiming success.

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

Changing the Tool arguments or bounded governance context creates a different request ID, so an approval cannot authorize a materially different action. Approval decisions are also single-use: once an approved or rejected decision is applied, it is consumed. A later identical material action must pause for a fresh human decision rather than replaying an earlier approval.

This file-backed approval mechanism is intentionally an MVP operator surface. Do not build an approval portal or workflow engine until repeated client evidence justifies one.

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

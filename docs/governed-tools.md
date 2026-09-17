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
       +-> versioned JSONL audit
       +-> approved connector
```

The sidecar is not a control plane and is not exposed on a host port. It is part of the same isolated client Docker deployment and consumes the same compiled single-Agent desired state. Persistent approval and audit state is mounted only into the governed Tool sidecar; Hermes does not mount that governance volume and therefore cannot create approval evidence by writing directly to the file-backed operator store.

Hermes only receives MCP Tool names corresponding to Tools explicitly selected by that Agent. Provisioning fails closed if a selected Tool has no implemented governed runtime binding.

## Google Calendar MVP binding

The reference Receptionist currently supports:

- `calendar.availability` — read-only free/busy lookup; no human approval required;
- `calendar.book` — event creation; exact-action human approval required.

When either Calendar Tool is selected, add an environment-scoped connector binding:

```yaml
environment: dev
runtime:
  profile: receptionist
  model_credential_ref: secret://model/dev
connectors:
  google_calendar:
    token_ref: secret://google/dev-calendar
    calendar_id: primary
```

Then `MODEL_GARDEN_RUNTIME_SECRETS_JSON` in the protected GitHub Environment must contain exactly those references, for example:

```json
{
  "secret://google/dev-calendar": "<runtime Google Calendar token>",
  "secret://model/dev": "<runtime model credential>"
}
```

The aggregate secret map exists only in the deployment process. Docker Compose injects the model credential only into Hermes and the Calendar credential only into the governed Tool sidecar.

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

Governed material actions emit versioned JSONL records with `schemaVersion: 1`. Depending on the stage, records capture:

- timestamp and event type (`authorization`, `approval`, `execution`);
- decision/outcome;
- immutable governed-action `requestId`;
- Agent and Tool;
- initiating user/workload when available;
- bounded governance context such as tenant, environment, deployment, session/conversation, runtime/model provenance and correlation ID;
- optional commercial attribution context such as cost center, billing subscription and shared consumption pool;
- approver identity for approved actions;
- result type plus a digest of the result rather than the full connector response;
- failure type without copying exception messages that may contain sensitive provider data.

The bounded context intentionally rejects arbitrary fields so prompts, raw credentials and uncontrolled application payloads cannot be smuggled into the governance envelope.

### Audit-to-cost correlation

Audit and billing remain separate systems with a stable join key. A usage event may include:

- `action_request_id` = the governed Tool `requestId`;
- `trace_id` = the same safe runtime/call correlation identifier used in governance context;
- client, Agent, environment, provider, cost center, billing subscription and shared-pool dimensions where applicable.

This enables enterprise showback and incident reconstruction without placing pricing logic in the Agent runtime or copying prompts/transcripts into the billing system. See `docs/metering.md` and `contracts/v1/usage-attribution.schema.json`.

A shared consumption pool is only a commercial/accounting grouping. It never weakens per-Agent action audit, approval rules or client/environment isolation.

## Current evidence boundary

Credential-free tests cover Tool selection, MCP materialization, governance, versioned action audit, approval identity, single-use approval behavior, bounded cost-attribution context, governance-volume isolation, audit behavior, Calendar connector behavior and Docker credential isolation. Metering tests cover canonical client/Agent/environment/provider attribution, shared-pool/subscription dimensions and rejection of obvious sensitive billing properties.

A real Google Calendar credential-backed end-to-end booking and production reconciliation from provider usage -> Lago -> customer subscription remain live acceptance gates and must not be claimed complete until exercised in a deployed client environment.

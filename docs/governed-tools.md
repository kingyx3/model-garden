# Governed Tools runtime

Model Garden exposes supported business Tools to Hermes through standard MCP while keeping authorization, connector credentials and audit outside the model process.

## Runtime shape

```text
Hermes / model
  model credential only
       |
       | MCP (internal Docker network)
       v
governed-tools sidecar
  selected connector credential only
       |
       +-> Model Garden allow / approval / deny
       +-> JSONL audit
       +-> approved connector
```

The sidecar is not a control plane and is not exposed on a host port. It is part of the same isolated client Docker deployment and consumes the same compiled single-Agent desired state.

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

An approval-required Tool call returns `pending-approval` and persists the exact request material under the client's existing runtime volume. The request ID binds Agent, Tool, exact arguments and initiating identity.

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

Changing the Tool arguments creates a different request ID, so an approval cannot authorize a materially different action.

This file-backed approval mechanism is intentionally an MVP operator surface. Do not build an approval portal or workflow engine until repeated client evidence justifies one.

## Current evidence boundary

Credential-free tests cover Tool selection, MCP materialization, governance, approval identity, audit behavior, Calendar connector behavior and Docker credential isolation. A real Google Calendar credential-backed end-to-end booking remains a live acceptance gate and must not be claimed complete until exercised in a deployed client environment.

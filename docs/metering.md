# Usage metering, cost attribution and billing

Model Garden uses an OSS-first boundary for commercial usage: **emit attributable usage facts, let Lago own billing.** Do not build a Model Garden pricing database, invoice engine, subscription engine or billing dashboard.

## Commercial principle: subscription-first, consumption-aware

The preferred customer experience is a predictable recurring subscription, not a surprise variable bill. Meter consumption continuously for transparency and unit economics, then configure commercial policy in Lago.

Default enterprise shape:

1. a fixed monthly/annual subscription for the managed AI workforce and agreed baseline capacity;
2. one shared consumption pool where practical so multiple approved Agents can draw from the same allowance;
3. clear included usage/allowances and forecast thresholds;
4. consumption-based overage, tier step-up or scheduled true-up only above the agreed allowance;
5. separately scoped Expansion work for material new capabilities.

Pure pay-as-you-go remains available where appropriate, but should not be the default when a subscription gives the customer better forecasting and Model Garden better recurring-revenue visibility.

Commercial prices, included allowances, tiers, credits, markups and invoices live in Lago/the commercial system. They never live in Agent, Skill or runtime business definitions.

## Architecture

```text
client runtime (per client environment)
  Hermes session stores (state.db) --read-only--> governed worker "usage" job
                                                    | usage.recorded (counts only)
  governed sidecar ---------------------------------+-- execution (Tool calls)
                                                    v
                                       hash-chained Action Ledger
                                                    | worker "export" job, as the export identity
                                                    v
                         retention-locked ledger export bucket (create-only)
                                                    | read as a verifier
                                                    v
shared billing host (infra/billing/gcp)
  billing sync (scripts/billing-sync.py) --> self-hosted Lago (customers, plans, allowances, invoices)
                    |
                    +--> monthly usage journal --> per client / Agent / channel report
```

The client runtime holds no billing credential and calls nothing to bill. Usage leaves it only inside the tamper-evident ledger
export it already writes, and the billing host reads that export with its own identity, which each client environment grants
read access as an `export_verifier_members` entry. One shared Lago serves every client; each client commercial contract is a Lago
customer and subscription.

## What is metered

| Usage | Source | Lago metric (`code`) | Dimensions |
| --- | --- | --- | --- |
| Model input, output, cache-read and cache-write tokens | Hermes' cumulative per-session counters in each `state.db` (the conversational runtime and the private coworker API) | `llm_input_tokens`, `llm_output_tokens`, `llm_cache_read_tokens`, `llm_cache_write_tokens` | client, Agent, environment, channel (Hermes `source`: Slack, Teams, API server and so on), provider, model |
| Governed Tool calls | `execution` events with outcome `succeeded` in the ledger | `tool_calls` | client, Agent, environment, channel, Tool, provider |

Every metric sums `quantity` and carries an `agent` filter, so a Lago plan can price, allow or show each Agent separately. Hermes'
reasoning-token and API-call counters and its estimated or provider-reported cost are journaled for the report but not sent as
billable metrics; price model usage on tokens in Lago, and use the cost figures to reconcile against provider statements.

Voice minutes are not metered yet: the voice worker (`livekit_receptionist.py`) runs outside the client runtime and its own LiveKit
and telephony bills are the source until it is.

### Metering in the runtime

The governed worker's `usage` job (every five minutes) opens each Hermes `state.db` read-only, compares every session's cumulative
counters with what it already metered, and ledgers the difference as one `usage.recorded` operator action per session
(`platform/metering/usage.py`). Details hold counters, the model and provider names, the channel and a 16-character hash of the
session id: never messages, titles, user or chat ids. The Docker bundle mounts both Hermes state volumes into the worker read-only
(`MODEL_GARDEN_USAGE_SOURCES`), so metering runs wherever the worker is enabled. Because counters are cumulative, a pass that is
skipped or fails loses nothing, and the next pass meters the whole difference; a store that exists but cannot be read fails the job
(ledgered as `worker.job-failed`). The first pass after an upgrade meters each existing session's usage to date in one fact.

`usageId` is a hash of the store, the session and the cumulative counters the fact ends at, so a fact recorded twice (a crash
between the ledger write and the cursor update) has the same identity and is billed once. The same `usage.recorded` events feed the
audit timeline's `usage` totals.

### The billing sync

`scripts/billing-sync.py` runs on the billing host every `billing_sync_minutes` (default 15):

1. `metrics` creates or updates the billable metrics above, with the Agents listed in `billing_sources` as `agent` filter values;
2. `sync` lists the records each client's export gained since its cursor, checks them before billing anything (each record id is
   its event's chain hash, sequences are contiguous and each event links to the one before; a gap stops that client at the gap,
   anything else fails it and bills nothing from it), converts usage facts and completed Tool calls to Lago events, appends them to
   a monthly journal, sends them in batches of 100 and only then advances the cursor. A failing client never stops the others.

The `transaction_id` of every event derives from the ledger (`<usageId>:<token kind>`, `<record id>:tool`), and Lago keeps one
event per transaction id. An event Lago already holds is reported as `alreadyPresent` and skipped, so a retried pass never bills
twice.

`billing-sync.py report --since 2026-10 --until 2026-12 [--by channel|model|provider|tool] [--format csv]` totals the journal per
month, client, environment and Agent. It counts a row journaled twice once, and needs no Lago access.

## Running the shared Lago

`infra/billing/gcp` creates the billing host in a Model Garden operator project: a private VM (no public address) running Lago
under Docker Compose, a persistent data disk for Postgres, Redis and Lago files with daily snapshots, nightly Postgres dumps to a
create-only bucket with a retention policy, and the billing service account. Every Lago secret (database password, Rails secret,
encryption keys, RSA key, the organization API key and the first administrator's password) is generated by Terraform as an
ephemeral value and written to Secret Manager write-only, so none is stored in Terraform state; the host reads them at boot and the
billing sync reads the API key on each run.

1. Apply it with the operator's own credentials:
   `terraform -chdir=infra/billing/gcp init -backend-config=bucket=<state bucket> -backend-config=prefix=billing/prod`, then
   `apply` with `project_id`, `region`, `zone`, `environment = "prod"`, `deletion_protection = true`, `platform_version` and
   `lago_admin_email`. Note the `billing_service_account` output.
2. For each client environment to bill, add `serviceAccount:<billing_service_account>` to its `export_verifier_members` and make
   sure the ledger export and the runtime worker are enabled (`worker.enabled` and `worker.ledger_export` in the binding).
3. Open the Lago UI through IAP: `gcloud compute start-iap-tunnel <instance_name> 80 --local-host-port=localhost:8080 --zone <zone>`
   and the same for port 3000 (`--local-host-port=localhost:3000`). Sign in as `lago_admin_email` with the `admin-password` secret.
4. In Lago, create the customer, plan (charges on the metrics above, per-Agent filters, included allowances, overage) and
   subscription for the client. The subscription's external id is the `subscription` of its billing source.
5. Add the client to `billing_sources` (its `ledger_export_bucket`, `prefix`, `subscription` and Agents) and apply again. The host
   picks the change up on its next boot, or at once with `sudo google_metadata_script_runner startup`.

Upgrading Lago is a change to `lago_version` followed by the same rerun; Lago migrates its database on start, so read its release
notes first and take a dump (`sudo systemctl start lago-backup`) before upgrading.

## Canonical attribution contract

`contracts/v1/usage-attribution.schema.json` defines the stable Model Garden dimensions used for cost showback and usage correlation. The helper `platform/metering/lago.py::build_attribution_properties` constructs those dimensions before they are sent as Lago event properties.

Required dimensions:

- `client`;
- `agent`;
- `environment`;
- `provider`.

Optional dimensions include model, usage type, quantity/unit, actual provider cost, currency, trace ID, governed-action `requestId`, cost center, billing subscription and shared-pool ID.

The same action request/trace identifier can therefore appear in the governed action audit and the usage event without copying prompts, responses, transcripts or credentials into the billing system.

## Stable Lago event boundary

Lago usage ingestion uses a unique `transaction_id`, `external_subscription_id`, billable metric `code`, timestamp, and arbitrary properties. Model Garden should preserve that contract rather than introducing a competing billing schema.

Example LLM event:

```json
{
  "event": {
    "transaction_id": "inf_20260915_acme_0001",
    "external_subscription_id": "acme-enterprise-2026",
    "code": "llm_usage",
    "timestamp": 1789466400,
    "properties": {
      "client": "acme",
      "agent": "sales-assistant",
      "environment": "prod",
      "provider": "openai",
      "model": "approved-openai-model",
      "usage_type": "llm_tokens",
      "quantity": 1500,
      "unit": "tokens",
      "tokens_in": 1200,
      "tokens_out": 300,
      "provider_cost_cents": 2.75,
      "currency": "SGD",
      "trace_id": "call-123",
      "action_request_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "cost_center": "front-office",
      "billing_subscription_id": "acme-enterprise-2026",
      "shared_pool_id": "acme-ai-workforce"
    }
  }
}
```

Example voice event:

```json
{
  "event": {
    "transaction_id": "voice_20260915_acme_0004",
    "external_subscription_id": "acme-enterprise-2026",
    "code": "voice_minutes",
    "timestamp": 1789466500,
    "properties": {
      "client": "acme",
      "agent": "receptionist",
      "environment": "prod",
      "provider": "livekit",
      "usage_type": "voice_minutes",
      "quantity": 4.5,
      "unit": "minutes",
      "provider_cost_cents": 3.2,
      "currency": "SGD",
      "shared_pool_id": "acme-ai-workforce"
    }
  }
}
```

`transaction_id` is the idempotency/deduplication key. Generate it from a stable source operation/request identifier where possible so retries do not double bill.

## Model Garden adapter

`platform/metering/lago.py` builds the canonical attribution properties and Lago-compatible event payloads, rejects obvious
sensitive content fields, sends events in batches and sets up billable metrics. It deliberately does **not** contain provider
pricing tables, customer prices, included allowances or overage tiers, cost-plus percentages, gross-margin calculations, plans,
subscription state, credits, invoice logic or payment collection. Those are Lago/commercial-system responsibilities.

## Cost attribution and showback

Every material provider cost that can be measured reliably should be attributable at least to client, Agent, environment and provider. Add model, Tool/action, cost center, trace/request and shared-pool dimensions where they materially improve reconciliation or enterprise showback.

When Model Garden can reliably determine underlying provider cost, emit that fact as `provider_cost_cents` with a currency. Configure the client's Lago plan/charge to apply the agreed commercial treatment. Do not calculate or hard-code the client's markup inside the Agent runtime.

This makes pricing changeable without redeploying the AI employee and prevents commercial terms from contaminating client-owned business definitions.

## Shared consumption pools

A shared pool is a commercial/accounting concept, not a tenancy boundary. `shared_pool_id` may group usage from several approved Agents under one subscription allowance, while security isolation, Agent identity and per-action audit remain unchanged.

Examples:

- all front-office Agents share one monthly token/voice allowance;
- a department cost center receives a pooled allowance but still gets Agent-level showback;
- a parent enterprise subscription pools consumption across approved environments while production and non-production remain separately attributable.

Do not use pooling to erase client/environment/Agent attribution.

## Data minimization

Usage events should contain only dimensions needed for attribution, operations or pricing. The adapter rejects obvious sensitive property names such as prompts, responses, transcripts, passwords, API keys, authorization headers, OAuth tokens, credentials and secrets.

Recommended dimensions include:

- client / external subscription;
- Agent;
- environment;
- provider and model;
- input/output token count;
- voice duration;
- metered connector/API unit count where commercially relevant;
- underlying provider cost when reliably known;
- cost center / shared pool where contracted;
- trace/request identifier when it contains no secret or sensitive business content.

## Integration timing

Metering reads the counters Hermes already keeps rather than inserting a model gateway. Add LiteLLM only if a gateway is
independently justified for routing/governance; add Langfuse when observability/evals justify it. Direct batch ingestion is
sufficient at current volume; do not add Kafka, ClickHouse or a custom metering pipeline until measured event volume or Lago
deployment requirements justify it.

## Verification

Before charging from metered usage:

1. stand up a `dev` billing host, point it at a non-production client environment, and run
   `billing-sync.py sync --dry-run` before the first real pass;
2. verify retries with the same transaction ID do not create unintended duplicate billable usage;
3. verify the event maps to the expected subscription, shared pool, metric and allowance/overage treatment;
4. reconcile a sample period against provider statements/usage exports;
5. verify Agent/environment/provider/cost-center showback totals reconcile to the customer-level total;
6. verify correlated governed actions can be traced by `action_request_id` or `trace_id` where applicable;
7. verify no prompt, response, transcript, secret or credential data appears in Lago events;
8. test subscription forecast thresholds and any agreed overage/true-up path with known usage;
9. restore a nightly dump into a scratch Postgres and confirm Lago starts on it.

External reference: Lago's usage ingestion API is `/api/v1/events/batch` (up to 100 events; one existing `transaction_id` rejects
the whole batch with `value_already_exist`, which the client handles), with the event fields `transaction_id`,
`external_subscription_id`, `code`, optional `timestamp`, `properties` and optional `precise_total_amount_cents`. The compose file
follows Lago's own `docker-compose.yml` for the pinned release.

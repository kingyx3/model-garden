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
client runtime / provider result / governed Tool
        |
        | attributable usage facts only
        | optional request/trace correlation to action audit
        v
Model Garden Lago adapter
        |
        v
shared Model Garden-operated Lago
  customer/subscription
  shared pool / billable metrics
  included allowance / pricing / overage policy
  credits
  invoices
```

Run one shared Lago deployment/service for Model Garden rather than one Lago instance per client. Represent each client commercial contract through Lago customers/subscriptions/plans. Keep the client AI runtime and the commercial back office separate.

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

`platform/metering/lago.py` provides only:

- a canonical usage-attribution builder;
- rejection of obvious sensitive content fields in metering properties;
- validation/building of Lago-compatible usage event payloads;
- authenticated POST to `/api/v1/events`;
- fail-closed handling of rejected events;
- an injectable transport for tests and future runtime integration.

It deliberately does **not** contain:

- provider pricing tables;
- customer subscription prices;
- included allowances or overage tiers;
- cost-plus percentages;
- gross-margin calculations;
- customer plans;
- subscription state;
- credits;
- invoice logic;
- payment collection.

Those are Lago/commercial-system responsibilities.

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

Do not insert a new model gateway solely to obtain metering. Instrument the narrow runtime/provider boundaries that already observe authoritative usage. If Hermes or a provider exposes reliable usage callbacks/receipts, adapt those into Lago events. Add LiteLLM only if a gateway is independently justified for routing/governance; add Langfuse when observability/evals justify it.

At low client volume, direct REST event ingestion is sufficient. Do not add Kafka, ClickHouse or a custom metering pipeline until measured event volume or Lago deployment requirements justify it.

## Verification

Before charging from metered usage:

1. send deterministic test events to a non-production Lago subscription;
2. verify retries with the same transaction ID do not create unintended duplicate billable usage;
3. verify the event maps to the expected subscription, shared pool, metric and allowance/overage treatment;
4. reconcile a sample period against provider statements/usage exports;
5. verify Agent/environment/provider/cost-center showback totals reconcile to the customer-level total;
6. verify correlated governed actions can be traced by `action_request_id` or `trace_id` where applicable;
7. verify no prompt, response, transcript, secret or credential data appears in Lago events;
8. test subscription forecast thresholds and any agreed overage/true-up path with known usage.

External reference: Lago's current usage ingestion API is `/api/v1/events` and its documented event fields are `transaction_id`, `external_subscription_id`, `code`, optional `timestamp`, `properties`, and optional `precise_total_amount_cents` for dynamic charges.

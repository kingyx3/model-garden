# Usage metering and billing

Model Garden uses an OSS-first boundary for commercial usage: **emit attributable usage facts, let Lago own billing.** Do not build a Model Garden pricing database, invoice engine, subscription engine or billing dashboard.

## Architecture

```text
client runtime / provider result
        |
        | attributable usage facts only
        v
Model Garden Lago adapter
        |
        v
shared Model Garden-operated Lago
  customer/subscription
  billable metrics
  pricing/markup policy
  credits
  invoices
```

Run one shared Lago deployment/service for Model Garden rather than one Lago instance per client. Represent each client commercial contract through Lago customers/subscriptions/plans. Keep the client AI runtime and the commercial back office separate.

## Stable event boundary

Lago usage ingestion uses a unique `transaction_id`, `external_subscription_id`, billable metric `code`, timestamp, and arbitrary properties. Model Garden should preserve that contract rather than introducing a competing usage schema.

Example LLM event:

```json
{
  "event": {
    "transaction_id": "inf_20260915_acme_0001",
    "external_subscription_id": "acme-prod",
    "code": "llm_usage",
    "timestamp": 1789466400,
    "properties": {
      "client": "acme",
      "agent": "sales-assistant",
      "environment": "prod",
      "provider": "openai",
      "model": "approved-openai-model",
      "tokens_in": 1200,
      "tokens_out": 300,
      "provider_cost_cents": 2.75
    }
  }
}
```

Example voice event:

```json
{
  "event": {
    "transaction_id": "voice_20260915_acme_0004",
    "external_subscription_id": "acme-prod",
    "code": "voice_minutes",
    "timestamp": 1789466500,
    "properties": {
      "client": "acme",
      "agent": "receptionist",
      "environment": "prod",
      "provider": "livekit",
      "minutes": 4.5,
      "provider_cost_cents": 3.2
    }
  }
}
```

`transaction_id` is the idempotency/deduplication key. Generate it from a stable source operation/request identifier where possible so retries do not double bill.

## Model Garden adapter

`platform/metering/lago.py` provides only:

- validation/building of Lago-compatible usage event payloads;
- authenticated POST to `/api/v1/events`;
- fail-closed handling of rejected events;
- an injectable transport for tests and future runtime integration.

It deliberately does **not** contain:

- provider pricing tables;
- cost-plus percentages;
- gross-margin calculations;
- customer plans;
- subscription state;
- credits;
- invoice logic;
- payment collection.

Those are Lago/commercial-system responsibilities.

## Cost-plus pricing boundary

When Model Garden can reliably determine underlying provider cost, emit that fact as a usage property such as `provider_cost_cents`. Configure the client's Lago plan/charge to apply the agreed commercial treatment. Do not calculate or hard-code the client's markup inside the Agent runtime.

This makes pricing changeable without redeploying the AI employee and prevents commercial terms from contaminating client-owned business definitions.

## Data minimization

Usage events should contain only dimensions needed for attribution, operations or pricing. Do not emit prompts, model responses, transcripts, customer message bodies, raw tool payloads, passwords, API keys or OAuth tokens merely for billing.

Recommended dimensions include:

- client / external subscription;
- Agent;
- environment;
- provider and model;
- input/output token count;
- voice duration;
- metered connector/API unit count where commercially relevant;
- underlying provider cost when reliably known;
- trace/request identifier when it contains no secret or sensitive business content.

## Integration timing

Do not insert a new model gateway solely to obtain metering. Instrument the narrow runtime/provider boundaries that already observe authoritative usage. If Hermes or a provider exposes reliable usage callbacks/receipts, adapt those into Lago events. Add LiteLLM only if a gateway is independently justified for routing/governance; add Langfuse when observability/evals justify it.

At low client volume, direct REST event ingestion is sufficient. Do not add Kafka, ClickHouse or a custom metering pipeline until measured event volume or Lago deployment requirements justify it.

## Verification

Before charging from metered usage:

1. send deterministic test events to a non-production Lago subscription;
2. verify retries with the same transaction ID do not create unintended duplicate billable usage;
3. verify the event maps to the expected billable metric/plan;
4. reconcile a sample period against provider statements/usage exports;
5. verify no secret or content data appears in Lago events;
6. test the agreed markup/pass-through policy with a known underlying cost.

External reference: Lago's current usage ingestion API is `/api/v1/events` and its documented event fields are `transaction_id`, `external_subscription_id`, `code`, optional `timestamp`, `properties`, and optional `precise_total_amount_cents` for dynamic charges.

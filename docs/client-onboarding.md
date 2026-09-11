# Client onboarding

Use this repository as the platform baseline while the customer-workspace split is being built. Keep customer-specific changes out of platform implementation directories whenever possible.

## 1. Protect environments

Create `dev`, `staging` and `prod` GitHub Environments. Require reviewers for production and restrict deployment branches.

## 2. Set variables

Recommended GitHub variables:

```text
CLIENT_SLUG=acme
K8S_NODE_COUNT=2
GATEWAY_REPLICAS=2
GATEWAY_SERVICE_TYPE=ClusterIP
LITELLM_IMAGE=ghcr.io/berriai/litellm:v1.99.1
```

Keep the gateway private by default (`ClusterIP`). Put production access behind the customer's ingress/API-management layer. Use `LoadBalancer` only for an explicitly accepted proof-of-value risk.

## 3. Connect cloud and models

Configure one complete cloud OIDC credential set from [`secrets.md`](secrets.md), `LITELLM_MASTER_KEY`, and at least one model provider configuration.

## 4. Validate business resources

Reference definitions live under `examples/workspace` and use the schemas in `contracts/v1`:

```bash
make dev
make contracts
```

## 5. Deploy

Run **Deploy model garden** with `action=plan`. After review, run `action=apply` against the intended GitHub Environment.

## Ownership boundary

Platform-owned: `infra/`, `platform/`, `scripts/`, `contracts/`, shared CI.

Business/customer-owned in the future workspace repo: `agents/`, `skills/`, `model-profiles/`, `tools/`, `knowledge/`, `policies/`, `evals/`.

See [`architecture.md`](architecture.md) and [`contracts.md`](contracts.md).

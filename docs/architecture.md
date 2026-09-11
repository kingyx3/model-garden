# Architecture

Model Garden separates the **platform plane** from **business-owned AI definitions**.

```text
Business-owned definitions
agents / skills / model profiles / tools / knowledge / policies
                         |
                         v
                 modelgarden.ai/v1
                         |
                         v
Platform runtime ---- Model gateway ---- OpenAI / Anthropic / open-weight
       |
       +---- policy / identity / audit / evaluation extension points
       |
       +---- EKS / GKE / AKS
```

## Current v1 components

- Terraform deployment targets for AWS, GCP and Azure.
- Portable Kubernetes runtime for the model gateway.
- Stable logical model aliases rendered from deployment configuration.
- Versioned resource contracts under `contracts/v1`.
- Reference workspace under `examples/workspace`.
- GitHub Actions validation and OIDC-first deployment.

## Deliberate extension points

The current repo does not yet pretend to be the full control plane. Add these behind stable contracts rather than embedding them into agent definitions:

1. persistent model/agent registry;
2. policy engine;
3. tool/MCP gateway;
4. SSO and per-team credentials;
5. OpenTelemetry and audit export;
6. evaluation service;
7. managed PostgreSQL and budget/usage metadata;
8. Agent Studio / self-service UI.

This keeps business content portable while the infrastructure can evolve independently.

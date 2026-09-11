# Enterprise Model Garden

A cloud-portable enterprise AI platform baseline for deploying one governed model gateway and a stable contract for business-owned agents, skills, tools and model profiles.

**Current platform version:** `0.2.0`

## What it provides

- Managed Kubernetes on **AWS/EKS**, **GCP/GKE** or **Azure/AKS**.
- One portable **LiteLLM gateway** for OpenAI, Anthropic and OpenAI-compatible/open-weight endpoints.
- OIDC-first GitHub Actions deployment and encrypted/versioned remote Terraform state.
- A safe-by-default private `ClusterIP` gateway service.
- Versioned `modelgarden.ai/v1` JSON Schemas for `Agent`, `Skill`, `ModelProfile`, `Tool`, `KnowledgeSource` and `Policy`.
- A reference business workspace that is validated in CI.
- Clear extension points for policy, tools/MCP, observability, evaluation and self-service authoring.

```text
Business teams
 agents / skills / knowledge / tools
             |
             v
      modelgarden.ai/v1
             |
             v
      Model Garden runtime
             |
        Model Gateway
        /     |      \
   OpenAI  Anthropic  open-weight/vLLM
             |
      EKS / GKE / AKS
```

## Quick start

1. Configure one cloud's OIDC secrets from [`docs/secrets.md`](docs/secrets.md).
2. Add `LITELLM_MASTER_KEY` and at least one model provider configuration.
3. Run **Actions → Deploy model garden** with `action=plan`.
4. Review the plan, then run `action=apply` against a protected GitHub Environment.

The gateway is private by default. Production exposure should be through enterprise ingress/API management with TLS, identity and rate limiting.

## Business resource contracts

Reference resources are under [`examples/workspace`](examples/workspace) and schemas under [`contracts/v1`](contracts/v1).

```bash
make dev
make contracts
```

Agents use logical profiles such as `reasoning.high`; they should not hard-code vendor model IDs. See [`docs/contracts.md`](docs/contracts.md).

## Repository layout

```text
infra/aws|gcp|azure/       Cloud infrastructure
platform/k8s/              Portable runtime
contracts/v1/              Stable business-facing resource contracts
examples/workspace/        Reference agents/skills/model profiles/tools
scripts/                    State bootstrap, config rendering, validation
.github/workflows/          Validation + deployment
docs/                       Architecture, onboarding, security, releases
VERSION                     Platform version
```

## Principles

- **Platform implementation and business AI content are separate concerns.**
- **Stable contracts beat infrastructure coupling.** Business teams author agents/skills, not Terraform.
- **Logical model profiles beat vendor IDs.** Provider changes should not require editing agents.
- **No model API keys in Terraform state.** Provider secrets are injected only at runtime.
- **OIDC first and private by default.** Long-lived cloud keys and public gateways are not the baseline.
- **Git is the initial control plane.** PR review, CODEOWNERS and versioned schemas provide governance before a UI is added.

## Validation

```bash
make dev
make validate
```

CI additionally runs Terraform `init -backend=false` + `validate` for all three clouds.

## Roadmap

The next platform layer is a real enterprise control plane: model/agent registry, policy engine, Tool/MCP Gateway, SSO/RBAC, persistent metadata, OpenTelemetry, evaluation services and an Agent Studio. See [`docs/architecture.md`](docs/architecture.md).

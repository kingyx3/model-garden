# Client onboarding

Use the repository as a template/fork per enterprise client. Keep client-specific changes out of the shared baseline whenever possible.

## 1. Fork and protect environments

Create `dev` and `prod` GitHub Environments. Require reviewers for `prod` and restrict which branches can deploy.

## 2. Set the client variables

Recommended GitHub variables:

```text
CLIENT_SLUG=acme
K8S_NODE_COUNT=2
LITELLM_IMAGE=ghcr.io/berriai/litellm:v1.99.1
```

`CLIENT_SLUG` should be short, lowercase, and DNS/resource-name safe.

## 3. Connect one cloud

Configure one complete OIDC credential set from [`secrets.md`](secrets.md). The workflow deploys only clouds whose complete credential set is available. Multiple complete sets intentionally create multiple deployments.

## 4. Register models

Set `LITELLM_MASTER_KEY`, then configure one or more provider pairs:

```text
OPENAI_API_KEY + OPENAI_MODEL_REF
ANTHROPIC_API_KEY + ANTHROPIC_MODEL_REF
OPEN_WEIGHT_API_BASE + OPEN_WEIGHT_MODEL_REF (+ optional API key)
```

Use provider/model IDs approved for that client. Open-weight inference is intentionally external to this baseline: point `OPEN_WEIGHT_API_BASE` at the client's vLLM or other OpenAI-compatible endpoint.

## 5. Deploy

Run **Deploy model garden** with `action=plan`, review the plan, then run `action=apply`.

The GitHub Actions job summary shows the Kubernetes `LoadBalancer` service. DNS/TLS and enterprise ingress should be added as part of production hardening rather than baked into every fork.

## What to customize vs. upstream

Keep in the client fork:

- regions, account/project/subscription identity
- approved model references and credentials
- sizing and network restrictions
- enterprise ingress/SSO
- model policies and data-classification rules

Prefer contributing back to the baseline:

- cloud deployment fixes
- new portable platform components
- CI/security improvements
- generic model/provider adapters

This separation makes future client upgrades a normal upstream merge instead of a reimplementation.

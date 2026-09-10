# Enterprise Model Garden

Forkable, cloud-portable starter for enterprises that want one governed AI gateway across OpenAI, Anthropic, and OpenAI-compatible/open-weight models.

## What this repo deploys

- A managed Kubernetes cluster in **AWS (EKS)**, **GCP (GKE)**, or **Azure (AKS)**.
- A shared **LiteLLM gateway** deployed the same way on every cloud.
- A simple model-garden convention: applications call stable aliases while provider/model details stay in deployment secrets.
- GitHub Actions that **auto-detect which cloud(s) to deploy** from the cloud credential secrets present in the repository/environment.
- OIDC-first cloud authentication and remote Terraform state bootstrap.

```text
Apps / Agents
     |
     v
LiteLLM Gateway  <-- one enterprise endpoint
     |
     +-- OpenAI
     +-- Anthropic
     +-- OpenAI-compatible endpoint (vLLM, TGI, etc.)

Deployment plane
     |
     +-- AWS / EKS
     +-- GCP / GKE
     +-- Azure / AKS
```

## Quick start

1. Fork this repository.
2. Add one cloud's required GitHub Actions secrets from [`docs/secrets.md`](docs/secrets.md).
3. Add `LITELLM_MASTER_KEY` plus at least one model provider configuration.
4. Run **Actions -> Deploy model garden -> Run workflow**.

The workflow discovers configured clouds automatically. If credentials for more than one cloud are present, it deploys to each of them.

### Model aliases

The deployment exposes these stable names when their corresponding secrets are configured:

- `general.openai`
- `general.anthropic`
- `general.open-weight`

Client applications should use these aliases instead of hard-coding vendor model IDs.

Example:

```bash
curl "$MODEL_GARDEN_URL/v1/chat/completions" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "general.openai",
    "messages": [{"role":"user","content":"Summarize this policy"}]
  }'
```

## Repository layout

```text
.github/workflows/deploy.yml    Multi-cloud deployment workflow
infra/aws/                      EKS + VPC Terraform
infra/gcp/                      GKE + VPC Terraform
infra/azure/                    AKS + VNet Terraform
platform/k8s/                   Shared gateway workload
scripts/bootstrap-state.sh      Idempotent remote-state bootstrap
scripts/render-model-config.sh  Builds LiteLLM config from supplied secrets
docs/                           Client onboarding and security notes
```

## Design principles

- **Your API, not a cloud API, is the abstraction.** Cloud providers are replaceable deployment targets.
- **No model API keys in Terraform.** Provider credentials are injected directly into Kubernetes by GitHub Actions.
- **OIDC first.** Long-lived cloud access keys are intentionally not the default path.
- **One portable runtime.** Kubernetes keeps the application layer identical across EKS/GKE/AKS.
- **Small fork surface.** Most client-specific changes belong in GitHub secrets and Terraform variables rather than code.

## Scope

This is a deployment foundation, not a complete enterprise AI governance product. Before production rollout, add private ingress, SSO/RBAC, network policy, central secrets management, audit export, persistent gateway metadata, policy-as-code, evaluation pipelines, and organization-specific data controls. See [`docs/security.md`](docs/security.md).

## Local validation

```bash
terraform -chdir=infra/aws fmt -check
terraform -chdir=infra/gcp fmt -check
terraform -chdir=infra/azure fmt -check
kubectl apply --dry-run=client -k platform/k8s
```

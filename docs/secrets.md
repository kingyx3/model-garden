# GitHub Actions secrets

The deploy workflow chooses target clouds from the credential sets that are present. Configure secrets in a protected GitHub **Environment** for production where possible.

## Shared model-garden secrets

Required:

| Secret | Purpose |
|---|---|
| `LITELLM_MASTER_KEY` | Bearer token required by the gateway |

Configure at least one model:

| Secret | Example shape | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | `sk-...` | OpenAI credential |
| `OPENAI_MODEL_REF` | `openai/<model-id>` | LiteLLM model reference |
| `ANTHROPIC_API_KEY` | `sk-ant-...` | Anthropic credential |
| `ANTHROPIC_MODEL_REF` | `anthropic/<model-id>` | LiteLLM model reference |
| `OPEN_WEIGHT_API_BASE` | `https://.../v1` | OpenAI-compatible endpoint such as vLLM |
| `OPEN_WEIGHT_MODEL_REF` | `openai/<served-model-name>` | Model reference used against that endpoint |
| `OPEN_WEIGHT_API_KEY` | provider-specific | Optional endpoint credential; set a non-secret placeholder only if your endpoint requires none |

The renderer only publishes aliases for fully configured providers.

## AWS

OIDC is preferred. Required to activate the AWS deployment:

- `AWS_ROLE_ARN`
- `AWS_REGION`

The role must trust this repository's GitHub OIDC identity and be able to create the VPC/EKS resources plus the remote-state S3 bucket. Avoid static AWS access keys.

## GCP

Required to activate the GCP deployment:

- `GCP_WORKLOAD_IDENTITY_PROVIDER`
- `GCP_SERVICE_ACCOUNT`
- `GCP_PROJECT_ID`
- `GCP_REGION`

Use Workload Identity Federation; the service account needs permissions for GKE, Compute networking, Service Usage, and the Terraform-state GCS bucket.

## Azure

Required to activate the Azure deployment:

- `AZURE_CLIENT_ID`
- `AZURE_TENANT_ID`
- `AZURE_SUBSCRIPTION_ID`
- `AZURE_LOCATION`

Create a federated credential for the repository/environment and grant the deployment principal the least privilege needed to create the resource group, VNet, AKS cluster, and state storage account.

## Optional deployment settings

These are read as GitHub repository/environment **variables** when present and otherwise use defaults:

| Variable | Default |
|---|---|
| `CLIENT_SLUG` | `client` |
| `ENVIRONMENT` | `dev` |
| `K8S_NODE_COUNT` | `2` |
| `LITELLM_IMAGE` | `ghcr.io/berriai/litellm:v1.99.1` |

Never commit model API keys, cloud credentials, generated kubeconfigs, or rendered LiteLLM configuration.

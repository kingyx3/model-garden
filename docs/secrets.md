# GitHub Actions secrets and variables

The deploy workflow can auto-detect target clouds from complete credential sets, or you can explicitly select `aws`, `gcp`, `azure` or `all`. Configure production values in a protected GitHub **Environment** wherever possible.

## Shared model-garden secrets

Required:

| Secret | Purpose |
|---|---|
| `LITELLM_MASTER_KEY` | Bearer token required by the gateway |

Configure at least one model provider:

| Secret | Example shape | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | `sk-...` | OpenAI credential |
| `OPENAI_MODEL_REF` | `openai/<model-id>` | LiteLLM model reference |
| `ANTHROPIC_API_KEY` | `sk-ant-...` | Anthropic credential |
| `ANTHROPIC_MODEL_REF` | `anthropic/<model-id>` | LiteLLM model reference |
| `OPEN_WEIGHT_API_BASE` | `https://.../v1` | OpenAI-compatible endpoint such as vLLM |
| `OPEN_WEIGHT_MODEL_REF` | `openai/<served-model-name>` | Model reference used against that endpoint |
| `OPEN_WEIGHT_API_KEY` | provider-specific | Optional endpoint credential |

The renderer publishes aliases only for fully configured providers. Provider credentials are never passed through Terraform.

## AWS

Required to activate AWS:

- `AWS_ROLE_ARN`
- `AWS_REGION`

Use GitHub OIDC. The role must trust the intended repository/environment and be able to create the VPC/EKS resources plus the remote-state S3 bucket. Avoid static AWS access keys.

## GCP

Required to activate GCP:

- `GCP_WORKLOAD_IDENTITY_PROVIDER`
- `GCP_SERVICE_ACCOUNT`
- `GCP_PROJECT_ID`
- `GCP_REGION`

Use Workload Identity Federation. The service account needs permissions for GKE, Compute networking, Service Usage and the Terraform-state GCS bucket.

## Azure

Required to activate Azure:

- `AZURE_CLIENT_ID`
- `AZURE_TENANT_ID`
- `AZURE_SUBSCRIPTION_ID`
- `AZURE_LOCATION`

Use a federated credential for the repository/environment. The deployment identity needs the least privilege required to create the resource group, VNet, AKS cluster and state storage account.

The current bootstrap workflow uses AKS admin kubeconfig after cluster creation. Production environments should move to Entra/Kubernetes RBAC and disable local AKS accounts once the deployment runner has the required cluster-user permissions.

## Deployment variables

These are GitHub repository/environment **variables**:

| Variable | Default | Purpose |
|---|---|---|
| `CLIENT_SLUG` | `client` | DNS/resource-safe client identifier |
| `K8S_NODE_COUNT` | `2` | Cluster worker count |
| `GATEWAY_REPLICAS` | `2` | Gateway replica count |
| `GATEWAY_SERVICE_TYPE` | `ClusterIP` | `ClusterIP` or `LoadBalancer`; keep private for production |
| `LITELLM_IMAGE` | `ghcr.io/berriai/litellm:v1.99.1` | Gateway image pinned by the deployment |

The workflow input chooses `dev`, `test`, `staging` or `prod` and passes that value to Terraform.

## Production notes

- Restrict Kubernetes API endpoints or use private/self-hosted deployment runners.
- Put the `ClusterIP` gateway behind enterprise ingress/API management, TLS, SSO and rate limiting.
- Replace directly created Kubernetes secrets with the customer's cloud secret manager and CSI/External Secrets integration.
- Pin approved model references per environment and rotate provider credentials independently of Terraform state.

Never commit provider API keys, cloud credentials, generated kubeconfigs or rendered LiteLLM configuration.

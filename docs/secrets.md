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

## Client workspace Docker target (MVP)

A bootstrapped client workspace can optionally deploy its locked Hermes profile to one isolated Docker host without introducing a separate control plane. The target is a **private self-hosted GitHub Actions runner** with Docker Engine and the Docker Compose plugin. Pull requests never run on this target; only protected `dev`/`main` push jobs can deploy.

Configure this repository/environment variable:

| Variable | Purpose |
|---|---|
| `MODEL_GARDEN_DOCKER_RUNNER` | Client-specific self-hosted runner label. When absent, the deploy job is skipped and the workflow remains artifact-only. |

Configure this secret separately in the client repository's `dev` and `prod` GitHub Environments:

| Secret | Purpose |
|---|---|
| `MODEL_GARDEN_RUNTIME_SECRETS_JSON` | JSON object whose keys exactly match the `secret://` references in the selected environment file. Missing or extra keys fail closed. |

For the generated bootstrap, the minimum environment values are logically equivalent to:

```json
{"secret://model/dev":"<dev model credential>"}
```

and:

```json
{"secret://model/prod":"<prod model credential>"}
```

Do not put those values in `environments/*.yaml`. Those files contain only references such as `model_credential_ref: secret://model/dev`.

The Docker target adapter consumes the aggregate JSON only inside the deployment process, removes it before calling Docker, and injects only the credential selected by the generated Hermes model profile. The runtime image is built from the exact Hermes revision pinned by Model Garden and a digest-pinned Python base image. A deployment must become healthy before success is reported; when a previous runtime exists, a failed candidate attempts to restore the immediately preceding image while retaining the client's persistent Hermes volume.

This MVP path is intentionally small. Google Calendar, LiveKit, or other credentials may be added to the same environment-scoped reference map only when the corresponding target binding consumes them; unreferenced keys are rejected rather than silently granting broader access.

## Production notes

- Restrict Kubernetes API endpoints or use private/self-hosted deployment runners.
- Put the `ClusterIP` gateway behind enterprise ingress/API management, TLS, SSO and rate limiting.
- Replace directly created Kubernetes secrets with the customer's cloud secret manager and CSI/External Secrets integration.
- Pin approved model references per environment and rotate provider credentials independently of Terraform state.
- Keep client Docker runners private, client-scoped and unavailable to pull-request jobs; use a dedicated runner/host per client where practical for the first production deployments.

Never commit provider API keys, cloud credentials, generated kubeconfigs, rendered LiteLLM configuration, or `MODEL_GARDEN_RUNTIME_SECRETS_JSON` values.

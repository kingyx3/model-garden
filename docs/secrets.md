# Secrets and credentials

Model Garden separates **one-time cloud bootstrap authority** from **runtime/integration credentials**. Neither belongs in client business-authored Agent, Skill or Knowledge content.

## Preferred managed GCP path

The normal bootstrap command is:

```bash
python3 scripts/launch.py acme \
  --bootstrap-credential ~/Downloads/acme-bootstrap.json
```

The service-account JSON is a **temporary local bootstrap credential**. It is used only to establish:

- remote Terraform state;
- GitHub Workload Identity Federation restricted to the exact client repository;
- a least-privilege deployment identity;
- a separate runtime identity; and
- the non-secret repository variables required by the generated keyless deployment workflow.

The JSON must never be:

- committed to Git;
- copied into the client workspace;
- stored as a long-lived GitHub secret;
- embedded in workflow source;
- passed as a Terraform variable; or
- intentionally persisted in Terraform state.

After the first keyless GitHub OIDC DEV deployment succeeds, the launch flow records verification. When local `gcloud` is available it revokes the exact bootstrap key and deletes the local JSON by default. Otherwise it prints one explicit revocation command and leaves the file in place until revocation succeeds.

Normal DEV/PROD deployment must continue without that bootstrap credential.

## Runtime and integration credentials

Application credentials remain separate from cloud bootstrap identity. Examples include:

- model-provider API credentials;
- Google Calendar or Microsoft authorization;
- LiveKit/telephony credentials;
- CRM, email or other selected connector credentials.

Client environment files contain only logical references, for example:

```yaml
environment: dev
runtime:
  profile: receptionist
  model_credential_ref: secret://model/dev
calendar:
  connector: google-calendar
  credential_ref: secret://google/dev-calendar
```

The actual values are stored in the protected GitHub `dev` and `prod` Environments as the aggregate secret:

| Secret | Purpose |
|---|---|
| `MODEL_GARDEN_RUNTIME_SECRETS_JSON` | JSON object whose keys exactly match the `secret://` references required by that environment. Missing or extra keys fail closed. |

A minimal DEV value is logically equivalent to:

```json
{"secret://model/dev":"<dev model credential>"}
```

Do not put the value in `environments/*.yaml`.

The deployment process resolves the aggregate map, removes it before Docker execution and injects only the credentials required by the selected runtime processes/capabilities. The Hermes/model process must not receive connector credentials it does not need; governed Tool sidecars receive only their connector-specific credentials.

## GitHub/cloud identity after bootstrap

The generated keyless client workflow uses non-secret repository variables for identifiers such as the GCP project, region, Workload Identity Provider, deployment service account and Terraform state location. GitHub exchanges its OIDC identity for short-lived cloud credentials at deployment time.

Do not create a permanent cloud JSON key merely to make routine deployment easier.

## Client-owned cloud

Client-owned and Model Garden-owned cloud use the same contract. The temporary bootstrap credential points at the target project/account, then federation takes over. Ownership changes who controls the cloud account and break-glass administration; it does not change the client workspace or runtime model.

## Self-hosted-runner fallback

The earlier Docker deployment path using a private client-labelled self-hosted GitHub runner remains a compatibility/exception option when cloud-native remote administration is unavailable or prohibited.

When that path is deliberately selected:

| Variable / secret | Purpose |
|---|---|
| `MODEL_GARDEN_DOCKER_RUNNER` | Client-specific private self-hosted runner label. |
| `MODEL_GARDEN_RUNTIME_SECRETS_JSON` | The same environment-scoped runtime secret map described above. |

Do not provision a self-hosted runner for the preferred managed GCP path merely because the fallback exists.

## Legacy Kubernetes/multi-cloud references

`infra/aws`, `infra/gcp`, `infra/azure`, `platform/k8s` and the legacy Kubernetes reference workflow are retained for historical/reference purposes. They are **not the standard SMB deployment path** and their older LiteLLM/Kubernetes secret model is not the canonical client onboarding contract.

Do not configure `LITELLM_MASTER_KEY`, `K8S_NODE_COUNT`, `GATEWAY_REPLICAS` or EKS/GKE/AKS deployment credentials for a normal new Model Garden client.

If a future real deployment justifies AWS, Azure, Kubernetes or a model gateway, implement or re-validate that target behind the same current invariants: pinned versions, client isolation, externalized runtime secrets and short-lived workload identity where supported.

## Rules that always apply

- Never commit API keys, OAuth tokens, service-account JSON, generated kubeconfigs or runtime secret maps.
- DEV and PROD use separate environment-scoped credentials.
- Store only the minimum credential required by the selected capability.
- Prompts, Skills and Agent instructions cannot create or expand authority.
- Record integration account ownership and revocation paths.
- Rotate provider/runtime credentials independently of Terraform state and client business definitions.

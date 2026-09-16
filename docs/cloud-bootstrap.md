# Keyless cloud bootstrap

Model Garden's preferred infrastructure lifecycle is **bootstrap once with one temporary cloud credential, then operate keylessly**.

The first implemented reference is GCP. Cloud ownership is orthogonal: the target GCP project may belong to Model Garden or the client; the bootstrap/deployment mechanics are identical.

## Invariant

```text
one temporary bootstrap credential
        ↓
Terraform bootstrap (once)
        ↓
remote Terraform state + GitHub OIDC trust
+ keyless deploy identity + runtime identity
        ↓
install client cloud deployment workflow
        ↓
NO cloud JSON key in GitHub
        ↓
GitHub Actions OIDC
        ↓
Terraform reconciles isolated Docker host
        ↓
cloud-native remote deployment
        ↓
Model Garden runtime health/rollback checks
```

The temporary bootstrap service-account JSON is never copied into the client repository, GitHub Secrets, Terraform variables, Terraform state, generated runtime artifacts, or workflow content. Keep it only at the local path supplied to the bootstrap command. After a keyless deployment has succeeded, revoke the cloud key and delete the local JSON file.

This does **not** eliminate legitimate runtime/provider credentials such as an LLM key or client-authorized Calendar token. Those remain environment-scoped runtime secrets and are separate from the infrastructure deployment identity.

## Prerequisites

- an existing GCP project, either client-owned or Model Garden-owned;
- one temporary GCP service-account JSON key with sufficient authority to create the bootstrap resources;
- local `terraform` and authenticated `gh` CLI;
- an existing private client workspace GitHub repository with the normal Model Garden client workflow;
- `dev`/`prod` runtime secrets configured separately when the AI employee needs them.

## One-time bootstrap

From an approved Model Garden checkout:

```bash
python3 scripts/bootstrap-cloud.py gcp \
  --project acme-ai-prod \
  --repo kingyx3/acme-ai-workspace \
  --client-slug acme \
  --agent receptionist \
  --credential-file /secure/temp/acme-bootstrap.json \
  --region asia-southeast1 \
  --zone asia-southeast1-b \
  --ownership client
```

Use `--ownership model-garden` when the project is in Model Garden's cloud account. This value is metadata/operating ownership; it does not change the deployment architecture.

The command:

1. validates the local credential without printing its private key;
2. keeps bootstrap Terraform working state outside the repository under `~/.model-garden/bootstrap/` by default;
3. creates a versioned GCS Terraform-state bucket;
4. creates a GitHub Workload Identity Pool/provider restricted to the exact client repository;
5. creates a keyless deploy service account and a narrow runtime VM identity;
6. grants the deploy identity the permissions required to reconcile the Docker host and connect through IAP/OS Login;
7. writes only non-secret bootstrap outputs to GitHub repository variables;
8. installs `.github/workflows/model-garden-cloud.yml` in the client repository;
9. enables the cloud deployment path only after configuration is complete.

The command is repeatable as long as its bootstrap state directory is retained. The same Terraform resources are reconciled rather than intentionally duplicated.

## Remove the bootstrap credential

After the first GitHub OIDC deployment succeeds, revoke the exact key identified by the command and delete the local JSON file. For GCP the revocation shape is:

```bash
gcloud iam service-accounts keys delete KEY_ID \
  --iam-account SERVICE_ACCOUNT_EMAIL \
  --project TARGET_PROJECT
rm /secure/temp/acme-bootstrap.json
```

Do not upload that JSON to GitHub "temporarily". GitHub never needs it.

## Normal deployment after bootstrap

A push to `dev` or `main` first passes the normal `Model Garden client runtime` validation/materialization workflow. The installed cloud workflow runs only after that workflow succeeds.

For GCP it then:

1. exchanges the GitHub Actions OIDC token through Workload Identity Federation;
2. initializes Terraform against the bootstrap-created GCS backend;
3. reconciles one isolated Compute Engine Docker host for the selected client/environment;
4. waits for the idempotent host bootstrap to install Docker/Compose/Python/Git;
5. transfers the reproducible environment binding and Hermes profile through IAP;
6. streams the environment-scoped runtime secret map to the remote deployment process without putting it in command arguments;
7. runs the existing Model Garden Docker deployment/health/rollback adapter remotely.

There is no permanent self-hosted GitHub runner requirement in this path.

## Idempotency boundaries

- **Bootstrap desired state:** Terraform under `infra/bootstrap/gcp`; normally run once, repeatable using retained bootstrap state.
- **Client infrastructure desired state:** Terraform under `infra/docker-host/gcp`; every deployment may safely reconcile it against remote state.
- **Application/runtime desired state:** the existing Model Garden compiler + Docker deployer; repeated deployments converge on the pinned client workspace/runtime state and retain rollback behavior.

Terraform does not own client business definitions, Skills, model/provider runtime secrets, OAuth grants, or telephony business configuration.

## Current scope

GCP is the first production-shaped implementation of this contract. AWS/Azure should use the same external contract—one temporary bootstrap credential, remote state, GitHub OIDC/federation, an isolated Docker host and cloud-native remote execution—only when a client or Model Garden deployment actually requires those providers. Do not reintroduce Kubernetes merely to make the clouds look symmetrical.

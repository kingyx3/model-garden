# Model Garden

Model Garden is an OSS-first platform and consulting delivery baseline for deploying governed AI employees for small and mid-sized businesses without requiring a client IT team.

**Current development release:** `0.3.2`

The platform repository is shared and versioned. Each client gets a separate private workspace repository containing only that client's business-specific desired state, knowledge references, evals, environment references and platform lockfile.

```text
model-garden release
  compiler + Hermes adapter + governance
  curated Skills + connectors + Docker target
             |
             | pinned dependency
             v
private client workspace
  Agent role + instructions + selected Skills/Tools
  Knowledge refs + evals + dev/prod refs
             |
             v
GitHub Actions + protected Environments
             |
             v
GitHub OIDC / workload federation
             |
             v
isolated client Docker host
```

## Operating principles

- **Do not fork the platform per client.** Pin an immutable Model Garden release in each client workspace.
- **Configure roles; do not rebuild the platform for roles.** Receptionist is the reference wedge, but Sales Assistant, Operations Coordinator, Support, Research and other bounded jobs use the same Agent/Skill/Tool contracts.
- **Secrets stay outside Git.** Client workspace files contain only logical `secret://` references; GitHub Environments hold runtime values.
- **GitHub is the engineering control plane.** Pull requests validate; `dev` deploys DEV; `main` deploys PROD.
- **Bootstrap cloud trust once, then operate keylessly.** A temporary bootstrap credential establishes remote state and GitHub workload federation; normal deployments use short-lived GitHub OIDC credentials.
- **One isolated Docker host per early client/environment.** Keep deployment boring until repeated operational evidence justifies fleet tooling.
- **OSS before proprietary infrastructure.** Hermes provides the Agent runtime; external systems such as LiveKit and Lago remain replaceable integrations.
- **Evidence before platform layers.** Kubernetes, broad multi-cloud parity, a control plane, Connector SDK or portal are not standard-path requirements until real deployments prove the need.

## Preferred client bootstrap — minimum inputs

Run from a published Model Garden release with authenticated `gh`, `git`, Terraform, and ideally `gcloud` available:

```bash
python3 scripts/launch.py acme \
  --bootstrap-credential ~/Downloads/acme-bootstrap.json
```

That is the normal Model Garden-managed path. By default it derives:

- the GCP target project from `project_id` in the supplied service-account JSON; and
- the private client repository as `<authenticated-github-user>/acme-ai-workspace`.

Override either only when the target differs:

```bash
python3 scripts/launch.py acme \
  --bootstrap-credential ~/Downloads/acme-bootstrap.json \
  --github-repo client-org/acme-ai-workspace \
  --gcp-project client-target-project \
  --ownership client
```

The launch flow is resumable and composes the existing safety boundaries. It:

1. creates or reuses the local client workspace and private GitHub repository;
2. creates `main` + `dev` through the existing Git-backed bootstrap;
3. creates/reuses protected `dev` and `prod` GitHub Environments;
4. derives each environment's exact `secret://` references and prompts only for missing runtime credentials;
5. runs the one-time GCP bootstrap using the local service-account JSON without copying it into GitHub or the workspace;
6. provisions remote Terraform state, GitHub Workload Identity Federation, a keyless deploy identity and a separate runtime identity;
7. installs the keyless GCP deployment workflow;
8. triggers an idempotent DEV infrastructure/runtime deployment and waits for GitHub Actions to verify it;
9. records the bootstrap as verified;
10. when `gcloud` is available, revokes the temporary service-account key and deletes the local JSON automatically; otherwise it prints the one exact revocation command and leaves the JSON in place;
11. runs `doctor` for both environments.

A rerun reuses existing workspace/repository state, configured runtime secret maps, cloud bootstrap variables and completed verification instead of recreating them.

Use another role without changing Model Garden itself:

```bash
python3 scripts/launch.py acme \
  --bootstrap-credential ~/Downloads/acme-bootstrap.json \
  --agent operations-coordinator \
  --role-title "Operations Coordinator"
```

The bootstrap credential is infrastructure-only. Model/provider, Calendar, telephony and other business-system credentials remain narrow runtime/integration credentials and are collected only when the selected workspace references them.

## Lower-level operator commands

`scripts/launch-client.py` is the explicit orchestration layer used by `scripts/launch.py`. The component commands remain available for diagnostics, non-GCP targets and unusual environments:

```bash
python3 scripts/client-operator.py init acme \
  --github-repo kingyx3/acme-ai-workspace

python3 scripts/client-operator.py configure ../acme-ai-workspace \
  --repo kingyx3/acme-ai-workspace \
  --environment dev

python3 scripts/bootstrap-cloud.py gcp \
  --project acme-model-garden-12345 \
  --repo kingyx3/acme-ai-workspace \
  --client-slug acme \
  --credential-file ~/Downloads/acme-bootstrap.json

python3 scripts/client-operator.py doctor ../acme-ai-workspace \
  --repo kingyx3/acme-ai-workspace \
  --environment dev
```

The older self-hosted-runner Docker path remains supported by passing `--runner-label` to `client-operator.py configure`, but it is no longer the preferred managed GCP path.

## Delivery path

```text
one-time bootstrap
  -> temporary cloud credential
  -> remote Terraform state + GitHub WIF/OIDC
  -> verify keyless DEV
  -> revoke/delete bootstrap credential

normal operation
  feature/business change
    -> pull request: validate only
    -> merge to dev: reconcile infrastructure + deploy DEV
    -> pilot/evals
    -> promote to main: reconcile infrastructure + deploy PROD
```

The keyless client workflow resolves the exact `v<modelgarden-version>` release, reconciles the isolated GCP Docker host through remote Terraform state, compiles/materializes the selected Agent and invokes the Docker deployment/health/rollback adapter through IAP/OS Login. No long-lived cloud deployment key is required after bootstrap.

## Release safety

`VERSION` is part of the client reproducibility contract. CI prevents a pull request from reusing an already-published version and automatically publishes a validated, previously-unreleased `VERSION` when it lands on `main`. Existing release tags are never moved.

The release gate validates the **current product path**: GCP bootstrap/Docker-host Terraform, contracts/compiler/runtime/operator tests, Receptionist acceptance evals and exact pinned Hermes compatibility. Legacy Kubernetes/multi-cloud reference prototypes are intentionally not release gates.

## Usage metering and billing

Model Garden does **not** implement a billing engine. `platform/metering/lago.py` is a thin adapter for sending attributable usage facts to Lago's usage-event API. Lago owns billable metrics, aggregation, percentage/cost-plus policy, credits and invoicing.

Model Garden usage events should contain dimensions such as client/subscription, Agent, environment, provider/model, token counts, voice minutes or provider cost. They should not contain prompts, transcripts, response content, API keys or Model Garden pricing tables.

See [`docs/metering.md`](docs/metering.md).

## Repository layout

```text
contracts/v1/                 Stable business-facing resource contracts
examples/workspace/           Reference Agents, Skills, Tools and Knowledge resources
platform/connectors/          Thin business-system adapters
platform/channels/            Replaceable channel adapters
platform/metering/            Thin external metering adapters; no billing engine
scripts/                       Minimum-input launch + lower-level compile/govern/deploy helpers
tests/                         Contract/runtime/operator/bootstrap regression tests
docs/                          Repository-facing architecture and operating guidance
infra/bootstrap/gcp/           Current one-time keyless trust/state bootstrap
infra/docker-host/gcp/         Current isolated GCP Docker-host desired state
infra/aws|gcp|azure/           Legacy Kubernetes/multi-cloud reference prototypes
platform/k8s/                  Legacy portable Kubernetes reference manifests
.github/workflows/legacy-kubernetes-reference.yml
                               Manual reference validation only; not a release gate
VERSION                        Next publishable Model Garden release version
```

## Development and validation

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m unittest discover -s tests -v
python3 scripts/validate-workspace.py examples/workspace
```

CI validates the current infrastructure path, release/version safety, contracts/runtime/operator behavior, pinned Hermes compatibility and the Receptionist acceptance harness. The legacy Kubernetes/multi-cloud references can be checked manually with the separate legacy reference workflow when intentionally needed.

## Build-vs-use rule

Build Model Garden code only where it provides a durable business contract, governance boundary, portability layer, evaluation/compatibility seam or reusable consulting IP. Prefer existing client systems, open standards and mature open source for commodity capabilities such as Agent runtime, telephony/media, billing/metering, observability and hosting.

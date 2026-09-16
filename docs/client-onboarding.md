# Client onboarding

Model Garden platform code stays in this repository. Create one separate private workspace repository per client and keep it limited to client-owned desired state, version pins and evals. Do not fork Model Garden per client.

## Default managed path — one command

For early SMB clients, the normal operator path is one resumable command from a published Model Garden release:

```bash
python3 scripts/launch-client.py acme \
  --github-repo kingyx3/acme-ai-workspace \
  --gcp-project acme-model-garden-12345 \
  --bootstrap-credential ~/Downloads/acme-bootstrap.json \
  --ownership client
```

Use `--ownership model-garden` when Model Garden owns the GCP project. Ownership changes metadata/account responsibility, not the deployment architecture.

The command performs the technical onboarding sequence for you:

```text
published Model Garden release
  -> create/reuse private client workspace repo
  -> configure missing dev/prod runtime secret maps
  -> use one temporary local GCP service-account JSON
  -> create remote Terraform state
  -> create GitHub Workload Identity Federation
  -> create keyless deploy + runtime identities
  -> install keyless deployment workflow
  -> trigger/reconcile isolated DEV Docker host
  -> deploy + health-check runtime
  -> verify keyless GitHub deployment
  -> record bootstrap verified
  -> revoke temporary key + delete JSON when gcloud is available
  -> doctor dev + prod
```

The flow is idempotent/resumable. A rerun reuses completed steps instead of asking the operator to recreate the repo, re-enter existing runtime secrets, recreate WIF, or re-provision verified infrastructure.

The temporary cloud credential is never written to the workspace or GitHub. It is used only to establish the durable trust/state boundary. After a verified keyless deployment it is no longer required. If `gcloud` is unavailable locally, the command prints the one exact key-revocation command and deliberately leaves the JSON file in place until revocation succeeds.

## Business configuration still matters

The one-command bootstrap removes technical choreography; it does not guess the client's business behaviour. Use discovery output to configure the initial Agent's role/instructions, selected curated Skills, approved Tools, authoritative Knowledge references, human handoff rules and representative evals.

Receptionist is the reference wedge, not a mandatory platform shape. For another bounded role:

```bash
python3 scripts/launch-client.py acme \
  --agent operations-coordinator \
  --role-title "Operations Coordinator" \
  --github-repo kingyx3/acme-ai-workspace \
  --gcp-project acme-model-garden-12345 \
  --bootstrap-credential ~/Downloads/acme-bootstrap.json
```

A new role normally changes only the client workspace. Change Model Garden core only when a reusable Skill, connector, governance capability or runtime seam is genuinely missing.

## What the operator still has to provide

Only information that Model Garden cannot safely invent or derive should require operator input:

- client slug/repository name;
- target GCP project and whether the account is client-owned or Model Garden-owned;
- one temporary bootstrap service-account JSON with sufficient bootstrap permissions;
- runtime/integration credentials actually referenced by the selected environment, prompted without echoing;
- business-approved Agent/Skill/Knowledge/eval content;
- provider/end-user OAuth consent where an external system requires the account owner to authorize access;
- production business approval before live traffic.

Do not turn cloud deployment steps, runner registration, Terraform state creation, WIF setup, GitHub variable creation, Docker installation or release publication into recurring manual checklists.

## Normal delivery after bootstrap

Once bootstrap is verified, GitHub is the control plane:

```text
pull request
  -> validate only

merge to dev
  -> GitHub OIDC
  -> reconcile DEV infrastructure from remote Terraform state
  -> compile/materialize selected Agent
  -> deploy isolated Docker runtime
  -> health/evals/pilot

promote tested revision to main
  -> GitHub OIDC
  -> reconcile PROD infrastructure
  -> deploy production runtime
  -> production smoke/acceptance
```

No long-lived cloud deployment JSON is needed after bootstrap. Platform releases do not silently mutate clients. Upgrade a client by changing `platform.lock.yaml` in a pull request, validating/evaluating in DEV, then promoting the tested revision to PROD.

## Lower-level/manual path — diagnostics or exceptions only

The underlying operations remain available independently when troubleshooting or supporting a non-default environment:

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

The legacy private self-hosted Docker-runner path remains supported through `--runner-label`, but it is not the preferred managed GCP onboarding path.

## Secret boundary

Generated `environments/*.yaml` files contain logical `secret://` references only. `scripts/launch-client.py`/`client-operator.py configure` derive the exact reference set and write the aggregate `MODEL_GARDEN_RUNTIME_SECRETS_JSON` to protected GitHub Environments through stdin. They do not write raw values into Git.

The temporary cloud bootstrap JSON is separate from runtime credentials such as model API keys, Calendar OAuth grants and telephony credentials. Cloud bootstrap establishes infrastructure identity; business-system credentials remain capability-scoped.

## Release boundary

Client workspaces must pin a real published Model Garden release. CI prevents a pull request from reusing an already-published `VERSION`; once a new version lands on `main` with all validation green, GitHub publishes its tag/release automatically. Existing release tags are never moved.

This keeps the one-command bootstrap from ever generating a client lockfile that points at a non-existent release.

## Meter usage without building billing

Where authoritative provider/runtime usage is available, send attributable usage events through the thin Lago adapter described in [`metering.md`](metering.md). Lago owns pricing plans, usage aggregation, percentage/cost-plus treatment and invoices. Do not put pricing logic or client commercial terms into the Agent workspace/runtime.

## Ownership boundary

Platform-owned here: contracts, compiler, Hermes adapter, governed actions, reusable Skills/connectors/channel adapters, keyless cloud bootstrap, Docker target adapter, thin Lago usage adapter, operator helpers and shared CI.

Client-owned in the separate private workspace: `modelgarden.yaml`, `platform.lock.yaml`, `agents/`, genuinely client-specific `skills/`, Knowledge references, `evals/`, and environment-specific non-secret configuration.

See [`architecture.md`](architecture.md), [`contracts.md`](contracts.md), [`secrets.md`](secrets.md) and [`metering.md`](metering.md).

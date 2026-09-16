# Model Garden

Model Garden is an OSS-first platform and consulting delivery baseline for deploying governed AI employees for small and mid-sized businesses without requiring a client IT team.

**Current development release:** `0.3.5`

## Architecture in one screen

```text
shared versioned Model Garden platform
             |
             | pinned by platform.lock.yaml
             v
private client workspace
Agent + Skills + Tools + Knowledge + evals
             |
      validate / compile
             |
             v
Hermes + governed MCP Tools
             |
             v
isolated client Docker host

cloud bootstrap once
  -> Terraform remote state
  -> GitHub OIDC / workload federation
  -> deploy + runtime identities

normal operation
  -> GitHub Actions
  -> Terraform infrastructure convergence
  -> Model Garden runtime convergence
```

Core rules:

- one shared Model Garden platform repo; do not fork it per client;
- one separate private business workspace per client;
- Receptionist is the reference wedge, not a platform restriction;
- Git holds business/software desired state; Terraform holds infrastructure desired state;
- raw secrets never live in Agent/Skill/Knowledge/workspace business content;
- one temporary cloud bootstrap credential may establish trust, then normal deployment is keyless;
- Model Garden-owned and client-owned hosting use the same application/workspace contract;
- build only the Model Garden differentiation layer; prefer client systems, open standards and mature OSS for commodity capabilities;
- do not add Kubernetes, a control plane, Connector SDK, custom gateway or portal without evidence from live deployments.

See [`docs/architecture.md`](docs/architecture.md), [`docs/security.md`](docs/security.md) and [`docs/secrets.md`](docs/secrets.md) for repository-facing detail.

## Preferred client launch

Run from a published Model Garden release with authenticated `gh`, `git`, Terraform and ideally `gcloud`:

```bash
python3 scripts/launch.py acme \
  --bootstrap-credential ~/Downloads/acme-bootstrap.json
```

In the normal managed GCP path Model Garden derives the target project from the JSON and the private repo as `<authenticated-github-user>/acme-ai-workspace`. Use explicit `--github-repo`, `--gcp-project` and `--ownership` overrides only when the target differs.

The resumable launch flow creates/reuses the workspace and private repo, protected DEV/PROD environments, required runtime secret maps, remote Terraform state, Workload Identity Federation, deploy/runtime identities and the keyless deployment workflow; triggers and verifies DEV; and revokes/deletes the temporary bootstrap key where local tooling permits. Resume checks fail closed if the local workspace belongs to a different client/repository or if an existing keyless cloud target differs from the requested project/region/ownership.

Runtime credentials such as model, Calendar and telephony credentials remain separate capability-scoped secrets and are requested only when the selected environment needs them.

Lower-level `scripts/launch-client.py`, `scripts/client-operator.py` and `scripts/bootstrap-cloud.py` commands exist for diagnostics and exceptional targets; they are not the normal onboarding checklist.

## Delivery lifecycle

```text
business change
  -> PR validation
  -> merge to dev: DEV deployment
  -> evals / pilot / approval
  -> promote tested revision to main
  -> PROD deployment
```

Client upgrades normally change only `platform.lock.yaml` to a newer published Model Garden release, then follow the same DEV-to-PROD promotion path.

## Governance

Enterprise actions are not granted by prompts. Selected Tools are exposed through the governed MCP boundary and evaluated as `allow | approval | deny`; approval-required actions bind to the exact material request, consume each human decision once, and produce audit evidence.

## OSS boundaries

- **Hermes**: reference Agent runtime.
- **LiveKit / existing PBX**: replaceable voice/media layer.
- **Lago**: default external usage metering/billing system; Model Garden emits attributable usage events only.
- **Langfuse**: optional observability when existing telemetry is insufficient.
- **Cloud provider**: GCP is the first current keyless Docker-host reference; add other clouds only when demanded by real deployment evidence.

## Repository layout

```text
contracts/v1/                 Versioned portable resource contracts
examples/workspace/           Reference employees and resources
platform/connectors/          Thin business-system adapters
platform/channels/            Replaceable channel adapters
platform/runtime/             Governed runtime/MCP boundary
platform/metering/            Thin external metering adapters
scripts/                       Launch, compile, govern and deploy helpers
tests/                         Regression/acceptance coverage
infra/bootstrap/gcp/           Current keyless trust/state bootstrap
infra/docker-host/gcp/         Current isolated GCP Docker-host desired state
templates/client-workflows/    Current generated client deployment workflow
```

Historical Kubernetes/LiteLLM and EKS/GKE/AKS prototypes were removed from the active repository after the Docker/keyless architecture became canonical. Git history and older immutable releases preserve them if future evidence ever justifies revisiting those approaches.

## Development

```bash
python3 -m pip install -r requirements-dev.txt
make validate
```

Release CI gates the current GCP bootstrap/Docker-host Terraform, contracts/compiler/runtime/operator tests, Receptionist acceptance evals and exact pinned Hermes compatibility. Validated unreleased `VERSION` values publish automatically after landing on `main`; existing tags are never moved.

The current product priority is not another infrastructure layer: it is proving the reference deployment live end to end with real cloud/model/Calendar/voice credentials and a stable human fallback path.

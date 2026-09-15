# Model Garden

Model Garden is an OSS-first platform and consulting delivery baseline for deploying governed AI employees for small and mid-sized businesses without requiring a client IT team.

**Current development version:** `0.3.0`

The platform repository is shared and versioned. Each client gets a separate private workspace repository containing only that client's business-specific desired state, knowledge references, evals, environment references, and platform lockfile.

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
isolated client Docker host
```

## Operating principles

- **Do not fork the platform per client.** Pin an immutable Model Garden release in each client workspace.
- **Configure roles; do not rebuild the platform for roles.** Receptionist is the reference wedge, but Sales Assistant, Operations Coordinator, Support, Research and other bounded jobs use the same Agent/Skill/Tool contracts.
- **Secrets stay outside Git.** Client workspace files contain only logical `secret://` references; GitHub Environments hold runtime values.
- **GitHub is the engineering control plane.** Pull requests validate; `dev` deploys DEV; `main` deploys PROD.
- **One isolated Docker host per early client.** Keep deployment boring until repeated operational evidence justifies fleet tooling.
- **OSS before proprietary infrastructure.** Hermes provides the Agent runtime; external systems such as LiveKit and Lago remain replaceable integrations.

## Bootstrap a client

From the exact Model Garden release you intend to deploy:

```bash
python3 scripts/client-operator.py init acme \
  --agent receptionist \
  --role-title "Receptionist" \
  --output ../acme-ai-workspace
```

A non-Receptionist role uses the same path:

```bash
python3 scripts/client-operator.py init acme \
  --agent sales-assistant \
  --role-title "Sales Assistant" \
  --output ../acme-sales-ai-workspace
```

If `git` and the GitHub CLI are authenticated, the helper can also create the private client repo and push `main` + `dev`:

```bash
python3 scripts/client-operator.py init acme \
  --github-repo kingyx3/acme-ai-workspace
```

The older `scripts/bootstrap-client-workspace.py` command remains supported directly and defaults to the Receptionist role for backwards compatibility.

## Configure deployment without hand-writing secret JSON

The generated workspace contains `environments/dev.yaml` and `environments/prod.yaml` with logical secret references. The operator helper derives the exact reference set and prompts for each value without echoing it:

```bash
python3 scripts/client-operator.py configure ../acme-ai-workspace \
  --repo kingyx3/acme-ai-workspace \
  --environment dev \
  --runner-label acme-docker
```

It creates/updates the GitHub Environment, writes `MODEL_GARDEN_RUNTIME_SECRETS_JSON` through `gh secret set` via stdin, and sets the environment-scoped `MODEL_GARDEN_DOCKER_RUNNER` variable. It never writes runtime credential values into the workspace.

Check readiness before a deployment:

```bash
python3 scripts/client-operator.py doctor ../acme-ai-workspace \
  --repo kingyx3/acme-ai-workspace \
  --environment dev
```

Run `doctor --host` on the target machine to additionally verify Docker Engine and Docker Compose.

## Delivery path

```text
feature/business change
  -> pull request: validate only
  -> merge to dev: materialize + deploy DEV
  -> pilot/evals
  -> promote to main: deploy PROD
```

The generated client workflow resolves the exact `v<modelgarden-version>` tag, compiles the selected Agent, renders a secret-free environment binding, materializes the pinned Hermes profile, and optionally deploys it through a client-labelled private self-hosted runner. Failed candidates retain/restore the last known-good Docker image where available.

## Usage metering and billing

Model Garden does **not** implement a billing engine. `platform/metering/lago.py` is a thin adapter for sending attributable usage facts to Lago's usage-event API. Lago owns billable metrics, aggregation, percentage/cost-plus policy, credits and invoicing.

Model Garden usage events should contain dimensions such as client/subscription, Agent, environment, provider/model, token counts, voice minutes or provider cost. They should not contain prompts, transcripts, response content, API keys or Model Garden pricing tables.

See [`docs/metering.md`](docs/metering.md).

## Repository layout

```text
contracts/v1/              Stable business-facing resource contracts
examples/workspace/        Reference Agents, Skills, Tools and Knowledge resources
platform/connectors/       Thin business-system adapters
platform/channels/         Replaceable channel adapters
platform/metering/         Thin external metering adapters; no billing engine
scripts/                    Bootstrap, compile, govern, deploy and operator helpers
tests/                      Contract/runtime/operator regression tests
docs/                       Repository-facing architecture and operating guidance
infra/ + platform/k8s/      Earlier/optional multi-cloud baseline; not the default SMB MVP path
VERSION                     Next Model Garden release version
```

## Development and validation

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m unittest discover -s tests -v
python3 scripts/validate-workspace.py examples/workspace
```

CI also keeps the existing infrastructure references valid, checks pinned Hermes compatibility, and runs the Receptionist acceptance harness.

## Build-vs-use rule

Build Model Garden code only where it provides a durable business contract, governance boundary, portability layer, evaluation/compatibility seam or reusable consulting IP. Prefer existing client systems, open standards and mature open source for commodity capabilities such as Agent runtime, telephony/media, billing/metering, observability and hosting.

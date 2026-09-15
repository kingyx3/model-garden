# Client onboarding

Model Garden platform code stays in this repository. Create one separate private workspace repository per client and keep that repository limited to client-owned desired state, version pins and evals. Do not fork Model Garden per client.

## 1. Bootstrap the first AI employee

Run the operator helper from the exact Model Garden release you intend to deploy:

```bash
python3 scripts/client-operator.py init acme \
  --agent receptionist \
  --role-title "Receptionist" \
  --output ../acme-ai-workspace
```

The role is not platform-specific. For another bounded job:

```bash
python3 scripts/client-operator.py init acme \
  --agent operations-coordinator \
  --role-title "Operations Coordinator" \
  --output ../acme-operations-ai-workspace
```

The helper delegates to the canonical bootstrap, which creates a secret-free compilable workspace and `platform.lock.yaml` pinned to the Model Garden `VERSION` and exact Hermes revision from that release. The default Agent remains `receptionist` for backwards compatibility.

If `git` and `gh` are authenticated, `--github-repo owner/name` also initializes Git, creates a private GitHub repository, pushes `main`, creates/pushes `dev`, and returns to `main`.

Do not copy Model Garden platform code, generic connectors, shared Skills, generated Hermes state or raw credentials into the client workspace.

## 2. Configure business requirements

Use the client discovery output to configure the initial Agent's role/instructions, selected curated Skills, approved Tools, authoritative Knowledge references, human handoff rules and representative evals.

Receptionist is the reference wedge, not a mandatory platform shape. A new role normally changes only the client workspace. Change Model Garden core only when a reusable Skill, connector, governance capability or runtime seam is genuinely missing.

## 3. Protect client environments

Create `dev` and `prod` GitHub Environments in the private client workspace. Require appropriate production review/protection before live traffic.

The generated `environments/*.yaml` files contain logical `secret://` references only. Configure them without hand-writing the aggregate secret JSON:

```bash
python3 scripts/client-operator.py configure ../acme-ai-workspace \
  --repo kingyx3/acme-ai-workspace \
  --environment dev \
  --runner-label acme-docker
```

The helper validates the environment file, derives the exact `secret://` reference set, prompts for each value without echoing it, creates/updates the GitHub Environment, writes `MODEL_GARDEN_RUNTIME_SECRETS_JSON` through stdin, and sets `MODEL_GARDEN_DOCKER_RUNNER`.

Repeat for `prod` using separate production credentials. Never paste client/runtime secrets into workspace YAML or Markdown.

## 4. Prepare the default MVP runtime target

For the first clients, use one isolated Linux VM/host per client with:

- Docker Engine;
- Docker Compose plugin;
- a private client-labelled GitHub self-hosted runner;
- no pull-request access to that runner;
- sufficient persistent storage for the client's Hermes runtime state.

Do not introduce Kubernetes or a separate deployment control plane merely to deploy an early SMB client.

On the target host, check the runtime prerequisites with:

```bash
python3 scripts/client-operator.py doctor ../acme-ai-workspace \
  --environment dev \
  --host
```

## 5. Check the full client configuration

From an authenticated operator machine:

```bash
python3 scripts/client-operator.py doctor ../acme-ai-workspace \
  --repo kingyx3/acme-ai-workspace \
  --environment dev
```

The helper checks the workspace schema, platform release pin, environment/profile consistency, logical secret references, generated Docker deployment workflow, and—when `--repo` is supplied—the expected GitHub Environment secret/runner variable names. It reports presence only; it never reads or prints secret values.

## 6. Deploy and promote

The client workspace's generated workflow is the normal deployment interface:

```text
pull request
  -> validate only

merge to dev
  -> dev GitHub Environment
  -> compile/materialize selected Agent
  -> isolated Docker target
  -> DEV health/evals/pilot

promote tested revision to main
  -> prod GitHub Environment
  -> production Docker target
```

Platform releases do not silently mutate clients. Upgrade a client by changing `platform.lock.yaml` in a pull request, validating/evaluating in DEV, then promoting the tested revision to PROD.

## 7. Meter usage without building billing

Where authoritative provider/runtime usage is available, send attributable usage events through the thin Lago adapter described in [`metering.md`](metering.md). Lago owns pricing plans, usage aggregation, percentage/cost-plus treatment and invoices. Do not put pricing logic or client commercial terms into the Agent workspace/runtime.

## Ownership boundary

Platform-owned here: contracts, compiler, Hermes adapter, governed actions, reusable Skills/connectors/channel adapters, Docker target adapter, thin Lago usage adapter, operator helper and shared CI.

Client-owned in the separate private workspace: `modelgarden.yaml`, `platform.lock.yaml`, `agents/`, genuinely client-specific `skills/`, Knowledge references, `evals/`, and environment-specific non-secret configuration.

See [`architecture.md`](architecture.md), [`contracts.md`](contracts.md), [`secrets.md`](secrets.md) and [`metering.md`](metering.md).

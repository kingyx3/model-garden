# Client onboarding

Model Garden platform code stays in this repository. Create one separate private workspace repository per client and keep that repository limited to client-owned desired state, version pins and tests/evals.

## 1. Bootstrap the client workspace

Run the operator command from the exact Model Garden release you intend to deploy:

```bash
python3 scripts/model-garden-client.py init acme --output ../acme-ai-workspace
```

Receptionist remains the backward-compatible default. For another bounded pilot role, select a role slug rather than forking Model Garden:

```bash
python3 scripts/model-garden-client.py init acme \
  --role sales-assistant \
  --output ../acme-ai-workspace
```

The command wraps the canonical bootstrap primitive and creates a secret-free, compilable workspace with the selected Agent, matching environment runtime profile, eval directory and locked GitHub delivery workflow. `platform.lock.yaml` is pinned to the Model Garden `VERSION` and exact Hermes runtime revision from that checkout. Unsafe client/role slugs and non-empty output directories fail closed.

`scripts/bootstrap-client-workspace.py` remains supported and continues to create the Receptionist workspace for backward compatibility.

Do not copy Model Garden platform code, generic connectors, generated Hermes state, or raw credentials into the client workspace. Shared/curated Skills should become pinned dependencies as dependency resolution is implemented; client-local Skills are only for genuinely client-specific procedures.

## 2. Protect client environments

In the client workspace repository, create `dev` and `prod` GitHub Environments. Require reviewers for production and restrict deployment branches. Add `staging` only when the engagement needs it.

## 3. Configure the MVP Docker target

For early managed clients, prefer one isolated Docker host per client. Register a private GitHub self-hosted runner on that host with a client-specific label and set repository/environment variable `MODEL_GARDEN_DOCKER_RUNNER` to that label. The generated workflow validates on hosted runners, materializes reproducible deployment inputs, then deploys only when the private runner variable is configured.

The host needs Docker Engine, the Docker Compose plugin and the GitHub runner. GitHub remains the engineering control plane; do not introduce Kubernetes or a separate fleet/control-plane layer until repeated operational evidence justifies it.

## 4. Connect models and runtime secrets

Each protected GitHub Environment supplies `MODEL_GARDEN_RUNTIME_SECRETS_JSON`. Keys must exactly match the logical `secret://` references in that environment file; missing or extra keys fail closed. Keep actual provider credentials in GitHub Environment secrets or provider-native authorization flows, never workspace YAML or Markdown.

The generated bootstrap uses:

```text
secret://model/dev
secret://model/prod
```

## 5. Validate and compile client desired state

Using the Model Garden release pinned by `platform.lock.yaml`:

```bash
python3 scripts/validate-workspace.py ../acme-ai-workspace
python3 scripts/compile-workspace.py ../acme-ai-workspace --agent <role> --output /tmp/employee.json
```

The generated workspace deliberately starts without business Skills, Tools or KnowledgeSource selections. Add only approved capabilities and representative evals during discovery/configuration.

## 6. Deploy and promote

Pull requests validate/test only. Use `dev` to deploy DEV. Merge/promote the approved revision to `main` to deploy PROD through the protected `prod` GitHub Environment.

Platform releases do not silently mutate client production. Upgrade a client by changing its `platform.lock.yaml` in a pull request, running validation/evals and a DEV smoke, then promoting that tested revision to PROD.

## Ownership boundary

Platform-owned here: `infra/`, `platform/`, `scripts/`, `contracts/`, reusable connector/runtime code and shared CI.

Client-owned in the separate private workspace repository: `modelgarden.yaml`, `platform.lock.yaml`, `agents/`, client-specific `skills/`, knowledge references, `evals/` and environment-specific non-secret configuration.

See [`architecture.md`](architecture.md), [`contracts.md`](contracts.md) and [`secrets.md`](secrets.md).

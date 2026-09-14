# Client onboarding

Model Garden platform code stays in this repository. Create one separate private workspace repository per client and keep that repository limited to client-owned desired state, version pins and tests/evals.

## 1. Bootstrap the client workspace

Run the bootstrap command from the exact Model Garden release you intend to deploy:

```bash
python3 scripts/bootstrap-client-workspace.py acme --output ../acme-ai-workspace
```

The command creates a secret-free, compilable Receptionist workspace and `platform.lock.yaml` pinned to the Model Garden `VERSION` and exact Hermes runtime revision from that checkout. It refuses unsafe client slugs and non-empty output directories rather than overwriting client work.

Do not copy Model Garden platform code, generic connectors, generated Hermes state, or raw credentials into the client workspace. Shared/curated Skills should become pinned dependencies as dependency resolution is implemented; client-local Skills are only for genuinely client-specific procedures.

## 2. Protect client environments

In the client workspace repository, create `dev` and `prod` GitHub Environments. Require reviewers for production and restrict deployment branches. Add `staging` only when the engagement needs it.

## 3. Configure non-secret deployment variables

Recommended GitHub variables for the current platform deploy workflow:

```text
CLIENT_SLUG=acme
K8S_NODE_COUNT=2
GATEWAY_REPLICAS=2
GATEWAY_SERVICE_TYPE=ClusterIP
LITELLM_IMAGE=ghcr.io/berriai/litellm:v1.99.1
```

Keep the gateway private by default (`ClusterIP`). Put production access behind the customer's ingress/API-management layer. Use `LoadBalancer` only for an explicitly accepted proof-of-value risk.

## 4. Connect cloud and models

Configure one complete cloud OIDC credential set from [`secrets.md`](secrets.md), `LITELLM_MASTER_KEY`, and at least one model provider configuration. Keep these values in GitHub repository/Environment secrets or provider-native authorization flows, not workspace YAML or Markdown.

## 5. Validate and compile client desired state

Using the Model Garden release pinned by `platform.lock.yaml`:

```bash
python3 scripts/validate-workspace.py ../acme-ai-workspace
python3 scripts/compile-workspace.py ../acme-ai-workspace --agent receptionist --output /tmp/receptionist.json
```

The generated bootstrap workspace deliberately starts without business Skills, Tools or KnowledgeSource selections. Add only approved capabilities and representative evals during discovery/configuration.

## 6. Deploy and promote

Pull requests validate/test only. Use a non-`main` integration branch to deploy DEV. Merge the approved revision to `main` to deploy PROD through the protected `prod` GitHub Environment.

Platform releases do not silently mutate client production. Upgrade a client by changing its `platform.lock.yaml` in a pull request, running validation/evals and a DEV smoke, then promoting that tested revision to PROD.

## Ownership boundary

Platform-owned here: `infra/`, `platform/`, `scripts/`, `contracts/`, reusable connector/runtime code and shared CI.

Client-owned in the separate private workspace repository: `modelgarden.yaml`, `platform.lock.yaml`, `agents/`, client-specific `skills/`, knowledge references, `evals/` and environment-specific non-secret configuration.

See [`architecture.md`](architecture.md), [`contracts.md`](contracts.md) and [`secrets.md`](secrets.md).

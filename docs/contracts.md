# Platform contracts

`contracts/v1/` is the compatibility boundary between the Model Garden platform, business-owned AI content and stable operational integrations.

Supported v1 business resources:

- `Agent` — composition of model profile, instructions, skills, tools and knowledge.
- `Skill` — reusable reasoning/instruction package.
- `ModelProfile` — logical model requirement mapped to approved candidates.
- `Tool` — executable enterprise capability with an explicit risk class.
- `KnowledgeSource` — approved source and data classification.
- `Policy` — portable policy metadata/rules; runtime enforcement can later be backed by OPA or another engine.

Stable operational contract:

- `usage-attribution.schema.json` — canonical non-secret client/Agent/environment/provider and optional cost/trace/subscription/shared-pool dimensions used to correlate runtime consumption with showback and billing. This is platform-generated operational metadata, not client-authored Agent/Skill desired state.

Business teams should depend on the business resource contracts rather than infrastructure details or physical model IDs. Runtime/commercial integrations should depend on the explicit operational contracts rather than inventing provider-specific attribution shapes. Breaking schema changes require a new API version, e.g. `modelgarden.ai/v2`.

## Validate a workspace

```bash
python3 -m pip install -r requirements-dev.txt
python3 scripts/validate-workspace.py examples/workspace
```

The examples are intentionally small and should become the seed for a separate customer workspace template later.

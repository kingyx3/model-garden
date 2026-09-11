# Platform contracts

`contracts/v1/` is the compatibility boundary between the Model Garden platform and business-owned AI content.

Supported v1 resources:

- `Agent` — composition of model profile, instructions, skills, tools and knowledge.
- `Skill` — reusable reasoning/instruction package.
- `ModelProfile` — logical model requirement mapped to approved candidates.
- `Tool` — executable enterprise capability with an explicit risk class.
- `KnowledgeSource` — approved source and data classification.
- `Policy` — portable policy metadata/rules; runtime enforcement can later be backed by OPA or another engine.

Business teams should depend on these contracts rather than infrastructure details or physical model IDs. Breaking schema changes require a new API version, e.g. `modelgarden.ai/v2`.

## Validate a workspace

```bash
python3 -m pip install -r requirements-dev.txt
python3 scripts/validate-workspace.py examples/workspace
```

The examples are intentionally small and should become the seed for a separate customer workspace template later.

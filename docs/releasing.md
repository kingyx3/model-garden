# Releases and compatibility

The repository version is stored in `VERSION`. Use semantic versioning:

- **PATCH** — fixes with no contract or deployment behavior change.
- **MINOR** — backward-compatible platform capabilities or optional schema fields.
- **MAJOR** — incompatible platform or contract changes.

## Release checklist

1. `make validate` passes.
2. All `modelgarden.ai/v1` examples still validate.
3. Terraform plan impact is reviewed for AWS/GCP/Azure.
4. Security-sensitive defaults are called out in release notes.
5. Any migration steps are documented before tagging.

Customer workspaces should eventually pin a released platform version rather than track `main`. Infrastructure upgrades and agent/skill changes should be separate deployment lifecycles.

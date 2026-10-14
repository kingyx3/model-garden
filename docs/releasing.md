# Releases and compatibility

The repository version is stored in `VERSION`. Use semantic versioning:

- **PATCH** — fixes with no contract or deployment behavior change.
- **MINOR** — backward-compatible platform capabilities or optional schema fields.
- **MAJOR** — incompatible platform or contract changes.

## Release checklist

1. `make validate` passes.
2. All `modelgarden.ai/v1` examples still validate.
3. Review Terraform plan impact for the supported GCP bootstrap and isolated Docker-host modules; additional clouds are not currently supported.
4. Security-sensitive defaults are called out in release notes.
5. Any migration steps are documented before tagging.

Client workspaces **must** pin an immutable published Model Garden release in `platform.lock.yaml`, not track `main`. Validate the same client revision through DEV before PROD promotion. Infrastructure upgrades and client Agent/Skill changes follow separately reviewed changes; production release tags are never moved.

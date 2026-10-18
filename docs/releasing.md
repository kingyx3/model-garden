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

## Publication and CI ownership

Merging a version-bumped PR into `main` runs the complete validation suite; **only a validated `main` run** publishes the immutable `v<VERSION>` tag and GitHub Release. Tag pushes do not run or publish a second copy of the same validation suite. Do not create release tags manually: if a tag already exists at a different commit, the `main` publisher fails closed rather than moving it. The `version-contract` CI job enforces `MAJOR.MINOR.PATCH` and refuses a pull request whose `v<VERSION>` tag is already published. Keep the GitHub release and client lockfile pointed to the published, unchanged commit.

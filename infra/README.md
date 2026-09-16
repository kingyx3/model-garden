# Infrastructure

Model Garden intentionally keeps the supported infrastructure surface small.

## Current early-client path

- `bootstrap/gcp` — one-time GCP bootstrap for remote Terraform state, GitHub Workload Identity Federation, keyless deployment identity and runtime VM identity.
- `docker-host/gcp` — idempotent isolated Docker host reconciled on normal client deployments.

This is the supported production-shaped path for early clients and works in either a Model Garden-owned or client-owned GCP project.

See [`docs/cloud-bootstrap.md`](../docs/cloud-bootstrap.md).

## Adding another target

Do not copy the removed EKS/GKE/AKS or Kubernetes prototypes back into the repository. Add AWS, Azure, Kubernetes or another hosting target only when a real deployment requires it, and implement the smallest adapter that preserves the same client workspace, release pinning, isolation, secret and workload-identity contracts.

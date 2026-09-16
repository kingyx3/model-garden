# Infrastructure

Model Garden has two infrastructure generations in this repository.

## Default early-client path

- `bootstrap/gcp` — one-time GCP bootstrap: remote Terraform state, GitHub Workload Identity Federation, keyless deployment identity and runtime VM identity.
- `docker-host/gcp` — idempotent isolated Docker host reconciled on normal client deployments.

This is the preferred production-shaped path for the first clients. It works identically in a Model Garden-owned or client-owned GCP project.

See [`docs/cloud-bootstrap.md`](../docs/cloud-bootstrap.md).

## Legacy / non-default Kubernetes paths

- `aws` — EKS reference infrastructure.
- `gcp` — GKE reference infrastructure.
- `azure` — AKS reference infrastructure.

These remain validation/reference assets but are **not** the default SMB deployment architecture. Do not choose Kubernetes merely because these modules already exist. The Docker-host path is intentionally smaller and should remain the default until workload evidence requires cluster orchestration.

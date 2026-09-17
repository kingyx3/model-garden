terraform {
  required_version = ">= 1.10.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.2"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  repository_hash         = substr(sha256(var.github_repository), 0, 8)
  state_bucket            = "model-garden-${var.project_id}-${local.repository_hash}-tfstate"
  deployer_id             = "mg-deploy-${local.repository_hash}"
  runtime_id              = "mg-runtime-${local.repository_hash}"
  pool_id                 = "mg-github-${local.repository_hash}"
  provider_id             = "github"
  deployment_workflow_ref = "${var.github_repository}/.github/workflows/model-garden-cloud.yml@refs/heads/main"

  labels = {
    project     = "model-garden"
    managed_by  = "terraform-bootstrap"
    repo_hash   = local.repository_hash
    infra_owner = var.infrastructure_owner
  }
}

resource "google_project_service" "bootstrap" {
  for_each = toset([
    "compute.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "iap.googleapis.com",
    "serviceusage.googleapis.com",
    "sts.googleapis.com",
    "storage.googleapis.com",
  ])

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_storage_bucket" "terraform_state" {
  #checkov:skip=CKV_GCP_62:The minimal baseline uses a private uniform-access state bucket restricted to deployment identities. Legacy per-bucket access logging is deployment-specific; enable the target organisation's cloud audit/logging policy where required.
  name                        = local.state_bucket
  location                    = var.region
  project                     = var.project_id
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  labels                      = local.labels

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      num_newer_versions = 20
      with_state         = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.bootstrap]
}

resource "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = local.pool_id
  display_name              = "Model Garden GitHub ${local.repository_hash}"
  description               = "Keyless GitHub Actions identities for ${var.github_repository}."
  disabled                  = false

  depends_on = [google_project_service.bootstrap]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  #checkov:skip=CKV_GCP_125:Trust is constrained to the exact repository, exact deployment workflow_ref and workflow_run event; service-account impersonation is additionally scoped to that repository principal set. Checkov does not recognise this workflow_ref condition shape.
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = local.provider_id
  display_name                       = "GitHub Actions"
  description                        = "Trust only the Model Garden cloud deployment workflow in ${var.github_repository}."

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }
  attribute_condition = "assertion.repository == '${var.github_repository}' && assertion.workflow_ref == '${local.deployment_workflow_ref}' && assertion.event_name == 'workflow_run'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "deployer" {
  project      = var.project_id
  account_id   = local.deployer_id
  display_name = "Model Garden GitHub deployer ${local.repository_hash}"
  description  = "Keyless deployment identity for ${var.github_repository}."

  depends_on = [google_project_service.bootstrap]
}

resource "google_service_account" "runtime" {
  project      = var.project_id
  account_id   = local.runtime_id
  display_name = "Model Garden runtime ${local.repository_hash}"
  description  = "Runtime VM identity for ${var.github_repository}."

  depends_on = [google_project_service.bootstrap]
}

resource "google_service_account_iam_member" "github_impersonates_deployer" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repository}"
}

resource "google_service_account_iam_member" "deployer_uses_runtime" {
  service_account_id = google_service_account.runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_project_iam_member" "deployer_roles" {
  for_each = toset([
    "roles/compute.admin",
    "roles/compute.osAdminLogin",
    "roles/iap.tunnelResourceAccessor",
    "roles/serviceusage.serviceUsageConsumer",
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_project_iam_member" "runtime_roles" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_storage_bucket_iam_member" "deployer_state" {
  bucket = google_storage_bucket.terraform_state.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_storage_bucket_iam_member" "deployer_bucket_metadata" {
  bucket = google_storage_bucket.terraform_state.name
  role   = "roles/storage.legacyBucketReader"
  member = "serviceAccount:${google_service_account.deployer.email}"
}

variable "project_id" {
  type        = string
  description = "Existing GCP project to bootstrap for Model Garden. May be client-owned or Model Garden-owned."
}

variable "region" {
  type        = string
  description = "Default GCP region for state and runtime resources."
  default     = "asia-southeast1"
}

variable "github_repository" {
  type        = string
  description = "Exact GitHub repository allowed to federate, in owner/name form."

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "github_repository must be owner/name."
  }
}

variable "infrastructure_owner" {
  type        = string
  description = "Descriptive ownership only; deployment mechanics are identical."
  default     = "model-garden"

  validation {
    condition     = contains(["model-garden", "client"], var.infrastructure_owner)
    error_message = "infrastructure_owner must be model-garden or client."
  }
}

output "project_id" {
  value = var.project_id
}

output "region" {
  value = var.region
}

output "terraform_state_bucket" {
  value = google_storage_bucket.terraform_state.name
}

output "workload_identity_provider" {
  value = google_iam_workload_identity_pool_provider.github.name
}

output "deployer_service_account" {
  value = google_service_account.deployer.email
}

output "runtime_service_account" {
  value = google_service_account.runtime.email
}

output "github_repository" {
  value = var.github_repository
}

output "infrastructure_owner" {
  value = var.infrastructure_owner
}

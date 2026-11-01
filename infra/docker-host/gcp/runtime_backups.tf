# Client runtime backups are opt-in until the coherent snapshot and restore workflows
# in #230 have passed a live recovery drill. Never equate provisioning a bucket
# with having an operational backup.
variable "runtime_backup_enabled" {
  type        = bool
  description = "Provision a private, client/environment-scoped immutable-duration bucket for future runtime backups."
  default     = false
}

variable "runtime_backup_retention_days" {
  type        = number
  description = "Days to protect client runtime backup objects before lifecycle cleanup."
  default     = 30

  validation {
    condition     = var.runtime_backup_retention_days >= 7 && var.runtime_backup_retention_days <= 3650 && floor(var.runtime_backup_retention_days) == var.runtime_backup_retention_days
    error_message = "runtime_backup_retention_days must be a whole number between 7 and 3650."
  }
}

resource "google_storage_bucket" "runtime_backups" {
  #checkov:skip=CKV_GCP_62:Use client project Cloud Audit Logs for data access; add separate access-log storage only on explicit client requirement.
  #checkov:skip=CKV_GCP_78:A locked retention policy and create-only writer enforce object protection without mutable object versions.
  count                       = var.runtime_backup_enabled ? 1 : 0
  name                        = "${substr(md5(var.project_id), 0, 10)}-${local.name}-backups"
  location                    = var.region
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  labels                      = local.labels

  retention_policy {
    retention_period = var.runtime_backup_retention_days * 86400
  }

  lifecycle_rule {
    condition {
      age = var.runtime_backup_retention_days + 1
    }
    action {
      type = "Delete"
    }
  }
}

# A separate writer owns the bucket's create-only permission. The runtime
# has NO direct bucket role; it can mint short-lived tokens for this one writer.
# Gcloud upload MUST use --impersonate-service-account for the derived account.
# An authorized recovery reader is granted separately by the client operator.
resource "google_service_account" "backup_writer" {
  count        = var.runtime_backup_enabled ? 1 : 0
  account_id   = "mg-bkp-${substr(md5(local.name), 0, 8)}"
  display_name = "Model Garden encrypted backup writer (${local.name})"
}

resource "google_storage_bucket_iam_member" "backup_writer_create" {
  count  = var.runtime_backup_enabled ? 1 : 0
  bucket = google_storage_bucket.runtime_backups[0].name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.backup_writer[0].email}"
}

resource "google_service_account_iam_member" "runtime_impersonates_backup_writer" {
  count              = var.runtime_backup_enabled ? 1 : 0
  service_account_id = google_service_account.backup_writer[0].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${var.runtime_service_account}"
}

# Recovery uses a distinct read-only service account, not the backup writer.
# No runtime, deployer, or host principal is automatically allowed to impersonate
# this reader. A client-authorized recovery operator must receive a separately
# reviewed Token Creator grant on this exact identity before running readback.
# ObjectViewer is restricted to the one client/environment backup bucket.
resource "google_service_account" "backup_reader" {
  count        = var.runtime_backup_enabled ? 1 : 0
  account_id   = "mg-bkr-${substr(md5(local.name), 0, 8)}"
  display_name = "Model Garden encrypted backup recovery reader (${local.name})"
}

resource "google_storage_bucket_iam_member" "backup_reader_view" {
  count  = var.runtime_backup_enabled ? 1 : 0
  bucket = google_storage_bucket.runtime_backups[0].name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.backup_reader[0].email}"
}

output "runtime_backup_bucket" {
  description = "Client/environment backup bucket name when provisioned; no schedule or successful recovery is implied."
  value       = var.runtime_backup_enabled ? google_storage_bucket.runtime_backups[0].name : null
}

output "runtime_backup_writer_service_account" {
  description = "Dedicated create-only backup writer for scoped gcloud impersonation; not a recovery reader or proof of live backup."
  value       = var.runtime_backup_enabled ? google_service_account.backup_writer[0].email : null
}

output "runtime_backup_reader_service_account" {
  description = "Dedicated bucket-scoped read-only recovery identity; client must separately authorize operator impersonation."
  value       = var.runtime_backup_enabled ? google_service_account.backup_reader[0].email : null
}

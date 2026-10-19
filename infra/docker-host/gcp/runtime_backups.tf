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
  count                       = var.runtime_backup_enabled ? 1 : 0
  #checkov:skip=CKV_GCP_62:Use client project Cloud Audit Logs for data access; add separate access-log storage only on explicit client requirement.
  #checkov:skip=CKV_GCP_78:A locked retention policy and create-only writer enforce object protection without mutable object versions.
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

# The runtime identity can upload NEW objects, not list, read, replace or delete
# a stored backup. Authorized operators retain recovery-read authority separately.
resource "google_storage_bucket_iam_member" "runtime_backup_writer" {
  count  = var.runtime_backup_enabled ? 1 : 0
  bucket = google_storage_bucket.runtime_backups[0].name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${var.runtime_service_account}"
}

output "runtime_backup_bucket" {
  description = "Client/environment backup bucket name when provisioned; no schedule or successful recovery is implied."
  value       = var.runtime_backup_enabled ? google_storage_bucket.runtime_backups[0].name : null
}

# Tamper-evident off-host copy of the Action Ledger (#80): a retention-locked bucket written by a separate identity.
#
# Opt-in (ledger_export_enabled). The runtime service account gets no access to the bucket. It can only mint tokens for the
# export identity, which can create objects but never read, overwrite or delete them, and once the retention policy is locked
# nobody (including the deployer) can shorten it or remove objects before they expire. A separate verifier reads the copy.

locals {
  ledger_export = var.ledger_export_enabled ? 1 : 0
}

resource "google_storage_bucket" "ledger_export" {
  #checkov:skip=CKV_GCP_62:The bucket holds hash-chained, already-redacted ledger records and is itself the audit target; access logging is available through project Data Access audit logs when a client requires it.
  #checkov:skip=CKV_GCP_78:Object versioning cannot be combined with the create-only, retention-locked design; objects are immutable until retention ends.
  count = local.ledger_export

  name                        = "${substr(md5("${var.project_id}-${local.name}"), 0, 10)}-${local.name}-ledger"
  location                    = var.region
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  labels                      = local.labels

  retention_policy {
    retention_period = var.export_retention_days * 86400
    is_locked        = var.export_lock_retention
  }

  lifecycle {
    precondition {
      condition     = var.environment != "prod" || var.export_lock_retention
      error_message = "Production ledger export must lock the retention policy (export_lock_retention = true); a locked policy cannot be removed or shortened."
    }
  }
}

resource "google_service_account" "ledger_export" {
  count = local.ledger_export

  account_id   = "mg-exp-${substr(md5(local.name), 0, 8)}"
  display_name = "Model Garden ledger export (${local.name})"
}

# Create-only: the export identity cannot list, read, overwrite or delete.
resource "google_storage_bucket_iam_member" "export_creates" {
  count = local.ledger_export

  bucket = google_storage_bucket.ledger_export[0].name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.ledger_export[0].email}"
}

# The runtime may only act as the export identity; it has no role on the bucket itself.
resource "google_service_account_iam_member" "runtime_acts_as_export" {
  count = local.ledger_export

  service_account_id = google_service_account.ledger_export[0].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${var.runtime_service_account}"
}

# Read access for the separately administered verifier (people or a service account named by the client).
resource "google_storage_bucket_iam_member" "export_verifiers" {
  for_each = var.ledger_export_enabled ? toset(var.export_verifier_members) : toset([])

  bucket = google_storage_bucket.ledger_export[0].name
  role   = "roles/storage.objectViewer"
  member = each.value
}

variable "ledger_export_enabled" {
  type        = bool
  description = "Create the retention-locked ledger export bucket and the separate export identity."
  default     = false
}

variable "export_retention_days" {
  type        = number
  description = "Days every exported ledger object is retained; objects cannot be deleted or replaced before then."
  default     = 365

  validation {
    condition     = var.export_retention_days >= 1 && var.export_retention_days <= 3650
    error_message = "export_retention_days must be between 1 and 3650."
  }
}

variable "export_lock_retention" {
  type        = bool
  description = "Lock the retention policy. Irreversible: a locked policy can only be lengthened. Required for prod."
  default     = false
}

variable "export_verifier_members" {
  type        = list(string)
  description = "IAM members (for example user:auditor@client.example) who may read the export to verify it. Must not include the runtime or deployer identities."
  default     = []

  validation {
    condition     = alltrue([for member in var.export_verifier_members : can(regex("^(user|group|serviceAccount):[^ ]+$", member))])
    error_message = "export_verifier_members must be user:, group: or serviceAccount: members."
  }

  validation {
    condition     = !contains(var.export_verifier_members, "serviceAccount:${var.runtime_service_account}")
    error_message = "The runtime service account must not be able to read the export."
  }
}

output "ledger_export_bucket" {
  value = var.ledger_export_enabled ? google_storage_bucket.ledger_export[0].name : null
}

output "ledger_export_service_account" {
  value = var.ledger_export_enabled ? google_service_account.ledger_export[0].email : null
}

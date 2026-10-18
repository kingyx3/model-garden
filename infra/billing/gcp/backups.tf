# Nightly Postgres dumps, on top of the daily data-disk snapshots. The host can add dumps but not read, replace or delete them; the
# retention policy keeps each one for backup_retention_days.

resource "google_storage_bucket" "backups" {
  #checkov:skip=CKV_GCP_62:Access to the dumps is visible in project Data Access audit logs; a separate access-log bucket is added when required.
  #checkov:skip=CKV_GCP_78:Versioning cannot be combined with the retention policy; dumps are create-only and kept for the retention period.
  name                        = "${substr(md5(var.project_id), 0, 10)}-${local.name}-backups"
  location                    = var.region
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  labels                      = local.labels

  retention_policy {
    retention_period = var.backup_retention_days * 86400
  }

  lifecycle_rule {
    condition {
      age = var.backup_retention_days + 1
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.billing]
}

resource "google_storage_bucket_iam_member" "billing_writes_backups" {
  bucket = google_storage_bucket.backups.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.billing.email}"
}

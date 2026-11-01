# Provider-mocked least-privilege backup identity contract (#230).
# No cloud resource is provisioned by this test.
mock_provider "google" {}

variables {
  project_id              = "sample-project-123"
  region                  = "asia-southeast1"
  zone                    = "asia-southeast1-a"
  client_slug             = "acme"
  environment             = "dev"
  platform_version        = "0.8.262"
  runtime_service_account = "runtime@sample-project-123.iam.gserviceaccount.com"
}

run "backup_writer_disabled_by_default" {
  command = plan

  assert {
    condition = length(google_service_account.backup_writer) == 0 && length(google_storage_bucket_iam_member.backup_writer_create) == 0 && length(google_service_account_iam_member.runtime_impersonates_backup_writer) == 0
    error_message = "Disabled backup must not provision a writer or grant impersonation."
  }
}

run "separate_create_only_writer_when_enabled" {
  command = apply
  variables {
    runtime_backup_enabled = true
  }

  assert {
    condition = length(google_service_account.backup_writer) == 1 && google_service_account.backup_writer[0].account_id == "mg-bkp-${substr(md5("acme-dev-mg"), 0, 8)}"
    error_message = "Backup writer must be a separate deterministic tenant/environment identity."
  }
  assert {
    condition = google_storage_bucket_iam_member.backup_writer_create[0].role == "roles/storage.objectCreator" && google_storage_bucket_iam_member.backup_writer_create[0].member == "serviceAccount:${google_service_account.backup_writer[0].email}" && google_storage_bucket_iam_member.backup_writer_create[0].member != "serviceAccount:${var.runtime_service_account}"
    error_message = "Only the dedicated backup identity may create objects; runtime cannot write directly."
  }
  assert {
    condition = google_service_account_iam_member.runtime_impersonates_backup_writer[0].role == "roles/iam.serviceAccountTokenCreator" && google_service_account_iam_member.runtime_impersonates_backup_writer[0].member == "serviceAccount:${var.runtime_service_account}" && google_service_account_iam_member.runtime_impersonates_backup_writer[0].service_account_id == google_service_account.backup_writer[0].name
    error_message = "Runtime may impersonate only its dedicated backup writer, not receive bucket access."
  }
  assert {
    condition = output.runtime_backup_writer_service_account == google_service_account.backup_writer[0].email
    error_message = "The scoped writer identity must be available to operators without exposing credentials."
  }
}

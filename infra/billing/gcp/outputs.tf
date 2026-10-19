output "billing_service_account" {
  description = "The billing identity. Add serviceAccount:<this> to each billed client environment's export_verifier_members."
  value       = google_service_account.billing.email
}

output "instance_name" {
  value = google_compute_instance.billing.name
}

output "zone" {
  value = google_compute_instance.billing.zone
}

output "backup_bucket" {
  value = google_storage_bucket.backups.name
}

output "secret" {
  description = "Secret Manager id of Lago's env secret (the first sign-in password is its LAGO_ORG_USER_PASSWORD line)."
  value       = google_secret_manager_secret.lago.secret_id
}

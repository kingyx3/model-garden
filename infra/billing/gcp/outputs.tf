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

output "secrets" {
  description = "Secret Manager ids of the generated Lago secrets (names only; read admin-password to sign in the first time)."
  value       = { for key, secret in google_secret_manager_secret.lago : key => secret.secret_id }
}

# Lago's secrets, generated once as ephemeral values and written write-only: they never appear in plan output or Terraform state.
# Each version is created when its secret is first created (secret_data_wo_version = 1) and is never regenerated afterwards, since
# Lago encrypts data with the encryption keys and Postgres keeps the password it was initialised with.

locals {
  generated_secrets = {
    postgres-password              = 32
    secret-key-base                = 64
    encryption-primary-key         = 32
    encryption-deterministic-key   = 32
    encryption-key-derivation-salt = 32
    api-key                        = 48
    admin-password                 = 24
  }
}

ephemeral "random_password" "lago" {
  for_each = local.generated_secrets

  length  = each.value
  special = false
}

ephemeral "tls_private_key" "lago" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "google_secret_manager_secret" "lago" {
  for_each = toset(concat(keys(local.generated_secrets), ["rsa-private-key"]))

  secret_id = "${local.name}-${each.value}"
  labels    = local.labels

  replication {
    auto {}
  }

  depends_on = [google_project_service.billing]
}

resource "google_secret_manager_secret_version" "lago" {
  for_each = google_secret_manager_secret.lago

  secret                 = each.value.id
  secret_data_wo         = each.key == "rsa-private-key" ? base64encode(ephemeral.tls_private_key.lago.private_key_pem) : ephemeral.random_password.lago[each.key].result
  secret_data_wo_version = 1
}

# Only the billing host reads them.
resource "google_secret_manager_secret_iam_member" "billing_reads" {
  for_each = google_secret_manager_secret.lago

  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.billing.email}"
}

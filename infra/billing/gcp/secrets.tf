# Lago's secrets as one Secret Manager secret holding an env file, generated once from ephemeral values and written write-only, so
# no value ever appears in plan output or Terraform state. One secret with one version is the cheapest Secret Manager footprint, and
# the host reads it once per boot. The version is written once (write-only version 1) and never regenerated, since Lago encrypts data
# with these keys and Postgres keeps the password it was initialised with.

locals {
  # Env name => generated length. LAGO_RSA_PRIVATE_KEY is added from the TLS key below.
  generated_env = {
    POSTGRES_PASSWORD                   = 32
    SECRET_KEY_BASE                     = 64
    LAGO_ENCRYPTION_PRIMARY_KEY         = 32
    LAGO_ENCRYPTION_DETERMINISTIC_KEY   = 32
    LAGO_ENCRYPTION_KEY_DERIVATION_SALT = 32
    LAGO_ORG_API_KEY                    = 48
    LAGO_ORG_USER_PASSWORD              = 24
  }
}

ephemeral "random_password" "lago" {
  for_each = local.generated_env

  length  = each.value
  special = false
}

ephemeral "tls_private_key" "lago" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "google_secret_manager_secret" "lago" {
  secret_id = "${local.name}-env"
  labels    = local.labels

  replication {
    auto {}
  }

  depends_on = [google_project_service.billing]
}

locals {
  # The env file: one NAME=value line per generated value, then the RSA key. Ephemeral, like its parts.
  lago_env = join("\n", concat(
    [for name in sort(keys(local.generated_env)) : "${name}=${ephemeral.random_password.lago[name].result}"],
    ["LAGO_RSA_PRIVATE_KEY=${base64encode(ephemeral.tls_private_key.lago.private_key_pem)}", ""],
  ))
}

resource "google_secret_manager_secret_version" "lago" {
  secret                 = google_secret_manager_secret.lago.id # pragma: allowlist secret (a Secret Manager resource id, not a value)
  secret_data_wo         = local.lago_env                       # pragma: allowlist secret (a reference to the ephemeral env above, not a value)
  secret_data_wo_version = 1
}

# Only the billing host reads it.
resource "google_secret_manager_secret_iam_member" "billing_reads" {
  secret_id = google_secret_manager_secret.lago.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.billing.email}"
}

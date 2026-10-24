# Default-disabled, client-project-native backup failure and recovery signals (#230).
# Existing authorized channels only: no IAM expansion, notification-channel creation,
# cloud activation, host timer or credential provisioning in this module.

variable "backup_health_alert_enabled" {
  type        = bool
  description = "Opt in to client-scoped Cloud Monitoring alerts for backup health events."
  default     = false

  validation {
    condition = !var.backup_health_alert_enabled || (
      var.runtime_backup_enabled &&
      can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id)) &&
      length(var.backup_health_notification_channels) >= 1 &&
      length(var.backup_health_notification_channels) <= 16 &&
      length(distinct(var.backup_health_notification_channels)) == length(var.backup_health_notification_channels)
    )
    error_message = "Backup health alerts require the isolated backup bucket, a valid GCP project ID and 1-16 distinct authorized channels."
  }
}

variable "backup_health_notification_channels" {
  type        = list(string)
  description = "Existing verified Cloud Monitoring channels in this exact client GCP project; no shared cross-tenant channels."
  default     = []

  validation {
    condition = alltrue([
      for channel in var.backup_health_notification_channels :
      can(regex("^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/notificationChannels/[0-9]+$", channel)) &&
      try(split("/", channel)[1] == var.project_id, false)
    ])
    error_message = "Every backup alert channel must be projects/THIS_CLIENT_PROJECT/notificationChannels/NUMERIC_ID."
  }
}

locals {
  # gcloud logging write defaults to the global monitored resource. Bind the
  # exact log, project, schema, component and client/environment before matching
  # health or transition; do not extract untrusted per-entry incident labels.
  backup_health_log_filter = "logName=\"projects/${var.project_id}/logs/model-garden-backup-health\" AND resource.type=\"global\" AND jsonPayload.schemaVersion=1 AND jsonPayload.component=\"client-backup\" AND jsonPayload.clientProject=\"${var.client_slug}-${var.environment}\""
}

resource "google_monitoring_alert_policy" "backup_health_failure" {
  count   = var.backup_health_alert_enabled ? 1 : 0
  project = var.project_id

  display_name          = "${local.name}-backup-unhealthy"
  combiner              = "OR"
  severity              = "ERROR"
  notification_channels = var.backup_health_notification_channels
  user_labels = {
    client      = var.client_slug
    environment = var.environment
    component   = "backup"
  }

  conditions {
    display_name = "Client backup health reported UNHEALTHY"
    condition_matched_log {
      filter = "${local.backup_health_log_filter} AND jsonPayload.health=\"UNHEALTHY\" AND severity>=ERROR"
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "Backup health is UNHEALTHY for ${var.client_slug}/${var.environment}. Inspect the private slot audit, durable incident state and encrypted upload receipt using approved operator access. Do not retry an already claimed slot. No customer data or secrets belong in alerts. A policy auto-close is NOT backup recovery; confirm a newer healthy slot and independently verify a restore before claiming recoverability."
  }

  alert_strategy {
    # One native notification per policy/hour even with repeated unhealthy polls.
    notification_rate_limit {
      period = "3600s"
    }
    # Log-match incidents cannot prove recovery from healthy samples. Avoid
    # automatic CLOSED notifications being misread as recovery evidence.
    notification_prompts = ["OPENED"]
    auto_close           = "604800s"
  }
}

resource "google_monitoring_alert_policy" "backup_health_recovery" {
  count   = var.backup_health_alert_enabled ? 1 : 0
  project = var.project_id

  display_name          = "${local.name}-backup-health-recovered"
  combiner              = "OR"
  severity              = "WARNING"
  notification_channels = var.backup_health_notification_channels
  user_labels = {
    client      = var.client_slug
    environment = var.environment
    component   = "backup"
  }

  conditions {
    display_name = "Durable incident monitor reported a new healthy backup slot"
    condition_matched_log {
      # HEALTHY level alone is insufficient: only a durable incident transition
      # from UNHEALTHY to HEALTHY is eligible for the separate recovery notice.
      filter = "${local.backup_health_log_filter} AND jsonPayload.health=\"HEALTHY\" AND jsonPayload.incidentEvent=\"backup.incident.recovered\" AND severity=INFO"
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "The client backup incident monitor observed a new healthy upload slot for ${var.client_slug}/${var.environment}. This is recovery of backup UPLOAD HEALTH only, not proof of cloud read-back, restoration, RPO/RTO, or closure of the separate unhealthy incident. Independently inspect the latest slot and run the authorized restore drill."
  }

  alert_strategy {
    notification_rate_limit {
      period = "3600s"
    }
    notification_prompts = ["OPENED"]
    auto_close           = "1800s"
  }
}

output "backup_health_alert_policy_names" {
  description = "Optional client-scoped alert policy names; no proof of live log ingestion or notification delivery."
  value = var.backup_health_alert_enabled ? [
    google_monitoring_alert_policy.backup_health_failure[0].name,
    google_monitoring_alert_policy.backup_health_recovery[0].name,
  ] : []
}

# Opt-in, independent ledger verifier health alerts (#80).
# The verifier runs outside the runtime under a separately approved read identity;
# these resources only observe redacted Cloud Logging level samples.
variable "ledger_verifier_alert_enabled" {
  type        = bool
  default     = false
  description = "Alert on unhealthy/missing independent ledger verifier samples; requires approved notification channels and a running external verifier timer."

  validation {
    condition     = !var.ledger_verifier_alert_enabled || (var.ledger_export_enabled && length(var.export_verifier_members) > 0 && length(var.gateway_uptime_notification_channels) > 0)
    error_message = "Independent ledger verifier alerts require ledger export, a separate verifier member and approved notification channels."
  }
}

resource "google_logging_metric" "ledger_verifier_unhealthy" {
  count  = var.ledger_verifier_alert_enabled ? 1 : 0
  name   = "${local.name}-ledger-verifier-unhealthy"
  filter = "logName=\"projects/${var.project_id}/logs/model-garden-ledger-verifier\" AND jsonPayload.component=\"ledger-independent-verifier\" AND jsonPayload.clientProject=\"${local.name}\" AND jsonPayload.health=\"UNHEALTHY\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_logging_metric" "ledger_verifier_heartbeat" {
  count  = var.ledger_verifier_alert_enabled ? 1 : 0
  name   = "${local.name}-ledger-verifier-heartbeat"
  filter = "logName=\"projects/${var.project_id}/logs/model-garden-ledger-verifier\" AND jsonPayload.component=\"ledger-independent-verifier\" AND jsonPayload.clientProject=\"${local.name}\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_monitoring_alert_policy" "ledger_verifier" {
  count                 = var.ledger_verifier_alert_enabled ? 1 : 0
  display_name          = "${local.name}-ledger-independent-verifier-degraded"
  combiner              = "OR"
  severity              = "ERROR"
  notification_channels = var.gateway_uptime_notification_channels
  user_labels = {
    client      = var.client_slug
    environment = var.environment
    component   = "ledger"
  }

  conditions {
    display_name = "Independent ledger verifier reported failure"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.ledger_verifier_unhealthy[0].name}\" AND resource.type=\"global\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  conditions {
    display_name = "Independent ledger verifier heartbeat missing"
    condition_absent {
      filter   = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.ledger_verifier_heartbeat[0].name}\" AND resource.type=\"global\""
      duration = "900s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "Independent ledger verifier for ${var.client_slug}/${var.environment} failed or stopped publishing. Check the separately authorized verifier timer, read-only identity, immutable export and complete chain. Cloud Logging acceptance is not alert delivery, a client-approved retention lock or independent restore proof. Do not treat an auto-closed incident as verified recovery."
  }

  alert_strategy {
    notification_prompts = ["OPENED"]
    auto_close           = "1800s"
  }
}

output "ledger_verifier_alert_policy_name" {
  description = "Opt-in alert policy; not evidence that any notification reached an operator."
  value       = var.ledger_verifier_alert_enabled ? google_monitoring_alert_policy.ledger_verifier[0].name : null
}

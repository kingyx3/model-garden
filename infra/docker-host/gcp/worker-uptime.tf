# Optional client-scoped governed-worker readiness monitoring (#231).
# Reuse the existing HTTPS gateway ingress and the gateway's already-authorized
# Cloud Monitoring channels; do not expose the private VM or grant new IAM.
# A worker may be absent in some deployments: never enable without verifying it
# is actually provisioned and shares the governance volume with the gateway.

variable "worker_uptime_alert_enabled" {
  type        = bool
  description = "Opt-in external HTTPS worker heartbeat/readiness probe, reusing authorized gateway monitoring."
  default     = false

  validation {
    condition     = !var.worker_uptime_alert_enabled || var.gateway_uptime_alert_enabled
    error_message = "Worker uptime monitoring requires an approved gateway uptime monitor, ingress and channels."
  }
}

resource "google_monitoring_uptime_check_config" "worker" {
  count = var.worker_uptime_alert_enabled ? 1 : 0

  display_name = "${local.name}-worker-readiness"
  timeout      = "10s"
  period       = "60s"
  user_labels = {
    client      = var.client_slug
    environment = var.environment
    component   = "worker"
  }

  http_check {
    path           = "/health/worker"
    port           = 443
    use_ssl        = true
    validate_ssl   = true
    request_method = "GET"
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = var.gateway_domain
    }
  }
}

resource "google_monitoring_alert_policy" "worker_uptime" {
  count = var.worker_uptime_alert_enabled ? 1 : 0

  display_name          = "${local.name}-worker-degraded"
  combiner              = "OR"
  severity              = "ERROR"
  notification_channels = var.gateway_uptime_notification_channels
  user_labels = {
    client      = var.client_slug
    environment = var.environment
    component   = "worker"
  }

  conditions {
    display_name = "Worker readiness probe pass fraction below 50% for three minutes"
    condition_threshold {
      filter                  = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\" AND metric.label.check_id=\"${google_monitoring_uptime_check_config.worker[0].uptime_check_id}\""
      comparison              = "COMPARISON_LT"
      threshold_value         = 0.5
      duration                = "180s"
      evaluation_missing_data = "EVALUATION_MISSING_DATA_ACTIVE"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_FRACTION_TRUE"
        cross_series_reducer = "REDUCE_MEAN"
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "Governed worker readiness degraded or probe telemetry missing for ${var.client_slug}/${var.environment}. Verify HTTPS ingress, then inspect the private worker heartbeat and the deployed worker with approved host access. This route reports only healthy/unavailable; a 503 can also indicate a shared-volume failure. A Cloud Monitoring incident closure is NOT verified worker recovery. Do not include job details, customer data or credentials in alerts."
  }

  alert_strategy {
    # A closed incident can be auto-closed even if the worker is still down.
    # Never label a CLOSED notification as a verified recovery.
    notification_prompts = ["OPENED"]
    auto_close           = "1800s"
  }
}

output "worker_uptime_check_id" {
  description = "Opt-in worker readiness check ID; not evidence of live notification delivery."
  value       = var.worker_uptime_alert_enabled ? google_monitoring_uptime_check_config.worker[0].uptime_check_id : null
}

# Optional client-scoped gateway availability alert (#231).
# Uses the existing HTTPS load balancer; never opens a VM port or grants IAM.
# Enable only after the client authorizes a verified Cloud Monitoring channel.

variable "gateway_uptime_alert_enabled" {
  type        = bool
  description = "Opt-in HTTPS gateway uptime check and failure/recovery alert, using existing client notification channels."
  default     = false

  validation {
    condition     = !var.gateway_uptime_alert_enabled || var.gateway_ingress_enabled
    error_message = "Gateway uptime alert requires the existing HTTPS gateway ingress."
  }

  validation {
    condition     = !var.gateway_uptime_alert_enabled || length(var.gateway_uptime_notification_channels) > 0
    error_message = "Gateway uptime alert requires at least one pre-authorized client notification channel."
  }
}

variable "gateway_uptime_notification_channels" {
  type        = list(string)
  description = "Existing, client-authorized Cloud Monitoring channel resource names in this exact GCP project."
  default     = []

  validation {
    condition = alltrue([
      for channel in var.gateway_uptime_notification_channels :
      can(regex("^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/notificationChannels/[0-9]+$", channel)) &&
      try(split("/", channel)[1] == var.project_id, false)
    ])
    error_message = "Notification channels must belong to this client GCP project and use projects/PROJECT/notificationChannels/NUMERIC_ID."
  }
}

resource "google_monitoring_uptime_check_config" "gateway" {
  count = var.gateway_uptime_alert_enabled ? 1 : 0

  display_name = "${local.name}-gateway-https"
  timeout      = "10s"
  period       = "60s"
  user_labels = {
    client      = var.client_slug
    environment = var.environment
    component   = "gateway"
  }

  http_check {
    path           = "/health/gateway"
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

resource "google_monitoring_alert_policy" "gateway_uptime" {
  count = var.gateway_uptime_alert_enabled ? 1 : 0

  display_name          = "${local.name}-gateway-degraded"
  combiner              = "OR"
  severity              = "ERROR"
  notification_channels = var.gateway_uptime_notification_channels
  user_labels = {
    client      = var.client_slug
    environment = var.environment
    component   = "gateway"
  }

  conditions {
    display_name = "Gateway HTTPS probe pass fraction below 50% for three minutes"
    condition_threshold {
      filter                  = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\" AND metric.label.check_id=\"${google_monitoring_uptime_check_config.gateway[0].uptime_check_id}\""
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
    content   = "Gateway HTTPS probe pass fraction below 50% for three minutes, or probe telemetry missing, for ${var.client_slug}/${var.environment}. Verify Cloud Monitoring probe samples, DNS/TLS and gateway health before classifying as an outage. Escalate through approved operator access. A CLOSED incident, especially after auto-close, is not proof of gateway, worker or voice recovery. Do not expose secrets or customer payloads."
  }

  alert_strategy {
    notification_prompts = ["OPENED", "CLOSED"]
    auto_close           = "1800s"
  }
}

output "gateway_uptime_check_id" {
  description = "Uptime check ID, if opted in. Does not prove a live notification was delivered."
  value       = var.gateway_uptime_alert_enabled ? google_monitoring_uptime_check_config.gateway[0].uptime_check_id : null
}

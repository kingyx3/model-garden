# Optional PRIVATE LiveKit Docker health monitoring (#231).
# A host-side opt-in timer publishes only a boolean via the existing GCE
# metricWriter identity. No ingress, additional IAM grant or monitoring SaaS.
# The custom metric must exist (first successful sample) before applying.
variable "voice_private_alert_enabled" {
  type        = bool
  default     = false
  description = "Opt-in alert on host-published native LiveKit voice health; requires authorized same-project channels."

  validation {
    condition     = !var.voice_private_alert_enabled || length(var.gateway_uptime_notification_channels) > 0
    error_message = "Private voice alert requires pre-authorized same-project notification channels."
  }
}

resource "google_monitoring_alert_policy" "voice_private" {
  count                 = var.voice_private_alert_enabled ? 1 : 0
  display_name          = "${local.name}-voice-private-degraded"
  combiner              = "OR"
  severity              = "ERROR"
  notification_channels = var.gateway_uptime_notification_channels
  user_labels = {
    client      = var.client_slug
    environment = var.environment
    component   = "voice"
  }

  conditions {
    display_name = "Private voice health unavailable for three minutes"
    condition_threshold {
      filter                  = "metric.type=\"custom.googleapis.com/model_garden/voice_ready\" AND resource.type=\"gce_instance\" AND resource.label.project_id=\"${var.project_id}\" AND resource.label.instance_id=\"${google_compute_instance.runtime.instance_id}\" AND metric.label.client=\"${var.client_slug}\" AND metric.label.environment=\"${var.environment}\""
      comparison              = "COMPARISON_LT"
      threshold_value         = 0.5
      duration                = "180s"
      evaluation_missing_data = "EVALUATION_MISSING_DATA_ACTIVE"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "Private LiveKit Docker-native readiness is failing or its host-side samples are missing for ${var.client_slug}/${var.environment}. Inspect the private voice container, metric writer timer, LiveKit worker registration and approved runbook; an idle call stream is not failure. The alert is not SIP/call success evidence. Incident auto-close is not recovery; verify new healthy samples and authorized recipient delivery. No caller data or secrets belong in alerts."
  }

  alert_strategy {
    notification_prompts = ["OPENED"]
    auto_close           = "1800s"
  }
}

output "voice_private_alert_policy_name" {
  description = "Opt-in voice alert policy name; not evidence of delivered notifications."
  value       = var.voice_private_alert_enabled ? google_monitoring_alert_policy.voice_private[0].name : null
}

# Optional PRIVATE gateway-loop and governed-worker monitoring (#231).
# Host timer publishes two booleans via existing GCE metricWriter identity.
# No public uptime endpoint, new ingress, credentials or IAM role.
variable "runtime_private_alert_components" {
  type        = set(string)
  default     = []
  description = "Opt-in private gateway/worker alerts; requires approved host sampler and same-project channels."
  validation {
    condition     = alltrue([for component in var.runtime_private_alert_components : contains(["gateway", "worker"], component)])
    error_message = "Private runtime alert components must be gateway or worker."
  }
  validation {
    condition     = length(var.runtime_private_alert_components) == 0 || length(var.gateway_uptime_notification_channels) > 0
    error_message = "Private runtime alerts require pre-authorized same-project notification channels."
  }
}

resource "google_monitoring_alert_policy" "runtime_private" {
  for_each              = var.runtime_private_alert_components
  display_name          = "${local.name}-${each.key}-private-degraded"
  combiner              = "OR"
  severity              = "ERROR"
  notification_channels = var.gateway_uptime_notification_channels
  user_labels = {
    client      = var.client_slug
    environment = var.environment
    component   = each.key
  }
  conditions {
    display_name = "Private ${each.key} readiness unavailable for three minutes"
    condition_threshold {
      filter                  = "metric.type=\"custom.googleapis.com/model_garden/${each.key}_ready\" AND resource.type=\"gce_instance\" AND resource.label.project_id=\"${var.project_id}\" AND resource.label.instance_id=\"${google_compute_instance.runtime.instance_id}\" AND metric.label.client=\"${var.client_slug}\" AND metric.label.environment=\"${var.environment}\""
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
    content   = "Private ${each.key} loopback readiness or host samples missing for ${var.client_slug}/${var.environment}. Inspect the approved host timer and Docker gateway/worker heartbeats using private access; verify metric ingestion and escalate to named operator. Incident auto-close is not recovery. This is not SIP, transaction or delivery proof. Never include tenant payloads or secrets."
  }
  alert_strategy {
    notification_prompts = ["OPENED"]
    auto_close           = "1800s"
  }
}

output "runtime_private_alert_policy_names" {
  description = "Opt-in private gateway/worker alert policies; not delivered notification proof."
  value       = { for component, policy in google_monitoring_alert_policy.runtime_private : component => policy.name }
}

# Credential-free private voice monitoring plan; no public VM endpoint (#231).
mock_provider "google" {}

variables {
  project_id              = "sample-project-123"
  region                  = "asia-southeast1"
  zone                    = "asia-southeast1-a"
  client_slug             = "acme"
  environment             = "dev"
  platform_version        = "0.8.136"
  runtime_service_account = "runtime@sample-project-123.iam.gserviceaccount.com"
}

run "disabled_by_default" {
  command = plan
  assert {
    condition     = length(google_monitoring_alert_policy.voice_private) == 0
    error_message = "Private voice alerts must be explicitly opted into."
  }
}

run "rejects_voice_without_authorized_channels" {
  command = plan
  variables {
    voice_private_alert_enabled = true
  }
  expect_failures = [var.voice_private_alert_enabled]
}

run "valid_private_voice_monitor_without_public_gateway" {
  command = plan
  override_resource {
    target          = google_compute_instance.runtime
    override_during = plan
    values = {
      instance_id = "123456789"
    }
  }
  variables {
    voice_private_alert_enabled          = true
    gateway_uptime_notification_channels = ["projects/sample-project-123/notificationChannels/123"]
  }
  assert {
    condition     = length(google_monitoring_alert_policy.voice_private) == 1 && length(google_monitoring_uptime_check_config.gateway) == 0
    error_message = "Private voice alert must not require public gateway ingress."
  }
  assert {
    condition     = google_monitoring_alert_policy.voice_private[0].notification_channels == var.gateway_uptime_notification_channels
    error_message = "Private voice alert must reuse client-authorized channels."
  }
  assert {
    condition     = google_monitoring_alert_policy.voice_private[0].conditions[0].condition_threshold[0].evaluation_missing_data == "EVALUATION_MISSING_DATA_ACTIVE" && google_monitoring_alert_policy.voice_private[0].conditions[0].condition_threshold[0].duration == "180s"
    error_message = "Missing/stale private samples must open an incident."
  }
  assert {
    condition     = strcontains(google_monitoring_alert_policy.voice_private[0].conditions[0].condition_threshold[0].filter, "custom.googleapis.com/model_garden/voice_ready") && strcontains(google_monitoring_alert_policy.voice_private[0].conditions[0].condition_threshold[0].filter, "resource.label.instance_id")
    error_message = "Alert must use the exact private voice metric and GCE instance."
  }
  assert {
    condition     = length(google_monitoring_alert_policy.voice_private[0].alert_strategy[0].notification_prompts) == 1 && contains(google_monitoring_alert_policy.voice_private[0].alert_strategy[0].notification_prompts, "OPENED")
    error_message = "Auto-closed incidents must not masquerade as verified recovery."
  }
}

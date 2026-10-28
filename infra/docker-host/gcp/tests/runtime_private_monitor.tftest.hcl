# Credential-free private gateway/worker monitoring plan (#231).
mock_provider "google" {}

variables {
  project_id              = "sample-project-123"
  region                  = "asia-southeast1"
  zone                    = "asia-southeast1-a"
  client_slug             = "acme"
  environment             = "dev"
  platform_version        = "0.8.190"
  runtime_service_account = "runtime@sample-project-123.iam.gserviceaccount.com"
}

run "disabled_by_default" {
  command = plan
  assert {
    condition     = length(google_monitoring_alert_policy.runtime_private) == 0
    error_message = "Private runtime alerts must be explicitly opted into."
  }
}

run "rejects_unapproved_channels" {
  command = plan
  variables {
    runtime_private_alert_components = ["gateway"]
  }
  expect_failures = [var.runtime_private_alert_components]
}

run "rejects_unknown_components" {
  command = plan
  variables {
    runtime_private_alert_components = ["voice"]
  }
  expect_failures = [var.runtime_private_alert_components]
}

run "valid_private_runtime_monitors_without_public_ingress" {
  command = plan
  override_resource {
    target          = google_compute_instance.runtime
    override_during = plan
    values = {
      instance_id = "123456789"
    }
  }
  variables {
    runtime_private_alert_components     = ["gateway", "worker"]
    gateway_uptime_notification_channels = ["projects/sample-project-123/notificationChannels/123"]
  }
  assert {
    condition     = length(google_monitoring_alert_policy.runtime_private) == 2 && length(google_monitoring_uptime_check_config.gateway) == 0
    error_message = "Private runtime monitors must not require public gateway ingress."
  }
  assert {
    condition = alltrue([for component, policy in google_monitoring_alert_policy.runtime_private :
      policy.notification_channels == var.gateway_uptime_notification_channels &&
      policy.conditions[0].condition_threshold[0].evaluation_missing_data == "EVALUATION_MISSING_DATA_ACTIVE" &&
      policy.conditions[0].condition_threshold[0].duration == "180s" &&
      strcontains(policy.conditions[0].condition_threshold[0].filter, "custom.googleapis.com/model_garden/${component}_ready") &&
      strcontains(policy.conditions[0].condition_threshold[0].filter, "resource.label.instance_id") &&
      length(policy.alert_strategy[0].notification_prompts) == 1 &&
      contains(policy.alert_strategy[0].notification_prompts, "OPENED")
    ])
    error_message = "Private alerts must be tenant-scoped, missing-data-sensitive, and avoid false recovery notices."
  }
}

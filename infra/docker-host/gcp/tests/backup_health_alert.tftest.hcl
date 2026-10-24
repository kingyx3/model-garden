# Offline provider-mock contract tests. No Cloud Monitoring apply or live delivery.
mock_provider "google" {}

variables {
  project_id              = "sample-project-123"
  region                  = "asia-southeast1"
  zone                    = "asia-southeast1-a"
  client_slug             = "acme"
  environment             = "dev"
  platform_version        = "0.8.95"
  runtime_service_account = "runtime@sample-project-123.iam.gserviceaccount.com"
}

run "disabled_by_default" {
  command = plan

  assert {
    condition     = length(google_monitoring_alert_policy.backup_health_failure) == 0 && length(google_monitoring_alert_policy.backup_health_recovery) == 0
    error_message = "Neither backup alert policy may exist without explicit opt-in."
  }
  assert {
    condition     = length(output.backup_health_alert_policy_names) == 0
    error_message = "Disabled monitoring must not advertise alert policy names."
  }
}

run "rejects_without_isolated_backup_bucket" {
  command = plan
  variables {
    backup_health_alert_enabled         = true
    backup_health_notification_channels = ["projects/sample-project-123/notificationChannels/123"]
  }
  expect_failures = [var.backup_health_alert_enabled]
}

run "rejects_without_approved_channels" {
  command = plan
  variables {
    runtime_backup_enabled      = true
    backup_health_alert_enabled = true
  }
  expect_failures = [var.backup_health_alert_enabled]
}

run "rejects_cross_project_channel" {
  command = plan
  variables {
    runtime_backup_enabled              = true
    backup_health_alert_enabled         = true
    backup_health_notification_channels = ["projects/other-project-123/notificationChannels/123"]
  }
  expect_failures = [var.backup_health_notification_channels]
}

run "rejects_malformed_channel" {
  command = plan
  variables {
    runtime_backup_enabled              = true
    backup_health_alert_enabled         = true
    backup_health_notification_channels = ["projects/sample-project-123/notificationChannels/not-a-number"]
  }
  expect_failures = [var.backup_health_notification_channels]
}

run "rejects_mixed_project_channels" {
  command = plan
  variables {
    runtime_backup_enabled              = true
    backup_health_alert_enabled         = true
    backup_health_notification_channels = [
      "projects/sample-project-123/notificationChannels/123",
      "projects/other-project-123/notificationChannels/999",
    ]
  }
  expect_failures = [var.backup_health_notification_channels]
}

run "rejects_duplicate_channels" {
  command = plan
  variables {
    runtime_backup_enabled              = true
    backup_health_alert_enabled         = true
    backup_health_notification_channels = [
      "projects/sample-project-123/notificationChannels/123",
      "projects/sample-project-123/notificationChannels/123",
    ]
  }
  expect_failures = [var.backup_health_alert_enabled]
}

run "rejects_filter_injection_project_id" {
  command = plan
  variables {
    project_id                          = "sample-project-123\" OR true"
    runtime_backup_enabled              = true
    backup_health_alert_enabled         = true
    backup_health_notification_channels = ["projects/sample-project-123/notificationChannels/123"]
  }
  expect_failures = [
    var.backup_health_alert_enabled,
    var.backup_health_notification_channels,
  ]
}

run "valid_scoped_failure_and_recovery_alerts" {
  command = plan
  variables {
    runtime_backup_enabled              = true
    backup_health_alert_enabled         = true
    backup_health_notification_channels = ["projects/sample-project-123/notificationChannels/123"]
  }
  assert {
    condition     = length(google_monitoring_alert_policy.backup_health_failure) == 1 && length(google_monitoring_alert_policy.backup_health_recovery) == 1
    error_message = "Opt-in must provision exactly one failure and one recovery-transition alert."
  }
  assert {
    condition = alltrue([
      for policy in [
        google_monitoring_alert_policy.backup_health_failure[0],
        google_monitoring_alert_policy.backup_health_recovery[0],
      ] : policy.project == var.project_id &&
      policy.notification_channels == var.backup_health_notification_channels &&
      policy.alert_strategy[0].notification_rate_limit[0].period == "3600s" &&
      policy.alert_strategy[0].notification_prompts == ["OPENED"]
    ])
    error_message = "Both policies must be scoped to the exact project and channels, rate-limited, and OPENED-only."
  }
  assert {
    condition = alltrue([
      for fragment in [
        "logName=\"projects/sample-project-123/logs/model-garden-backup-health\"",
        "resource.type=\"global\"",
        "jsonPayload.schemaVersion=1",
        "jsonPayload.component=\"client-backup\"",
        "jsonPayload.clientProject=\"acme-dev\"",
      ] : strcontains(google_monitoring_alert_policy.backup_health_failure[0].conditions[0].condition_matched_log[0].filter, fragment) &&
      strcontains(google_monitoring_alert_policy.backup_health_recovery[0].conditions[0].condition_matched_log[0].filter, fragment)
    ])
    error_message = "Both filters must bind the exact project, log, resource, schema and tenant."
  }
  assert {
    condition     = strcontains(google_monitoring_alert_policy.backup_health_failure[0].conditions[0].condition_matched_log[0].filter, "jsonPayload.health=\"UNHEALTHY\"") && strcontains(google_monitoring_alert_policy.backup_health_failure[0].conditions[0].condition_matched_log[0].filter, "severity>=ERROR")
    error_message = "Failure policy must match only unhealthy ERROR-level samples."
  }
  assert {
    condition     = strcontains(google_monitoring_alert_policy.backup_health_recovery[0].conditions[0].condition_matched_log[0].filter, "jsonPayload.health=\"HEALTHY\"") && strcontains(google_monitoring_alert_policy.backup_health_recovery[0].conditions[0].condition_matched_log[0].filter, "jsonPayload.incidentEvent=\"backup.incident.recovered\"") && strcontains(google_monitoring_alert_policy.backup_health_recovery[0].conditions[0].condition_matched_log[0].filter, "severity=INFO")
    error_message = "Recovery policy must match the durable transition, never ordinary healthy polls."
  }
  assert {
    condition     = google_monitoring_alert_policy.backup_health_failure[0].alert_strategy[0].auto_close == "604800s" && google_monitoring_alert_policy.backup_health_recovery[0].alert_strategy[0].auto_close == "1800s"
    error_message = "Native incident auto-close must remain explicit and never be treated as backup recovery."
  }
}

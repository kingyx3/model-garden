variable "project_id" {
  type        = string
  description = "GCP project for the shared Model Garden billing host (a Model Garden operator project, not a client's)."
}

variable "region" {
  type        = string
  description = "GCP region."
}

variable "zone" {
  type        = string
  description = "GCP zone."
}

variable "environment" {
  type        = string
  description = "dev for a trial Lago, prod for the one that invoices clients."

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be dev or prod."
  }
}

variable "platform_version" {
  type        = string
  description = "Model Garden version the billing sync is shipped from, for resource labels."
}

variable "lago_version" {
  type        = string
  description = "Lago release for the getlago/api and getlago/front images. Upgrade deliberately: Lago migrates its database on start."
  default     = "v1.55.0"

  validation {
    condition     = can(regex("^v[0-9]+\\.[0-9]+\\.[0-9]+$", var.lago_version))
    error_message = "lago_version must be a release tag such as v1.55.0."
  }
}

variable "lago_organization" {
  type        = string
  description = "Name of the Lago organization created on first start."
  default     = "Model Garden"
}

variable "lago_admin_email" {
  type        = string
  description = "Email of the first Lago administrator; the password is generated into Secret Manager (admin-password)."

  validation {
    condition     = can(regex("^[^@ ]+@[^@ ]+\\.[^@ ]+$", var.lago_admin_email))
    error_message = "lago_admin_email must be an email address."
  }
}

variable "lago_api_url" {
  type        = string
  description = "The Lago API address as an operator's browser reaches it (the IAP tunnel to port 3000)."
  default     = "http://localhost:3000"
}

variable "lago_front_url" {
  type        = string
  description = "The Lago front end address as an operator's browser reaches it (the IAP tunnel to port 80)."
  default     = "http://localhost:8080"
}

variable "billing_sources" {
  type = list(object({
    client       = string
    environment  = string
    subscription = string
    bucket       = string
    prefix       = optional(string, "")
    agents       = optional(list(string), [])
  }))
  description = "One entry per client environment to bill: its ledger export bucket (the docker-host ledger_export_bucket output), the Lago external subscription id it bills to, and its Agents (each gets a Lago filter value). Add the billing_service_account output to that environment's export_verifier_members first."
  default     = []

  validation {
    condition = alltrue([for source in var.billing_sources :
      can(regex("^[a-z0-9][a-z0-9-]{0,62}$", source.client)) && can(regex("^[a-z0-9][a-z0-9-]{0,62}$", source.environment))
    ])
    error_message = "Each billing source needs a client and environment slug."
  }
}

variable "billing_sync_minutes" {
  type        = number
  description = "How often the billing sync sends new usage to Lago."
  default     = 15

  validation {
    condition     = var.billing_sync_minutes >= 5 && var.billing_sync_minutes <= 1440
    error_message = "billing_sync_minutes must be between 5 and 1440."
  }
}

variable "machine_type" {
  type        = string
  description = "Compute Engine machine type for the billing host."
  default     = "e2-standard-2"
}

variable "data_disk_gb" {
  type        = number
  description = "Persistent disk for Postgres, Redis, Lago files and the billing sync's state."
  default     = 50

  validation {
    condition     = var.data_disk_gb >= 20 && var.data_disk_gb <= 2000
    error_message = "data_disk_gb must be between 20 and 2000."
  }
}

variable "snapshot_retention_days" {
  type        = number
  description = "Days each daily data-disk snapshot is kept."
  default     = 14
}

variable "backup_retention_days" {
  type        = number
  description = "Days each nightly Postgres dump is kept (and cannot be deleted)."
  default     = 35

  validation {
    condition     = var.backup_retention_days >= 7 && var.backup_retention_days <= 3650
    error_message = "backup_retention_days must be between 7 and 3650."
  }
}

variable "subnet_cidr" {
  type        = string
  description = "CIDR for the billing host subnet."
  default     = "10.80.0.0/28"
}

variable "host_image" {
  type        = string
  description = "Approved immutable debian-cloud boot image name. Empty uses the newest debian-12 image at apply time."
  default     = ""
}

variable "deletion_protection" {
  type        = bool
  description = "Protect the host from accidental Terraform deletion; required for prod."
  default     = false
}

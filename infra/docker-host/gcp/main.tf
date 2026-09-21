terraform {
  required_version = ">= 1.10.0"

  backend "gcs" {}

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.2"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

locals {
  name = "${var.client_slug}-${var.environment}-mg"
  labels = {
    project          = "model-garden"
    platform_version = replace(var.platform_version, ".", "-")
    client           = var.client_slug
    environment      = var.environment
    managed_by       = "terraform"
    infra_owner      = var.infrastructure_owner
  }
  startup_script = <<-EOT
    #!/bin/bash
    set -euo pipefail
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y ca-certificates curl git python3 python3-venv
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    . /etc/os-release
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $VERSION_CODENAME stable" > /etc/apt/sources.list.d/docker.list
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
    mkdir -p /opt/model-garden
    chmod 0755 /opt/model-garden
    docker compose version
    touch /var/lib/model-garden-bootstrap-complete
  EOT
}

data "google_compute_image" "debian" {
  family  = "debian-12"
  project = "debian-cloud"
}

resource "google_compute_network" "main" {
  name                    = local.name
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "main" {
  name                     = local.name
  region                   = var.region
  network                  = google_compute_network.main.id
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true

  log_config {
    aggregation_interval = "INTERVAL_5_SEC"
    flow_sampling        = 0.5
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

resource "google_compute_router" "main" {
  name    = "${local.name}-nat"
  region  = var.region
  network = google_compute_network.main.id
}

resource "google_compute_router_nat" "main" {
  name                               = "${local.name}-nat"
  router                             = google_compute_router.main.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

resource "google_compute_firewall" "iap_ssh" {
  name    = "${local.name}-iap-ssh"
  network = google_compute_network.main.name

  direction     = "INGRESS"
  source_ranges = ["35.235.240.0/20"]
  target_tags   = ["model-garden-runtime"]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

resource "google_compute_instance" "runtime" {
  #checkov:skip=CKV_GCP_38:The managed baseline relies on Google-managed disk encryption. Customer-managed/supplied keys are enabled only when a client or regulatory requirement justifies separate key control.
  name         = local.name
  machine_type = var.machine_type
  zone         = var.zone
  labels       = local.labels
  tags         = ["model-garden-runtime"]

  allow_stopping_for_update = true

  boot_disk {
    initialize_params {
      image = data.google_compute_image.debian.self_link
      size  = var.boot_disk_gb
      type  = "pd-balanced"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.main.id
  }

  metadata = {
    enable-oslogin           = "TRUE"
    block-project-ssh-keys   = "TRUE"
    model-garden-client      = var.client_slug
    model-garden-environment = var.environment
  }

  metadata_startup_script = local.startup_script

  service_account {
    email  = var.runtime_service_account
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  lifecycle {
    precondition {
      condition     = var.environment != "prod" || var.deletion_protection
      error_message = "Production Docker hosts must enable deletion_protection."
    }
  }

  deletion_protection = var.deletion_protection

  depends_on = [google_compute_router_nat.main]
}

variable "project_id" {
  type        = string
  description = "GCP project containing the client runtime."
}

variable "region" {
  type        = string
  description = "GCP region."
}

variable "zone" {
  type        = string
  description = "GCP zone."
}

variable "client_slug" {
  type        = string
  description = "Client identifier used in resource names."

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,19}$", var.client_slug))
    error_message = "client_slug must be 2-20 lowercase letters, digits or hyphens."
  }
}

variable "environment" {
  type        = string
  description = "Deployment environment."

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be dev or prod."
  }
}

variable "platform_version" {
  type        = string
  description = "Pinned Model Garden version for resource metadata."
}

variable "runtime_service_account" {
  type        = string
  description = "Runtime service account created by the one-time bootstrap."
}

variable "infrastructure_owner" {
  type        = string
  description = "Whether the cloud account/project is Model Garden-owned or client-owned."
  default     = "model-garden"

  validation {
    condition     = contains(["model-garden", "client"], var.infrastructure_owner)
    error_message = "infrastructure_owner must be model-garden or client."
  }
}

variable "machine_type" {
  type        = string
  description = "Compute Engine machine type for the Docker runtime."
  default     = "e2-standard-2"
}

variable "boot_disk_gb" {
  type        = number
  description = "Runtime boot disk size."
  default     = 40

  validation {
    condition     = var.boot_disk_gb >= 30 && var.boot_disk_gb <= 500
    error_message = "boot_disk_gb must be between 30 and 500."
  }
}

variable "subnet_cidr" {
  type        = string
  description = "CIDR for the isolated client runtime subnet."
  default     = "10.70.0.0/24"
}

variable "deletion_protection" {
  type        = bool
  description = "Protect the VM from accidental Terraform deletion; required for prod."
  default     = false
}

output "instance_name" {
  value = google_compute_instance.runtime.name
}

output "zone" {
  value = google_compute_instance.runtime.zone
}

output "network" {
  value = google_compute_network.main.name
}

output "internal_ip" {
  value = google_compute_instance.runtime.network_interface[0].network_ip
}

terraform {
  required_version = ">= 1.10.0"

  backend "azurerm" {}

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

locals {
  name = "${var.client_slug}-${var.environment}-model-garden"
  tags = {
    Project         = "model-garden"
    PlatformVersion = var.platform_version
    Client          = var.client_slug
    Environment     = var.environment
    ManagedBy       = "terraform"
  }
}

resource "azurerm_resource_group" "main" {
  name     = local.name
  location = var.location
  tags     = local.tags
}

resource "azurerm_virtual_network" "main" {
  name                = local.name
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  address_space       = ["10.50.0.0/16"]
  tags                = local.tags
}

resource "azurerm_subnet" "aks" {
  name                 = "aks"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.50.0.0/20"]
}

resource "azurerm_kubernetes_cluster" "main" {
  name                = local.name
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  dns_prefix          = replace(local.name, "_", "-")

  role_based_access_control_enabled = true
  local_account_disabled            = var.disable_local_accounts
  oidc_issuer_enabled               = true
  workload_identity_enabled         = true
  azure_policy_enabled              = true

  default_node_pool {
    name            = "system"
    node_count      = var.node_count
    vm_size         = var.node_vm_size
    vnet_subnet_id  = azurerm_subnet.aks.id
    os_disk_size_gb = 64
    type            = "VirtualMachineScaleSets"
  }

  identity {
    type = "SystemAssigned"
  }

  network_profile {
    network_plugin = "azure"
    network_policy = "azure"
    service_cidr   = "10.60.0.0/16"
    dns_service_ip = "10.60.0.10"
  }

  tags = local.tags
}

variable "client_slug" {
  type        = string
  description = "Short lowercase client identifier used in resource names."
  default     = "client"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,19}$", var.client_slug))
    error_message = "client_slug must be 2-20 lowercase letters, digits or hyphens and start with a letter/digit."
  }
}

variable "environment" {
  type        = string
  description = "Deployment environment."
  default     = "dev"

  validation {
    condition     = contains(["dev", "test", "staging", "prod"], var.environment)
    error_message = "environment must be one of dev, test, staging or prod."
  }
}

variable "platform_version" {
  type        = string
  description = "Model Garden platform version applied to resource metadata."
  default     = "dev"
}

variable "subscription_id" {
  type        = string
  description = "Azure subscription ID."
}

variable "location" {
  type        = string
  description = "Azure region."
}

variable "node_count" {
  type        = number
  description = "Desired AKS worker count."
  default     = 2

  validation {
    condition     = var.node_count >= 1 && var.node_count <= 20
    error_message = "node_count must be between 1 and 20."
  }
}

variable "node_vm_size" {
  type        = string
  description = "AKS worker VM size."
  default     = "Standard_D4s_v5"
}

variable "disable_local_accounts" {
  type        = bool
  description = "Disable AKS local admin accounts. Requires the deployment identity to have Kubernetes RBAC access."
  default     = false
}

output "cluster_name" {
  value = azurerm_kubernetes_cluster.main.name
}

output "resource_group" {
  value = azurerm_resource_group.main.name
}

output "location" {
  value = var.location
}

output "platform_version" {
  value = var.platform_version
}

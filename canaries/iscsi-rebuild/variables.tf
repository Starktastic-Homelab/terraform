variable "fixture_id" {
  type        = string
  description = "Explicit isolated fixture identity, confirmed in the caller."
  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$", var.fixture_id))
    error_message = "Use 1-32 lowercase letters, digits and interior hyphens."
  }
}

variable "vm_id" {
  type        = number
  description = "Explicit UNUSED Proxmox VM ID. Inventory must be checked by the caller."
  validation {
    condition     = var.vm_id >= 100 && var.vm_id <= 999999999 && floor(var.vm_id) == var.vm_id
    error_message = "Specify an unused integer VM ID between 100 and 999999999."
  }
}

variable "target_node" {
  type        = string
  description = "Explicit Proxmox node containing the template."
}

variable "template_name" {
  type        = string
  description = "Explicit clean cloud-init template; not the production root manifest."
}

variable "management_cidr" {
  type        = string
  description = "Reserved canary management host IPv4/CIDR, never a production node address."
  validation {
    condition     = can(cidrnetmask(var.management_cidr))
    error_message = "Specify an IPv4 CIDR."
  }
}

variable "storage_cidr" {
  type        = string
  description = "Reserved canary storage host IPv4/CIDR, distinct from the management network."
  validation {
    condition     = can(cidrnetmask(var.storage_cidr))
    error_message = "Specify an IPv4 CIDR."
  }
}

variable "management_gateway" {
  type        = string
  nullable    = true
  description = "Explicit management gateway, or null when the storage NIC has the default route."
}

variable "storage_gateway" {
  type        = string
  nullable    = true
  description = "Explicit storage gateway, or null when the management NIC has the default route."
}

variable "management_bridge" {
  type        = string
  description = "Explicit management bridge."
}

variable "storage_bridge" {
  type        = string
  description = "Explicit storage bridge."
}

variable "nameserver" {
  type        = string
  description = "Explicit DNS server IPv4 address."
}

variable "ciuser" {
  type        = string
  description = "Template cloud-init SSH user."
}

variable "ssh_public_key" {
  type        = string
  sensitive   = true
  description = "Public SSH key read from the operator-selected public key file."
}

variable "cloudinit_storage" {
  type        = string
  description = "Explicit Proxmox cloud-init disk storage."
}

variable "os_storage" {
  type        = string
  description = "Explicit Proxmox disposable OS disk storage."
}

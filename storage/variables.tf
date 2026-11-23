variable "storage_template_vmid" {
  type        = number
  description = "VMID of the Packer storage template (the manifest entry's artifact_id); ignored after creation"
}

variable "storage_template_name" {
  type        = string
  description = "PVE name of the template at storage_template_vmid (the manifest's storage entry); only the clone check reads it"

  validation {
    condition     = can(regex("^[A-Za-z0-9.-]+$", var.storage_template_name))
    error_message = "storage_template_name must be a PVE VM name (letters, digits, dots and hyphens)."
  }
}

variable "node_name" {
  type        = string
  default     = "pve"
  description = "Proxmox node that hosts the storage VM"
}

variable "vm_id" {
  type        = number
  description = "VMID of the storage VM"
}

variable "name" {
  type        = string
  description = "Name of the storage VM"
}

variable "cores" {
  type    = number
  default = 2
}

variable "memory" {
  type        = number
  default     = 2048
  description = "Memory in MiB"
}

variable "lan_bridge" {
  type    = string
  default = "vmbr0"
}

variable "lan_ipv4" {
  type        = string
  description = "Static LAN address in CIDR form; becomes ipconfig0, which ansible reads as ansible_host"
}

variable "lan_gateway" {
  type    = string
  default = null
}

variable "storage_bridge" {
  type = string
}

variable "storage_ipv4" {
  type        = string
  description = "Static storage-network address in CIDR form"
}

variable "nameserver" {
  type = string
}

variable "cloudinit_datastore" {
  type = string
}

variable "username" {
  type    = string
  default = "debian"
}

variable "ssh_pub_keys" {
  type      = list(string)
  sensitive = true
}

variable "data_disks" {
  type = list(object({
    interface         = string
    datastore_id      = string
    path_in_datastore = string
    size              = number
  }))
  description = "Existing volumes owned by a placeholder VMID, attached in place (size in GiB)"

  validation {
    # A missing path would reuse whatever volume state holds at that index; virtio0 is the root disk.
    condition     = alltrue([for d in var.data_disks : d.path_in_datastore != null && d.path_in_datastore != "" && d.interface != "virtio0"])
    error_message = "Every data disk needs a path_in_datastore and an interface other than virtio0."
  }
}

variable "tags" {
  type    = list(string)
  default = ["storage"]
}

variable "generation" {
  type        = string
  description = "Changing this asks for a rebuild; prevent_destroy refuses it until a rebuild PR lifts it"
}

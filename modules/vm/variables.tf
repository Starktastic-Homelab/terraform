variable "vm_id" {
  type        = number
  description = "Unique ID for the VM"
}

variable "name" {
  type        = string
  description = "Name of the VM"
}

variable "target_node" {
  type        = string
  description = "Target Proxmox node for the VM"
}

variable "clone" {
  type        = string
  description = "Name of the base VM/template to clone"
}

variable "scsihw" {
  type        = string
  description = "SCSI controller type (e.g., 'virtio-scsi-pci')"
  default     = "virtio-scsi-pci"
}

variable "os_type" {
  type    = string
  default = "cloud-init"
}

variable "ciuser" {
  type        = string
  description = "Username for cloud-init"
}

variable "cipassword" {
  type        = string
  description = "Username for cloud-init"
  default     = null
}

variable "sshkeys" {
  type        = string
  description = "SSH public key(s) for cloud-init"
  sensitive   = true
}

variable "cpu_type" {
  type        = string
  description = "CPU type (e.g., 'host')"
  default     = "host"
}

variable "network_bridges" {
  type        = list(string)
  description = "List of network bridges"
  default     = ["vmbr0"]
}

variable "cloudinit_storage" {
  type        = string
  description = "Storage location for cloud-init disk"
}

variable "os_disk_size" {
  type        = string
  description = "Size of the OS disk (e.g., '32G')"
}

variable "os_storage" {
  type        = string
  description = "Storage location for OS disk"
}

variable "longhorn_disk_size" {
  type        = string
  description = "Size of the dedicated Longhorn data disk (e.g., '200G'). Set to null to skip."
  default     = null
}

variable "longhorn_storage" {
  type        = string
  description = "Storage pool for the Longhorn disk"
  default     = ""
}

variable "tags" {
  type        = string
  description = "Tags for the VM"
}

variable "cores" {
  type        = number
  description = "Number of CPU cores"
}

variable "memory" {
  type        = number
  description = "Memory size in MB"
}

variable "ipconfigs" {
  type        = list(string)
  description = "List of ipconfig strings per interface"
  default     = ["ip=dhcp,ip6=dhcp"]
}

variable "nameserver" {
  type        = string
  description = "Default DNS nameserver"
}

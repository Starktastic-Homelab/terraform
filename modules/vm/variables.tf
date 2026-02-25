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

variable "machine" {
  type        = string
  description = "VM machine type (e.g., 'q35')"
  default     = "q35"
}

variable "bios" {
  type        = string
  description = "BIOS type (e.g., 'seabios')"
  default     = "seabios"
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
  description = "Password for cloud-init user (optional)"
  default     = null
  sensitive   = true
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

variable "pci_devices" {
  description = "PCI Passthrough Devices"
  type = list(object({
    host   = string
    pcie   = bool
    rombar = bool
  }))
  default = []
}

variable "usb_devices" {
  description = "USB Passthrough Devices (via Proxmox USB mappings)"
  type = list(object({
    mapping_id = string
    usb3       = optional(bool, false)
  }))
  default = []
}

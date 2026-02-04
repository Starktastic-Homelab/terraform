variable "master_count" {
  type        = number
  description = "Number of K3s master/control-plane nodes to create"
}

variable "worker_count" {
  type        = number
  description = "Number of K3s worker nodes to create"
}

variable "name_prefix" {
  type        = string
  default     = "kube"
  description = "Prefix for VM names (e.g., 'kube' produces 'kube-master-01')"
}

variable "proxmox_target_node" {
  type        = string
  default     = "pve"
  description = "Target Proxmox node where VMs will be created"
}

variable "start_vm_id" {
  type        = number
  default     = 200
  description = "Starting VM ID; subsequent VMs increment from this value"
}

variable "base_vm_name" {
  type        = string
  description = "Name of the base VM template to clone (from Packer manifest)"
}

variable "username" {
  type        = string
  default     = "debian"
  description = "Default cloud-init user for the VMs"
}

variable "master_cores" {
  type        = number
  description = "Number of CPU cores for master nodes"
}

variable "master_memory" {
  type        = number
  description = "Memory in MB for master nodes"
}

variable "worker_cores" {
  type        = number
  description = "Number of CPU cores for worker nodes"
}

variable "worker_memory" {
  type        = number
  description = "Memory in MB for worker nodes"
}

variable "cloudinit_storage" {
  type        = string
  default     = "local-zfs"
  description = "Proxmox storage pool for cloud-init drives"
}

variable "os_storage" {
  type        = string
  default     = "vm-pool"
  description = "Proxmox storage pool for OS disks"
}

variable "os_disk_size" {
  type        = string
  default     = "32G"
  description = "Size of the OS disk (e.g., '32G', '100G')"
}

variable "network_interfaces" {
  type = list(object({
    bridge       = string
    base_cidr    = string
    start_offset = number
    gateway      = optional(string)
  }))
  description = "List of network interface configurations with bridge, CIDR, IP offset, and optional gateway"
}

variable "nameserver" {
  type        = string
  default     = "1.1.1.1"
  description = "DNS nameserver for the VMs"
}

variable "ssh_pub_key" {
  type        = string
  sensitive   = true
  description = "SSH public key(s) to inject via cloud-init for the default user"
}

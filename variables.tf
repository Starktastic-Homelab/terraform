variable "master_count" {
  type = number
}

variable "worker_count" {
  type = number
}

variable "name_prefix" {
  default = "kube"
}

variable "proxmox_target_node" {
  default = "pve"
}

variable "base_vm_name" {
  type        = string
  description = "Received from Packer's build manifest"
}

variable "username" {
  default = "debian"
}

variable "cpu_type" {
  default = "host"
}

variable "master_cores" {}

variable "master_memory" {}

variable "worker_cores" {}

variable "worker_memory" {}

variable "cloudinit_storage" {
  default = "local-zfs"
}

variable "os_storage" {
  default = "vmpool"
}

variable "os_disk_size" {
  default = "32G"
}

variable "network_bridges" {
  default = ["vmbr0", "vmbr1"]
}

variable "ssh_pub_key" {
  type = string
}

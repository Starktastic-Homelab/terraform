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

variable "master_cores" {
  type = number
}

variable "master_memory" {
  type = number
}

variable "worker_cores" {
  type = number
}

variable "worker_memory" {
  type = number
}

variable "cloudinit_storage" {
  #default = "local-zfs"
  default = "local-lvm" # TEMPORARY
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

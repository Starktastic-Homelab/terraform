variable "storage_template_vmid" {
  type        = number
  description = "VMID of the Packer storage template to clone; ignored after creation"
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
}

variable "tags" {
  type    = list(string)
  default = ["storage"]
}

variable "generation" {
  type        = string
  description = "Changing this asks for a rebuild; prevent_destroy refuses it until a rebuild PR lifts it"
}

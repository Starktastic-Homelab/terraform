terraform {
  required_version = ">= 1.5.0"

  # The caller supplies an absolute, private .state/<fixture_id>/terraform.tfstate.
  backend "local" {}

  required_providers {
    proxmox = {
      source  = "telmate/proxmox"
      version = "3.0.2-rc10"
    }
  }
}

provider "proxmox" {
  pm_tls_insecure = false
  pm_log_enable   = false
  pm_debug        = false
}

module "canary" {
  source = "../../modules/vm"

  vm_id       = var.vm_id
  name        = "iscsi-rebuild-canary-${var.fixture_id}"
  target_node = var.target_node
  clone       = var.template_name
  tags        = "iscsi-rebuild-canary"

  ciuser  = var.ciuser
  sshkeys = var.ssh_public_key

  network_bridges   = [var.management_bridge, var.storage_bridge]
  cloudinit_storage = var.cloudinit_storage
  os_disk_size      = "16G"
  os_storage        = var.os_storage
  cores             = 2
  memory            = 4096
  pci_devices       = []
  usb_devices       = []

  ipconfigs = [
    var.management_gateway == null ? "ip=${var.management_cidr}" : "ip=${var.management_cidr},gw=${var.management_gateway}",
    var.storage_gateway == null ? "ip=${var.storage_cidr}" : "ip=${var.storage_cidr},gw=${var.storage_gateway}",
  ]
  nameserver = var.nameserver
}

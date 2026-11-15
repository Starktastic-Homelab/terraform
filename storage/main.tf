resource "terraform_data" "generation" {
  input = var.generation
}

resource "proxmox_virtual_environment_vm" "storage" {
  node_name = var.node_name
  vm_id     = var.vm_id
  name      = var.name
  tags      = var.tags

  clone {
    vm_id = var.storage_template_vmid
    full  = true
  }

  cpu {
    cores = var.cores
  }

  memory {
    dedicated = var.memory
  }

  network_device {
    bridge = var.lan_bridge
  }

  network_device {
    bridge = var.storage_bridge
  }

  initialization {
    datastore_id = var.cloudinit_datastore

    user_account {
      username = var.username
      keys     = var.ssh_pub_keys
    }

    # NIC order: the LAN address is ipconfig0, which ansible's inventory reads.
    ip_config {
      ipv4 {
        address = var.lan_ipv4
        gateway = var.lan_gateway
      }
    }

    ip_config {
      ipv4 {
        address = var.storage_ipv4
      }
    }

    dns {
      servers = [var.nameserver]
    }
  }

  # The data disks belong to a placeholder VMID, so destroying this VM never deletes them.
  dynamic "disk" {
    for_each = var.data_disks

    content {
      interface         = disk.value.interface
      datastore_id      = disk.value.datastore_id
      path_in_datastore = disk.value.path_in_datastore
      size              = disk.value.size
      file_format       = "raw"
      discard           = "on"
      iothread          = true
      cache             = "none"
    }
  }

  stop_on_destroy                      = true
  purge_on_destroy                     = false
  delete_unreferenced_disks_on_destroy = false

  lifecycle {
    prevent_destroy = true
    # Ansible guest_startup.yml owns startup, as for the k3s VMs.
    ignore_changes       = [clone, startup]
    replace_triggered_by = [terraform_data.generation]
  }
}

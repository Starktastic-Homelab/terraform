locals {
  ipconfigs = concat(
    var.ipconfigs,
    [for _ in range(16 - length(var.ipconfigs)) : null]
  )
}

resource "proxmox_vm_qemu" "vm" {
  vmid               = var.vm_id
  name               = var.name
  target_node        = var.target_node
  clone              = var.clone
  scsihw             = var.scsihw
  boot               = "order=virtio0"
  start_at_node_boot = true
  agent              = 1
  machine            = var.machine
  bios               = var.bios

  cpu {
    sockets = 1
    cores   = var.cores
    type    = var.cpu_type
  }

  memory = var.memory

  os_type    = var.os_type
  ciuser     = var.ciuser
  cipassword = var.cipassword
  sshkeys    = var.sshkeys

  ipconfig0  = local.ipconfigs[0]
  ipconfig1  = local.ipconfigs[1]
  ipconfig2  = local.ipconfigs[2]
  ipconfig3  = local.ipconfigs[3]
  ipconfig4  = local.ipconfigs[4]
  ipconfig5  = local.ipconfigs[5]
  ipconfig6  = local.ipconfigs[6]
  ipconfig7  = local.ipconfigs[7]
  ipconfig8  = local.ipconfigs[8]
  ipconfig9  = local.ipconfigs[9]
  ipconfig10 = local.ipconfigs[10]
  ipconfig11 = local.ipconfigs[11]
  ipconfig12 = local.ipconfigs[12]
  ipconfig13 = local.ipconfigs[13]
  ipconfig14 = local.ipconfigs[14]
  ipconfig15 = local.ipconfigs[15]

  nameserver = var.nameserver

  dynamic "network" {
    for_each = var.network_bridges

    content {
      id     = index(var.network_bridges, network.value)
      model  = "virtio"
      bridge = network.value
    }
  }

  disks {
    ide {
      ide2 {
        cloudinit {
          storage = var.cloudinit_storage
        }
      }
    }

    virtio {
      virtio0 {
        disk {
          size     = var.os_disk_size
          storage  = var.os_storage
          iothread = true
          discard  = true
        }
      }
    }
  }

  pcis {
    dynamic "pci0" {
      for_each = length(var.pci_devices) > 0 ? [var.pci_devices[0]] : []
      content {
        raw {
          raw_id = pci0.value.host
          pcie   = try(pci0.value.pcie, false)
          rombar = try(pci0.value.rombar, true)
        }
      }
    }
    dynamic "pci1" {
      for_each = length(var.pci_devices) > 1 ? [var.pci_devices[1]] : []
      content {
        raw {
          raw_id = pci1.value.host
          pcie   = try(pci1.value.pcie, false)
          rombar = try(pci1.value.rombar, true)
        }
      }
    }
    dynamic "pci2" {
      for_each = length(var.pci_devices) > 2 ? [var.pci_devices[2]] : []
      content {
        raw {
          raw_id = pci2.value.host
          pcie   = try(pci2.value.pcie, false)
          rombar = try(pci2.value.rombar, true)
        }
      }
    }
    dynamic "pci3" {
      for_each = length(var.pci_devices) > 3 ? [var.pci_devices[3]] : []
      content {
        raw {
          raw_id = pci3.value.host
          pcie   = try(pci3.value.pcie, false)
          rombar = try(pci3.value.rombar, true)
        }
      }
    }
    dynamic "pci4" {
      for_each = length(var.pci_devices) > 4 ? [var.pci_devices[4]] : []
      content {
        raw {
          raw_id = pci4.value.host
          pcie   = try(pci4.value.pcie, false)
          rombar = try(pci4.value.rombar, true)
        }
      }
    }
    dynamic "pci5" {
      for_each = length(var.pci_devices) > 5 ? [var.pci_devices[5]] : []
      content {
        raw {
          raw_id = pci5.value.host
          pcie   = try(pci5.value.pcie, false)
          rombar = try(pci5.value.rombar, true)
        }
      }
    }
    dynamic "pci6" {
      for_each = length(var.pci_devices) > 6 ? [var.pci_devices[6]] : []
      content {
        raw {
          raw_id = pci6.value.host
          pcie   = try(pci6.value.pcie, false)
          rombar = try(pci6.value.rombar, true)
        }
      }
    }
    dynamic "pci7" {
      for_each = length(var.pci_devices) > 7 ? [var.pci_devices[7]] : []
      content {
        raw {
          raw_id = pci7.value.host
          pcie   = try(pci7.value.pcie, false)
          rombar = try(pci7.value.rombar, true)
        }
      }
    }
    dynamic "pci8" {
      for_each = length(var.pci_devices) > 8 ? [var.pci_devices[8]] : []
      content {
        raw {
          raw_id = pci8.value.host
          pcie   = try(pci8.value.pcie, false)
          rombar = try(pci8.value.rombar, true)
        }
      }
    }
    dynamic "pci9" {
      for_each = length(var.pci_devices) > 9 ? [var.pci_devices[9]] : []
      content {
        raw {
          raw_id = pci9.value.host
          pcie   = try(pci9.value.pcie, false)
          rombar = try(pci9.value.rombar, true)
        }
      }
    }
    dynamic "pci10" {
      for_each = length(var.pci_devices) > 10 ? [var.pci_devices[10]] : []
      content {
        raw {
          raw_id = pci10.value.host
          pcie   = try(pci10.value.pcie, false)
          rombar = try(pci10.value.rombar, true)
        }
      }
    }
    dynamic "pci11" {
      for_each = length(var.pci_devices) > 11 ? [var.pci_devices[11]] : []
      content {
        raw {
          raw_id = pci11.value.host
          pcie   = try(pci11.value.pcie, false)
          rombar = try(pci11.value.rombar, true)
        }
      }
    }
    dynamic "pci12" {
      for_each = length(var.pci_devices) > 12 ? [var.pci_devices[12]] : []
      content {
        raw {
          raw_id = pci12.value.host
          pcie   = try(pci12.value.pcie, false)
          rombar = try(pci12.value.rombar, true)
        }
      }
    }
    dynamic "pci13" {
      for_each = length(var.pci_devices) > 13 ? [var.pci_devices[13]] : []
      content {
        raw {
          raw_id = pci13.value.host
          pcie   = try(pci13.value.pcie, false)
          rombar = try(pci13.value.rombar, true)
        }
      }
    }
    dynamic "pci14" {
      for_each = length(var.pci_devices) > 14 ? [var.pci_devices[14]] : []
      content {
        raw {
          raw_id = pci14.value.host
          pcie   = try(pci14.value.pcie, false)
          rombar = try(pci14.value.rombar, true)
        }
      }
    }
  }

  tags = var.tags

  lifecycle {
    ignore_changes = [
      startup_shutdown
    ]
  }
}

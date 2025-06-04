locals {
  ipconfigs = concat(
    var.ipconfigs,
    [for _ in range(16 - length(var.ipconfigs)) : null]
  )
}

resource "proxmox_vm_qemu" "vm" {
  name        = var.name
  target_node = var.target_node
  clone       = var.clone
  scsihw      = var.scsihw
  boot        = "order=virtio0"
  onboot      = true
  agent       = 1

  cpu_type = var.cpu_type
  cores    = var.cores
  memory   = var.memory

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
      ide0 {
        cloudinit {
          storage = var.cloudinit_storage
        }
      }
    }

    virtio {
      virtio0 {
        disk {
          size    = var.os_disk_size
          storage = var.os_storage
        }
      }
    }
  }

  tags = var.tags
}

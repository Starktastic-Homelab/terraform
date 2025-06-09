locals {
  ipconfigs = [for _ in range(length(var.network_bridges)) : "ip=dhcp,ip6=dhcp"]
}

module "master_nodes" {
  source = "./modules/vm"
  count  = var.master_count

  name = "${var.name_prefix}-master-${format("%02s", count.index + 1)}"

  target_node = var.proxmox_target_node
  clone       = var.base_vm_name

  cores  = var.master_cores
  memory = var.master_memory

  ciuser  = var.username
  sshkeys = sensitive(var.ssh_pub_key)

  network_bridges = var.network_bridges
  ipconfigs       = local.ipconfigs

  cloudinit_storage   = var.cloudinit_storage
  os_storage        = var.os_storage
  os_disk_size      = var.os_disk_size

  tags = "k3s,master"
}

module "worker_nodes" {
  source = "./modules/vm"
  count  = var.worker_count

  name = "${var.name_prefix}-worker-${format("%02s", count.index + 1)}"

  target_node = var.proxmox_target_node
  clone       = var.base_vm_name

  cores  = var.worker_cores
  memory = var.worker_memory

  ciuser  = var.username
  sshkeys = sensitive(var.ssh_pub_key)

  network_bridges = var.network_bridges
  ipconfigs       = local.ipconfigs

  cloudinit_storage = var.cloudinit_storage
  os_storage        = var.os_storage
  os_disk_size      = var.os_disk_size

  tags = "k3s,worker"
}

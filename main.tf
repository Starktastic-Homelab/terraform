locals {
  network_bridges = [for nic in var.network_interfaces : nic.bridge]

  default_gateways = [
    for nic in var.network_interfaces :
    # fallback to .1 in the subnet if gateway not specified
    coalesce(nic.gateway, cidrhost("${nic.start_ip}/${var.subnet_mask}", 1))
  ]

  master_ipconfigs = [
    for idx in range(var.master_count) : [
      for i, nic in var.network_interfaces :
      "ip=${cidrhost("${nic.start_ip}/${var.subnet_mask}", idx)},gw=${local.default_gateways[i]}"
    ]
  ]

  worker_ipconfigs = [
    for idx in range(var.worker_count) : [
      for i, nic in var.network_interfaces :
      "ip=${cidrhost("${nic.start_ip}/${var.subnet_mask}", idx + var.master_count)},gw=${local.default_gateways[i]}"
    ]
  ]
}

module "master_nodes" {
  source = "./modules/vm"
  count  = var.master_count

  vm_id = var.start_vm_id + count.index
  name  = "${var.name_prefix}-master-${format("%02d", count.index + 1)}"

  target_node = var.proxmox_target_node
  clone       = var.base_vm_name

  cores  = var.master_cores
  memory = var.master_memory

  ciuser  = var.username
  sshkeys = var.ssh_pub_key

  network_bridges   = local.network_bridges
  ipconfigs         = local.master_ipconfigs[count.index]
  cloudinit_storage = var.cloudinit_storage
  os_storage        = var.os_storage
  os_disk_size      = var.os_disk_size

  tags = "k3s,master"
}

module "worker_nodes" {
  source = "./modules/vm"
  count  = var.worker_count

  vm_id = var.start_vm_id + var.master_count + count.index
  name  = "${var.name_prefix}-worker-${format("%02d", count.index + 1)}"

  target_node = var.proxmox_target_node
  clone       = var.base_vm_name

  cores  = var.worker_cores
  memory = var.worker_memory

  ciuser  = var.username
  sshkeys = var.ssh_pub_key

  network_bridges   = local.network_bridges
  ipconfigs         = local.worker_ipconfigs[count.index]
  cloudinit_storage = var.cloudinit_storage
  os_storage        = var.os_storage
  os_disk_size      = var.os_disk_size

  tags = "k3s,worker"
}

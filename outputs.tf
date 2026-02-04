output "master_nodes" {
  description = "Master node details including VM ID, name, and IP addresses"
  value = [
    for idx, node in module.master_nodes : {
      vm_id = node.vm_id
      name  = node.name
      ips   = local.master_ipconfigs[idx]
    }
  ]
}

output "worker_nodes" {
  description = "Worker node details including VM ID, name, and IP addresses"
  value = [
    for idx, node in module.worker_nodes : {
      vm_id = node.vm_id
      name  = node.name
      ips   = local.worker_ipconfigs[idx]
    }
  ]
}

output "cluster_summary" {
  description = "Summary of the K3s cluster configuration"
  value = {
    master_count = var.master_count
    worker_count = var.worker_count
    total_nodes  = var.master_count + var.worker_count
  }
}

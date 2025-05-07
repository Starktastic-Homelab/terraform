output "master_default_ipv4_addresses" {
  value = module.master_nodes[*].default_ipv4_address
}

output "worker_default_ipv4_addresses" {
  value = module.worker_nodes[*].default_ipv4_address
}

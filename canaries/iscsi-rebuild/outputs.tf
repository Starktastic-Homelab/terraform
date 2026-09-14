output "vm_id" {
  value       = module.canary.vm_id
  description = "Disposable VM ID, not an ownership guarantee without inventory verification."
}

output "name" {
  value       = module.canary.name
  description = "Fixed-prefix canary VM name."
}

output "target_node" {
  value       = module.canary.target_node
  description = "Proxmox node hosting the canary."
}

output "vm_id" {
  description = "The VM ID of the created virtual machine"
  value       = proxmox_vm_qemu.vm.vmid
}

output "name" {
  description = "The name of the created virtual machine"
  value       = proxmox_vm_qemu.vm.name
}

output "target_node" {
  description = "The Proxmox node where the VM was created"
  value       = proxmox_vm_qemu.vm.target_node
}

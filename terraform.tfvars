master_count  = 1
master_cores  = 4
master_memory = 16384

worker_count  = 2
worker_cores  = 6
worker_memory = 28672

os_disk_size = "96G"

network_interfaces = [
  {
    bridge       = "vmbr0"
    base_cidr    = "10.9.9.0/24"
    start_offset = 50
  },
  {
    bridge       = "vmbr1"
    base_cidr    = "10.9.8.0/24"
    start_offset = 50
    gateway      = "10.9.8.1"
  }
]

nameserver = "10.9.9.1"

# Retained CSI requires the old worker cohort to disappear on a fresh control plane.
# First enablement replaces both workers; later worker-only changes stay independent.
rebuild_workers_with_control_plane = true
k3s_resource_pool                  = "k3s-csi"

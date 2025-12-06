master_count  = 1
master_cores  = 2
master_memory = 4096

worker_count  = 2
worker_cores  = 6
worker_memory = 24576

network_interfaces = [
  {
    bridge       = "vmbr0"
    base_cidr    = "10.9.9.0/24"
    start_offset = 50
    gateway      = "10.9.9.1"
  },
  {
    bridge       = "vmbr1"
    base_cidr    = "10.9.8.0/24"
    start_offset = 50
  }
]

nameserver = "10.9.9.1"

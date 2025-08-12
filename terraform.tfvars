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

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
  }
]

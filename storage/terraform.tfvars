vm_id               = 101
name                = "storage-01"
lan_ipv4            = "10.9.9.40/24"
lan_gateway         = "10.9.9.1"
storage_bridge      = "vmbr2"
storage_ipv4        = "10.99.2.10/24"
nameserver          = "10.9.9.1"
cloudinit_datastore = "local-zfs"
generation          = "1"
data_disks = [
  {
    interface         = "virtio1"
    datastore_id      = "k3s-block"
    path_in_datastore = "9998/vm-9998-disk-0.raw"
    size              = 232
    serial            = "retained"
  },
  {
    interface         = "virtio2"
    datastore_id      = "k3s-block"
    path_in_datastore = "9998/vm-9998-disk-1.raw"
    size              = 56
    serial            = "regen"
  },
]

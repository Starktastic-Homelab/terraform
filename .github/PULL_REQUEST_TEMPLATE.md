## Pre-apply actions

> [!WARNING]
> Selecting a destructive action requires checking the confirmation box.
> Drain and destroy are mutually exclusive.

- [ ] Drain cluster nodes before apply
- [ ] Destroy and recreate infrastructure before apply
- [ ] I confirm the destructive action selected above

## Proxmox CSI disks

> [!CAUTION]
> The provider rewrites every SCSI slot without `wwn=` on any in-place VM update, so
> Proxmox CSI volumes stop mounting after the reboot. Apply refuses such a plan
> (the plan comment says which VMs) unless this box is ticked before merge. If you
> tick it, restore wwn and reboot each worker right after (`docs/storage-safe-maintenance.md` "Proxmox CSI disk ownership").

- [ ] Accept that this apply strips wwn from Proxmox CSI disks

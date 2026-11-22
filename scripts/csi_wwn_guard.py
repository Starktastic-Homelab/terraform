#!/usr/bin/env python3
"""Find in-place VM updates that would strip wwn= from SCSI (Proxmox CSI) disks.

telmate/proxmox 3.0.2-rc10 rebuilds every disks[0].scsi slot from state on any VM
update, ignore_changes or not. Its nested `disks` block declares `wwn` but never
reads it from Proxmox or sends it (sdk_disks.go, terraform_disks.go), so each CSI
slot is rewritten without wwn= and after the provider's reboot the guest has no
/dev/disk/by-id/wwn-* for CSI to mount (outage 2026-11-16, terraform#241).

Usage: terraform show -json plan.tfplan | csi_wwn_guard.py   (report only, exit 0)
"""
import json
import sys

ACK_TEXT = 'Accept that this apply strips wwn from Proxmox CSI disks'


def scsi_updates(plan):
    """Addresses of proxmox_vm_qemu in-place updates whose prior state holds a SCSI disk."""
    hits = []
    for change in plan.get('resource_changes', []):
        if change.get('type') != 'proxmox_vm_qemu' or change['change']['actions'] != ['update']:
            continue
        before = change['change'].get('before') or {}
        # shortcut: reads telmate 3.0.2-rc10's disks schema (pinned exactly); the guard lapses at Retirement 5.3.
        if 'disks' not in before:
            hits.append(change['address'])  # a schema this guard cannot read fails closed
            continue
        slots = [slot for disks in before.get('disks') or [] for scsi in disks.get('scsi') or []
                 for value in scsi.values() for slot in value or []]
        if any(slot.get('disk') or slot.get('passthrough') for slot in slots):
            hits.append(change['address'])
    return hits


def acknowledged(env):
    """The dispatch input, or a PR-body line that is exactly the ticked box."""
    if env.get('EVENT_NAME') == 'workflow_dispatch':
        return env.get('ACCEPT_CSI_WWN_LOSS') == 'true'
    return any(line.strip() == '- [x] ' + ACK_TEXT for line in env.get('PR_BODY', '').splitlines())


def message(hits):
    return ('These in-place VM updates rewrite their SCSI slots without wwn=, so Proxmox CSI '
            'volumes stop mounting after the reboot: ' + ', '.join(hits) + '. Apply refuses '
            'this unless the PR body ticks "- [x] ' + ACK_TEXT + '" (or the dispatch input '
            'accept_csi_wwn_loss is set); then restore wwn by hand and reboot each worker '
            '(docs/storage-safe-maintenance.md "Proxmox CSI disk ownership").')


if __name__ == '__main__':
    hits = scsi_updates(json.load(sys.stdin))
    print(('WARNING: ' + message(hits)) if hits else 'No in-place updates of VMs holding SCSI disks.')

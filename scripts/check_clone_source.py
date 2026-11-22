#!/usr/bin/env python3
"""Exit 0 only if the Proxmox VMID given is a template; 1 if absent or not a template.

main(vmid, root_disk=(datastore, size_gib)) also compares the template's virtio0 with the plan's root disk.

Reads PROXMOX_VE_ENDPOINT and PROXMOX_VE_API_TOKEN (bpg's env), and PROXMOX_VE_INSECURE=true to skip TLS checks.
Any other failure (missing env, HTTP error) raises, so the step also fails.
"""
import json
import os
import ssl
import sys
import urllib.request


GIB = {'K': 1 / 1024 ** 2, 'M': 1 / 1024, 'G': 1, 'T': 1024}


def get(path):
    url = os.environ['PROXMOX_VE_ENDPOINT'].rstrip('/') + '/api2/json/' + path
    request = urllib.request.Request(url, headers={'Authorization': 'PVEAPIToken=' + os.environ['PROXMOX_VE_API_TOKEN']})
    context = ssl._create_unverified_context() if os.environ.get('PROXMOX_VE_INSECURE', '').lower() in ('1', 'true') else None
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        return json.load(response)['data']


def main(vmid, root_disk=None):
    row = next((r for r in get('cluster/resources?type=vm') if r.get('vmid') == vmid and r.get('template') == 1), None)
    if row is None:
        print(f'clone source {vmid} is missing or not a template', file=sys.stderr)
        return 1
    print(f'clone source {vmid} is a template')
    if root_disk is None:
        return 0
    disk = get(f"nodes/{row['node']}/qemu/{vmid}/config").get('virtio0')
    if not disk:
        print(f'clone source {vmid} has no virtio0 root disk', file=sys.stderr)
        return 1
    # e.g. local-zfs:base-902-disk-0,size=4G; an unknown size unit fails closed as a mismatch.
    options = dict(o.split('=', 1) for o in disk.split(',')[1:] if '=' in o)
    size = options.get('size', '')
    if size[-1:] not in GIB or not size[:-1].replace('.', '', 1).isdigit():
        print(f'clone source {vmid} root disk {disk!r} has no readable size', file=sys.stderr)
        return 1
    storage, gib = disk.split(':', 1)[0], float(size[:-1]) * GIB[size[-1]]
    if (storage, gib) != (root_disk[0], float(root_disk[1])):
        print(f'clone source {vmid} root disk {storage}:{gib:g}G, plan wants {root_disk[0]}:{root_disk[1]:g}G',
              file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(int(sys.argv[1])))

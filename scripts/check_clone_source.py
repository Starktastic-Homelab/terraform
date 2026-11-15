#!/usr/bin/env python3
"""Exit 0 only if the Proxmox VMID given is a template; 1 if absent or not a template.

Reads PROXMOX_VE_ENDPOINT and PROXMOX_VE_API_TOKEN (bpg's env), and PROXMOX_VE_INSECURE=true to skip TLS checks.
Any other failure (missing env, HTTP error) raises, so the step also fails.
"""
import json
import os
import ssl
import sys
import urllib.request


def main(vmid):
    url = os.environ['PROXMOX_VE_ENDPOINT'].rstrip('/') + '/api2/json/cluster/resources?type=vm'
    request = urllib.request.Request(url, headers={'Authorization': 'PVEAPIToken=' + os.environ['PROXMOX_VE_API_TOKEN']})
    context = ssl._create_unverified_context() if os.environ.get('PROXMOX_VE_INSECURE', '').lower() in ('1', 'true') else None
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        resources = json.load(response)['data']
    if any(r.get('vmid') == vmid and r.get('template') == 1 for r in resources):
        print(f'clone source {vmid} is a template')
        return 0
    print(f'clone source {vmid} is missing or not a template', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main(int(sys.argv[1])))

#!/usr/bin/env python3
"""Run one Terraform operation under the external runner's persistent lock."""
import argparse
import json
import os
from pathlib import Path
import ssl
import subprocess
import time
import urllib.parse
import urllib.request
from maintenance_lock import acquire, verify, release, advance
from csi_wwn_guard import acknowledged, scsi_updates, message


def pve_request(method, path, data=None):
    request = urllib.request.Request(
        os.environ['PM_API_URL'].rstrip('/') + path, method=method,
        data=urllib.parse.urlencode(data).encode() if data else None,
        headers={'Authorization': 'PVEAPIToken=%s=%s' % (os.environ['PM_API_TOKEN_ID'], os.environ['PM_API_TOKEN_SECRET'])})
    # Self-signed PVE certificate: the same trust the provider gets from pm_tls_insecure = true.
    with urllib.request.urlopen(request, context=ssl._create_unverified_context(), timeout=30) as response:
        return json.load(response)['data']


def vm_status(node, vmid):
    return '/nodes/%s/qemu/%s/status' % (urllib.parse.quote(node, safe=''), vmid)


def vm_targets(doc, destroy):
    """(node, vmid) of every VM the plan deletes, or of every VM in state when destroying."""
    if destroy:
        ids, modules = [], [doc.get('values', {}).get('root_module', {})]
        while modules:
            module = modules.pop()
            modules += module.get('child_modules', [])
            ids += [r['values']['id'] for r in module.get('resources', []) if r['type'] == 'proxmox_vm_qemu']
    else:
        ids = [c['change']['before']['id'] for c in doc.get('resource_changes', [])
               if c['type'] == 'proxmox_vm_qemu' and 'delete' in c['change']['actions']]
    targets = []
    for ident in ids:
        node, kind, vmid = ident.split('/')
        if kind != 'qemu' or not vmid.isdigit() or not node:
            raise ValueError('Unexpected VM id ' + ident)
        targets.append((node, vmid))
    return targets


def shutdown_vms(targets, timeout=240, poll=5):
    """Clean guest shutdowns (about 95s on these VMs) so writes reach disk before Terraform's hard stop."""
    pending = []
    for node, vmid in targets:
        if pve_request('GET', vm_status(node, vmid) + '/current')['status'] != 'stopped':
            pve_request('POST', vm_status(node, vmid) + '/shutdown', {'timeout': timeout})
            pending.append((node, vmid))
    deadline = time.monotonic() + timeout
    while pending and time.monotonic() < deadline:
        time.sleep(poll)
        pending = [t for t in pending if pve_request('GET', vm_status(*t) + '/current')['status'] != 'stopped']
    if pending:
        raise TimeoutError('still running: ' + ', '.join(vmid + '@' + node for node, vmid in pending))


def graceful_shutdown(plan, run, check_lock):
    """Shut down the VMs the reviewed plan deletes, or every VM in state when plan is None (destroy mode).

    Fails open: any problem leaves the VMs to Terraform's hard stop, as before this step existed.
    """
    targets = []
    try:
        if plan is None:
            # State JSON holds cloud-init secrets: parse it, never print it.
            targets = vm_targets(json.loads(run('terraform', 'show', '-json', capture=True).stdout), True)
        else:
            targets = vm_targets(plan, False)
        print('Graceful shutdown before Terraform: ' + (', '.join(v + '@' + n for n, v in targets) or 'none'), flush=True)
        check_lock()
        shutdown_vms(targets)
    except Exception as error:
        check_lock()  # A lost lock aborts; everything else falls back to the hard stop.
        print('::warning::Graceful shutdown skipped: %s: %s' % (type(error).__name__, error), flush=True)
    return targets


# shortcut: restarts by vmid, so after a mid-apply failure it may boot the half-built replacement that
# reused the id; compare smbios1 uuids captured before the shutdown if that ever matters.
def restart_vms(targets, check_lock):
    try:
        check_lock()
    except Exception:
        return  # Someone else owns the infrastructure now; leave it alone.
    for node, vmid in targets:
        try:
            pve_request('POST', vm_status(node, vmid) + '/start')
        except Exception as error:  # Deleted or already running.
            print('::warning::Not restarted %s@%s: %s' % (vmid, node, error), flush=True)


def execute(mode, saved_plan, root=Path('/maintenance')):
    if mode not in ('normal', 'drain', 'destroy'):
        raise ValueError('Unknown apply mode')
    changed = True
    if saved_plan and mode != 'destroy':
        try:
            code = Path('plan.exitcode').read_text().strip()
            if code not in ('0', '2') or not Path('plan.tfplan').is_file():
                raise ValueError()
        except (OSError, ValueError):
            raise ValueError('PR plan or its successful exit status is missing') from None
        changed = code == '2'
    owner = os.environ['MAINTENANCE_OWNER']
    nonce = acquire(root, 'terraform-' + mode, owner)
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        print('::add-mask::' + nonce, flush=True)

    def check_lock():
        verify(root, owner, nonce)

    def run(*args, capture=False):
        check_lock()
        return subprocess.run(list(args), check=True, text=True, capture_output=capture)

    drained_nodes = []
    plan = None
    if changed:
        run('terraform', 'init')
        if mode != 'destroy':
            # Inspect exactly what will be applied, before any drain or mutation.
            if not saved_plan:
                run('terraform', 'plan', '-out=plan.tfplan')
            plan = json.loads(run('terraform', 'show', '-json', 'plan.tfplan', capture=True).stdout)
            hits = scsi_updates(plan)
            if hits and not acknowledged(os.environ):
                release(root, owner, nonce)  # nothing has changed yet, so the refusal holds no lock
                raise SystemExit('Refusing apply. ' + message(hits))
            if hits:
                print('::warning::Acknowledged: ' + message(hits), flush=True)
        if mode == 'drain':
            nodes = json.loads(run('kubectl', 'get', 'nodes', '-o', 'json', capture=True).stdout)['items']
            workers = []; masters = []
            for node in nodes:
                labels = node['metadata'].get('labels', {})
                group = masters if any(k in labels for k in ('node-role.kubernetes.io/master', 'node-role.kubernetes.io/control-plane')) else workers
                group.append(node['metadata']['name'])
                if not node.get('spec', {}).get('unschedulable', False):
                    drained_nodes.append(node['metadata']['name'])
            for name in workers + masters:
                run('kubectl', 'cordon', name)
            for name in workers + masters:
                run('kubectl', 'drain', name, '--ignore-daemonsets', '--delete-emptydir-data', '--disable-eviction', '--timeout=120s')
        advance(root, owner, nonce, 'acquired', 'applying')
        targets = graceful_shutdown(plan, run, check_lock)
        try:
            if mode == 'destroy':
                run('terraform', 'destroy', '-auto-approve')
            run('terraform', 'apply', '-auto-approve' if mode == 'destroy' else 'plan.tfplan')
        except BaseException:
            restart_vms(targets, check_lock)  # A failed apply must not leave the cluster powered off.
            raise
        advance(root, owner, nonce, 'applying', 'recovering')
    release(root, owner, nonce)
    # Fresh guests do not run k3s yet. Ansible recovers scheduling after install.
    # Failed mutations retain ownership and never publish a success handoff.
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            stream.write('has_changes=' + str(changed).lower() + '\n')
            stream.write('drained_nodes=' + json.dumps(drained_nodes, separators=(',', ':')) + '\n')
    return changed


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', required=True, choices=['normal', 'drain', 'destroy'])
    parser.add_argument('--saved-plan', action='store_true')
    args = parser.parse_args()
    execute(args.mode, args.saved_plan)

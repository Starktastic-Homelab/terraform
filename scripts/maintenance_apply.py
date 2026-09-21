#!/usr/bin/env python3
"""Run one Terraform operation under the external runner's persistent lock."""
import argparse
import json
import os
from pathlib import Path
import subprocess
from maintenance_lock import acquire, verify, release, advance


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

    def run(*args):
        verify(root, owner, nonce)
        return subprocess.run(list(args), check=True, text=True, capture_output=args[:3] == ('kubectl', 'get', 'nodes'))

    if changed:
        if mode == 'drain':
            nodes = json.loads(run('kubectl', 'get', 'nodes', '-o', 'json').stdout)['items']
            workers = []; masters = []
            for node in nodes:
                labels = node['metadata'].get('labels', {})
                group = masters if any(k in labels for k in ('node-role.kubernetes.io/master', 'node-role.kubernetes.io/control-plane')) else workers
                group.append(node['metadata']['name'])
            for name in workers + masters:
                run('kubectl', 'cordon', name)
            for name in workers + masters:
                run('kubectl', 'drain', name, '--ignore-daemonsets', '--delete-emptydir-data', '--disable-eviction', '--timeout=120s')
        advance(root, owner, nonce, 'acquired', 'applying')
        run('terraform', 'init')
        if mode == 'destroy':
            run('terraform', 'destroy', '-auto-approve')
        run('terraform', 'apply', 'plan.tfplan' if saved_plan and mode != 'destroy' else '-auto-approve')
        advance(root, owner, nonce, 'applying', 'recovering')
        if mode == 'drain':
            # Failure or uncertainty intentionally retains the operation; no always-unlock.
            nodes = json.loads(run('kubectl', 'get', 'nodes', '-o', 'json').stdout)['items']
            for node in nodes:
                run('kubectl', 'uncordon', node['metadata']['name'])
            run('kubectl', 'wait', '--for=condition=Ready', 'nodes', '--all', '--timeout=300s')
    release(root, owner, nonce)
    return changed


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', required=True, choices=['normal', 'drain', 'destroy'])
    parser.add_argument('--saved-plan', action='store_true')
    args = parser.parse_args()
    changed = execute(args.mode, args.saved_plan)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            stream.write('has_changes=' + str(changed).lower() + '\n')

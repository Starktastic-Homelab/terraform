#!/usr/bin/env python3
"""Real provider planning, synthetic local state, closed endpoint; never apply.

Terraform provider mocks omit SDK ForceNew planning, so this test uses the pinned
provider with refresh and its API permission probe disabled in a temporary copy.
Production backend/provider configuration and state are never used or modified.
"""
import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
TERRAFORM = os.environ.get('TERRAFORM', 'terraform')
MASTER = 'module.master_nodes[0].proxmox_vm_qemu.vm'
WORKERS = [f'module.worker_nodes[{i}].proxmox_vm_qemu.vm' for i in range(2)]
NODES = [MASTER, *WORKERS]
OLD = '00000000-0000-4000-8000-000000000001'
NEW = '00000000-0000-4000-8000-000000000002'
ENV = {k: v for k, v in os.environ.items() if not k.startswith(('TF_', 'PM_', 'AWS_'))}


def run(root, *args, success=True):
    result = subprocess.run([TERRAFORM, '-chdir=' + str(root), *args], env=ENV,
                            capture_output=True, text=True, timeout=120)
    if success and result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return result


def plan(root, *args):
    run(root, 'plan', '-refresh=false', '-input=false', '-no-color', '-out=case.tfplan', *args)
    return json.loads(run(root, 'show', '-json', 'case.tfplan').stdout)


def state_from_plan(document):
    resources = []
    for module in document['planned_values']['root_module']['child_modules']:
        for resource in module['resources']:
            assert resource['type'] == 'proxmox_vm_qemu'
            attributes = resource['values']
            attributes['unused_disk'] = []
            attributes['id'] = 'offline/qemu/' + str(attributes['vmid'])
            attributes['smbios'] = [dict(uuid=OLD, family='', manufacturer='', product='',
                                         serial='', sku='', version='')]
            if resource['address'] in WORKERS:
                attributes['force_recreate_on_change_of'] = json.dumps([OLD], separators=(',', ':'))
            resources.append(dict(module=module['address'], mode='managed', type=resource['type'],
                                  name=resource['name'], provider='provider["registry.terraform.io/telmate/proxmox"]',
                                  instances=[dict(schema_version=resource['schema_version'], attributes=attributes,
                                                  sensitive_attributes=[[{'type': 'get_attr', 'value': key}]
                                                                        for key in ('sshkeys', 'cipassword', 'ssh_private_key')])]))
    return dict(version=4, serial=1, lineage=str(uuid.uuid4()), outputs={}, resources=resources)


def attributes(state, address):
    return next(r['instances'][0]['attributes'] for r in state['resources']
                if r['module'] + '.' + r['type'] + '.' + r['name'] == address)


def main():
    with tempfile.TemporaryDirectory(prefix='terraform-cohort-') as directory:
        root = Path(directory)
        for source in ROOT.glob('*.tf'):
            shutil.copy(source, root / source.name)
        shutil.copytree(ROOT / 'modules', root / 'modules')
        provider = (root / 'providers.tf').read_text()
        provider, count = re.subn(r'  backend "s3" \{.*?\n  \}\n', '', provider, flags=re.S)
        assert count == 1, 'Expected exactly one production backend to remove'
        provider, count = re.subn(r'provider "proxmox" \{.*?\n\}', '''provider "proxmox" {
  pm_api_url = "https://127.0.0.1:1/api2/json"
  pm_api_token_id = "offline@pve!test"
  pm_api_token_secret = "unused-test-placeholder"
  pm_minimum_permission_check = false
}''', provider, flags=re.S)
        assert count == 1, 'Expected exactly one provider to isolate'
        (root / 'providers.tf').write_text(provider)
        fixture = dict(base_vm_name='offline-template', master_count=1, worker_count=2,
                       master_cores=2, master_memory=2048, worker_cores=2, worker_memory=2048,
                       network_interfaces=[dict(bridge='offline', base_cidr='192.0.2.0/24', start_offset=10)],
                       ssh_pub_key='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA offline-fixture')
        (root / 'fixture.auto.tfvars.json').write_text(json.dumps(fixture))
        init = ['init', '-backend=false', '-input=false', '-no-color']
        if os.environ.get('COHORT_PLUGIN_DIR'):
            init.append('-plugin-dir=' + os.environ['COHORT_PLUGIN_DIR'])
        run(root, *init)
        enabled = '-var=rebuild_workers_with_control_plane=true'
        state = state_from_plan(plan(root, enabled))
        state_file = root / 'terraform.tfstate'
        state_file.write_text(json.dumps(state))
        # Normalize SDK optional nested defaults using a plan, never an apply.
        normalized = plan(root, enabled)
        for change in normalized['resource_changes']:
            attributes(state, change['address']).update(change['change']['after'])
            attributes(state, change['address'])['unused_disk'] = []

        def check(name, sample, expected, *options):
            state_file.write_text(json.dumps(sample))
            document = plan(root, *options)
            actual = {r['address']: r['change']['actions'] for r in document['resource_changes']}
            changed = {r['address']: [k for k, v in (r['change']['after'] or {}).items()
                                      if v != (r['change']['before'] or {}).get(k)]
                       for r in document['resource_changes']}
            assert actual == expected, f'{name}: expected {expected}, got {actual}; changed fields: {changed}'
            print(name + ': passed', flush=True)
            return document

        noop = {node: ['no-op'] for node in NODES}
        check('unchanged', state, noop, enabled)
        check('master in-place update', state, dict(noop, **{MASTER: ['update']}), enabled, '-var=master_memory=4096')
        check('replace master', state, {node: ['delete', 'create'] for node in NODES}, enabled, '-replace=' + MASTER)
        check('replace one worker', state, dict(noop, **{WORKERS[0]: ['delete', 'create']}), enabled, '-replace=' + WORKERS[0])
        check('Packer image change', state, {node: ['delete', 'create'] for node in NODES}, enabled, '-var=base_vm_name=new-template')

        interrupted = copy.deepcopy(state)
        attributes(interrupted, MASTER)['smbios'][0]['uuid'] = NEW
        check('retry after master replacement', interrupted,
              dict(noop, **{node: ['delete', 'create'] for node in WORKERS}), enabled)
        attributes(interrupted, WORKERS[0])['force_recreate_on_change_of'] = json.dumps([NEW], separators=(',', ':'))
        check('retry after one worker replacement', interrupted,
              dict(noop, **{WORKERS[1]: ['delete', 'create']}), enabled)

        legacy = copy.deepcopy(state)
        for node in WORKERS:
            attributes(legacy, node)['force_recreate_on_change_of'] = None
        check('disabled by source default', legacy, noop)
        check('first activation replaces workers', legacy,
              dict(noop, **{node: ['delete', 'create'] for node in WORKERS}), enabled)
        pool = '-var=k3s_resource_pool=offline-csi'
        document = check('pool enrollment updates VMs without replacement', legacy,
                         {node: ['update'] for node in NODES}, pool)
        enrolled = copy.deepcopy(legacy)
        for change in document['resource_changes']:
            assert change['change']['after']['pool'] == 'offline-csi'
            attributes(enrolled, change['address']).update(change['change']['after'])
        check('existing pool membership is stable', enrolled, noop, pool)
        document = check('worker recreation retains pool membership', enrolled,
                         dict(noop, **{WORKERS[0]: ['delete', 'create']}), pool, '-replace=' + WORKERS[0])
        assert all(change['change']['after']['pool'] == 'offline-csi' for change in document['resource_changes'])
        attributes(interrupted, MASTER)['smbios'] = []
        state_file.write_text(json.dumps(interrupted))
        refusal = run(root, 'plan', '-refresh=false', '-input=false', '-no-color', enabled, success=False)
        assert refusal.returncode != 0 and 'exactly one control-plane VM' in refusal.stdout + refusal.stderr
        print('missing master UUID refused: passed', flush=True)
        print('13 offline real-provider planning checks passed; no API access or apply.')


if __name__ == '__main__':
    main()

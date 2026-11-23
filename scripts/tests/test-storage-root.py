#!/usr/bin/env python3
"""Real bpg provider planning of the storage/ root, synthetic local state; never apply.

Needs the terraform binary (TERRAFORM=) and the bpg provider (registry, or STORAGE_PLUGIN_DIR=).
Like test-rebuild-cohort.py, it plans a temporary copy (tf_offline.py) with refresh off, a local backend
and a closed endpoint, so production backend, provider configuration and state are never used.
"""
import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tf_offline import ENV, TERRAFORM, prepare  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
STORAGE = ROOT / 'storage'
VM = 'proxmox_virtual_environment_vm.storage'
GENERATION = 'terraform_data.generation'
PROVIDER = 'provider["registry.terraform.io/bpg/proxmox"]'
KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA offline-fixture'
DISKS = [dict(interface='virtio1', datastore_id='k3s-block', path_in_datastore='9990/vm-9990-disk-0.raw', size=64,
              serial='retained'),
         dict(interface='virtio2', datastore_id='k3s-block', path_in_datastore='9990/vm-9990-disk-1.raw', size=32,
              serial='regen')]
FIXTURE = dict(storage_template_vmid=902, vm_id=250, name='offline-storage',
               lan_ipv4='192.0.2.20/24', lan_gateway=None, storage_bridge='offline1',
               storage_ipv4='198.51.100.20/24', nameserver='192.0.2.1', cloudinit_datastore='local-zfs',
               ssh_pub_keys=[KEY], data_disks=DISKS, generation='1', storage_template_name='packer-storage-13.7.0-1')
# Source references to the storage root, not every mention of it (comments may name storage/).
STORAGE_REFERENCE = re.compile(r'^\s*(source|path|config)\s*=\s*"[^"]*storage|data\s+"terraform_remote_state"', re.M)
POOL_ID = re.compile(r'^\s*pool_id\s*=|variable\s+"pool_id"', re.M)


def run(root, *args, success=True, env=None):
    result = subprocess.run([TERRAFORM, '-chdir=' + str(root), *args], env={**ENV, **(env or {})},
                            capture_output=True, text=True, timeout=300)
    if success and result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return result


def plan(root, *args, env=None):
    run(root, 'plan', '-refresh=false', '-input=false', '-no-color', '-out=case.tfplan', *args, env=env)
    return json.loads(run(root, 'show', '-json', 'case.tfplan').stdout)


def planned(document, address=VM):
    return next(r['values'] for r in document['planned_values']['root_module']['resources']
                if r['address'] == address)


def generation_input(document):
    return planned(document, GENERATION)['input']


def actions(document):
    return {r['address']: r['change']['actions'] for r in document['resource_changes']}


class StorageRoot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='terraform-storage-')
        cls.root = root = Path(cls.tmp.name)
        prepare(STORAGE, root, os.environ.get('STORAGE_PLUGIN_DIR'))
        (root / 'fixture.auto.tfvars.json').write_text(json.dumps(FIXTURE))
        cls.created = plan(root)
        cls.state = cls.state_from_plan(cls.created)
        cls.state_file = root / 'terraform.tfstate'
        cls.state_file.write_text(json.dumps(cls.state))
        # Normalize SDK optional nested defaults using a plan, never an apply.
        for change in plan(root)['resource_changes']:
            if change['address'] == VM:
                cls.vm(cls.state).update(change['change']['after'])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @staticmethod
    def state_from_plan(document):
        resources = []
        for resource in document['planned_values']['root_module']['resources']:
            attributes = copy.deepcopy(resource['values'])
            sensitive = []
            if resource['address'] == VM:
                # Computed values a real read would fill in.
                attributes.update(id=str(attributes['vm_id']), boot_order=['virtio0'], ipv4_addresses=[],
                                  ipv6_addresses=[], mac_addresses=[], network_interface_names=[])
                first = dict(type='index', value=dict(value=0, type='number'))
                sensitive = [[dict(type='get_attr', value='initialization'), first,
                              dict(type='get_attr', value='user_account'), first, dict(type='get_attr', value=key)]
                             for key in ('keys', 'password')]
            else:
                value = dict(value=attributes['input'], type='string')
                attributes.update(id=str(uuid.uuid4()), input=value, output=value)
            resources.append(dict(mode='managed', type=resource['type'], name=resource['name'],
                                  provider=PROVIDER if resource['address'] == VM
                                  else 'provider["terraform.io/builtin/terraform"]',
                                  instances=[dict(schema_version=resource['schema_version'],
                                                  attributes=attributes, sensitive_attributes=sensitive)]))
        return dict(version=4, serial=1, lineage=str(uuid.uuid4()), outputs={}, resources=resources)

    @staticmethod
    def vm(state):
        return next(r['instances'][0]['attributes'] for r in state['resources'] if r['type'] + '.' + r['name'] == VM)

    def plan_against(self, state, *args, success=True, env=None):
        self.state_file.write_text(json.dumps(state))
        if not success:
            return run(self.root, 'plan', '-refresh=false', '-input=false', '-no-color', *args, success=False, env=env)
        return plan(self.root, *args, env=env)

    def planned_vm(self):
        return next(r['values'] for r in self.created['planned_values']['root_module']['resources']
                    if r['address'] == VM)

    def test_unchanged_is_noop(self):
        self.assertEqual(actions(self.plan_against(self.state)), {VM: ['no-op'], GENERATION: ['no-op']})

    def test_template_change_is_noop(self):
        # Every packer build renames the template, so a manifest PR must leave the storage root untouched.
        self.assertEqual(actions(self.plan_against(self.state, '-var=storage_template_vmid=903',
                                                   '-var=storage_template_name=packer-storage-13.7.0-2')),
                         {VM: ['no-op'], GENERATION: ['no-op']})

    def test_template_name_in_saved_plan(self):
        # maintenance_apply.py reads it from terraform show -json, though no resource references it.
        self.assertEqual(self.created['variables']['storage_template_name']['value'], 'packer-storage-13.7.0-1')

    def test_template_name_validated(self):
        for bad in ('', 'bad name'):
            with self.subTest(bad=bad):
                result = self.plan_against(self.state, '-var=storage_template_name=' + bad, success=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('storage_template_name must be a PVE VM name', result.stdout + result.stderr)

    def test_generation_change_refused(self):
        result = self.plan_against(self.state, '-var=generation=2', success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Instance cannot be destroyed', result.stdout + result.stderr)

    def test_data_disks_attach_existing(self):
        vm = self.planned_vm()
        self.assertEqual((vm['clone'][0]['vm_id'], vm['clone'][0]['full']), (902, True))
        disks = {d['interface']: d for d in vm['disk']}
        self.assertEqual(sorted(disks), ['virtio0', 'virtio1', 'virtio2'])
        # The cloned root disk, at the template's datastore and size: other values move or grow it at clone.
        root = disks['virtio0']
        self.assertEqual((root['datastore_id'], root['size'], root['discard'], root['replicate']),
                         ('local-zfs', 4, 'on', False))
        self.assertFalse(root.get('path_in_datastore'))
        for expected in DISKS:
            disk = disks[expected['interface']]
            for key in ('datastore_id', 'path_in_datastore', 'size', 'serial'):
                self.assertEqual(disk[key], expected[key], key)
            self.assertEqual((disk['file_format'], disk['discard'], disk['iothread']), ('raw', 'on', True))
        # A manual size change made outside Terraform shows up as a diff, never a replacement.
        drifted = copy.deepcopy(self.state)
        next(d for d in self.vm(drifted)['disk'] if d['interface'] == 'virtio1')['size'] = 65
        self.assertNotIn('delete', actions(self.plan_against(drifted))[VM])

    def test_root_disk_added_to_existing_state(self):
        # State written before the root disk was declared holds only the data disks (bpg reads back
        # only declared interfaces). Adding virtio0 must be an in-place update that keeps each data
        # disk on its own volume, never a replacement or a shifted path.
        old = copy.deepcopy(self.state)
        self.vm(old)['disk'] = [d for d in self.vm(old)['disk'] if d['interface'] != 'virtio0']
        change = next(r['change'] for r in self.plan_against(old)['resource_changes'] if r['address'] == VM)
        self.assertEqual(change['actions'], ['update'])
        after = {d['interface']: d for d in change['after']['disk']}
        self.assertEqual(sorted(after), ['virtio0', 'virtio1', 'virtio2'])
        for expected in DISKS:
            self.assertEqual(after[expected['interface']]['path_in_datastore'], expected['path_in_datastore'])
        self.assertNotIn(after['virtio0'].get('path_in_datastore'), [d['path_in_datastore'] for d in DISKS])
        self.assertEqual((after['virtio0']['datastore_id'], after['virtio0']['size']), ('local-zfs', 4))

    def test_data_disk_needs_path(self):
        for bad in (dict(DISKS[0], path_in_datastore=''), dict(DISKS[0], interface='virtio0')):
            result = self.plan_against(self.state, '-var=data_disks=' + json.dumps([bad, DISKS[1]]), success=False)
            self.assertIn('Every data disk needs a path_in_datastore', result.stdout + result.stderr)

    def test_data_disk_serials_validated(self):
        # The guest finds each disk by /dev/disk/by-id/virtio-<serial>, which virtio-blk caps at 20 bytes.
        for bad in ([DISKS[0], dict(DISKS[1], serial='retained')], [dict(DISKS[0], serial=''), DISKS[1]],
                    [dict(DISKS[0], serial='x' * 21), DISKS[1]], [dict(DISKS[0], serial='rétained'), DISKS[1]]):
            result = self.plan_against(self.state, '-var=data_disks=' + json.dumps(bad), success=False)
            self.assertIn('Every data disk needs a distinct serial', result.stdout + result.stderr)
        self.plan_against(self.state, '-var=data_disks=' + json.dumps([dict(DISKS[0], serial='x' * 20), DISKS[1]]))

    def test_production_tfvars(self):
        # The committed storage/terraform.tfvars plus only the CI-supplied keys, no fixture.
        with tempfile.TemporaryDirectory(prefix='terraform-storage-prod-') as tmp:
            prod = Path(tmp)
            prepare(STORAGE, prod, os.environ.get('STORAGE_PLUGIN_DIR'))
            shutil.copy(STORAGE / 'terraform.tfvars', prod / 'terraform.tfvars')
            (prod / 'keys.auto.tfvars.json').write_text(json.dumps(dict(ssh_pub_keys=[KEY])))
            prod = plan(prod)
        vm = planned(prod)
        self.assertEqual((vm['clone'][0]['vm_id'], vm['vm_id'], vm['name']), (902, 101, 'storage-01'))
        self.assertIsNone(vm.get('pool_id'))
        self.assertEqual(vm['tags'], ['storage'])
        disks = {d['interface']: (d['datastore_id'], d.get('path_in_datastore'), d['size']) for d in vm['disk']}
        self.assertEqual(disks['virtio1'], ('k3s-block', '9998/vm-9998-disk-0.raw', 232))
        self.assertEqual(disks['virtio2'], ('k3s-block', '9998/vm-9998-disk-1.raw', 56))
        # The placeholder VMID owns the data disks; it must never be a real VM or template.
        self.assertNotIn(9998, (9999, 900, 901, 902, 200, 201, 202, 300, 100))
        self.assertEqual({d['interface']: d['serial'] for d in vm['disk'] if d['interface'] != 'virtio0'},
                         {'virtio1': 'retained', 'virtio2': 'regen'})
        self.assertEqual([c['ipv4'][0]['address'] for c in vm['initialization'][0]['ip_config']],
                         ['10.9.9.40/24', '10.99.2.10/24'])
        self.assertEqual(vm['initialization'][0]['ip_config'][0]['ipv4'][0]['gateway'], '10.9.9.1')
        self.assertEqual([n['bridge'] for n in vm['network_device']], ['vmbr0', 'vmbr2'])
        self.assertEqual(generation_input(prod), '1')

    def test_destroy_keeps_unreferenced_disks(self):
        vm = self.planned_vm()
        self.assertEqual((vm['stop_on_destroy'], vm['purge_on_destroy'], vm['delete_unreferenced_disks_on_destroy']),
                         (True, False, False))
        self.assertFalse(vm['reboot_after_update'])
        self.assertEqual(vm['cpu'][0]['type'], 'host')
        result = self.plan_against(self.state, '-destroy', success=False)
        self.assertIn('Instance cannot be destroyed', result.stdout + result.stderr)

    def test_cloud_init_user_and_keys(self):
        vm = self.planned_vm()
        init = vm['initialization'][0]
        self.assertEqual(init['datastore_id'], 'local-zfs')
        self.assertEqual(init['user_account'][0]['username'], 'debian')
        self.assertEqual(init['user_account'][0]['keys'], [KEY])
        # LAN first: ansible's inventory composes ansible_host from ipconfig0.
        self.assertEqual([c['ipv4'][0] for c in init['ip_config']],
                         [dict(address='192.0.2.20/24', gateway=None), dict(address='198.51.100.20/24', gateway=None)])
        self.assertEqual([n['bridge'] for n in vm['network_device']], ['vmbr0', 'offline1'])
        self.assertEqual(init['dns'][0]['servers'], ['192.0.2.1'])
        self.assertEqual(vm['tags'], ['storage'])
        gateway = self.plan_against(self.state, '-var=lan_gateway=192.0.2.1')
        after = next(r['change']['after'] for r in gateway['resource_changes'] if r['address'] == VM)
        self.assertEqual(after['initialization'][0]['ip_config'][0]['ipv4'][0]['gateway'], '192.0.2.1')
        self.assertIn(KEY, json.dumps(self.created['planned_values']))
        self.assertIn('ssh_pub_keys', self.created['configuration']['root_module']['variables'])
        self.assertTrue(self.created['configuration']['root_module']['variables']['ssh_pub_keys']['sensitive'])

    def test_no_pool_id(self):
        # The storage VM is never in a resource pool (ADR 0017: not in the Proxmox CSI pool).
        for name in ('main.tf', 'variables.tf'):
            self.assertNotRegex((STORAGE / name).read_text(), POOL_ID, name)
        self.assertIsNone(self.planned_vm().get('pool_id'))

    def test_startup_ignored_and_cache_none(self):
        self.assertEqual({d['interface']: d['cache'] for d in self.planned_vm()['disk']},
                         dict(virtio0='none', virtio1='none', virtio2='none'))
        started = copy.deepcopy(self.state)
        self.vm(started)['startup'] = [dict(order=2, up_delay=60, down_delay=-1)]
        self.assertEqual(actions(self.plan_against(started)), {VM: ['no-op'], GENERATION: ['no-op']})

    def test_base_image_change_leaves_storage_untouched(self):
        # A Packer image change sets TF_VAR_base_vm_name for the k3s root; it must never reach this one.
        self.assertNotIn('base_vm_name', self.created['configuration']['root_module'].get('variables', {}))
        for source in STORAGE.glob('*.tf'):
            self.assertNotIn('packer-manifest', source.read_text(), source)
        doc = self.plan_against(self.state, env={'TF_VAR_base_vm_name': 'new-template'})
        self.assertEqual(actions(doc), {VM: ['no-op'], GENERATION: ['no-op']})

    def test_root_is_independent(self):
        for source in [*ROOT.glob('*.tf'), *ROOT.glob('modules/**/*.tf')]:
            self.assertNotRegex(source.read_text(), STORAGE_REFERENCE, source)
        key = re.compile(r'^\s*key\s*=\s*"([^"]+)"', re.M)
        self.assertEqual(key.findall((ROOT / 'providers.tf').read_text()), ['terraform.tfstate'])
        self.assertEqual(key.findall((STORAGE / 'providers.tf').read_text()), ['storage/terraform.tfstate'])


class SourceGreps(unittest.TestCase):
    def test_greps_catch_references_not_comments(self):
        for text in ('module "s" {\n  source = "../storage"\n}\n',
                     'data "terraform_remote_state" "s" {\n  backend = "s3"\n}\n'):
            self.assertRegex(text, STORAGE_REFERENCE)
        self.assertNotRegex('# The storage VM lives in storage/, its own root.\n', STORAGE_REFERENCE)
        for text in ('  pool_id = "lab"\n', 'variable "pool_id" {\n}\n'):
            self.assertRegex(text, POOL_ID)
        self.assertNotRegex('# No pool_id: never in the CSI pool.\n', POOL_ID)


if __name__ == '__main__':
    unittest.main()

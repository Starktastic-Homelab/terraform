#!/usr/bin/env python3
"""The storage root's CI wiring: workflow structure, and its step scripts run under `sh -e` against fakes.

Step scripts run with sh, not bash: the hashicorp/terraform image has no bash until the S3 step installs it,
and drift.yml never installs it. No real terraform, Proxmox, S3 or GitHub is contacted.
"""
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / '.github/workflows'
KEY1 = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA one'
KEY2 = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB two'
FAKE_PM = dict(PM_API_URL='https://192.0.2.5:8006/api2/json', PM_API_TOKEN_ID='ci@pve!t', PM_API_TOKEN_SECRET='s')
TEMPLATE = {'name': 'storage', 'artifact_id': '902', 'custom_data': {'vm_name': 'packer-storage-13.7.0-1'}}
OTHER = {'name': 'k3s', 'artifact_id': '901', 'custom_data': {'vm_name': 'packer-k3s-13.7.0-1'}}
PR = '/pr-${{ github.event.pull_request.number }}/'
K3S_NAME = 'packer-k3s-13.7.0-1'
K3S, OLD, STORAGE = ({'name': n, 'custom_data': {'vm_name': v}} for n, v in (
    ('k3s', K3S_NAME), ('debian-13', 'packer-debian-13.7.0-0'), ('storage', 'packer-storage-13.7.0-1')))
JQ = re.compile(r"jq -er '([^']+)' packer-manifest\.json")


def workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def step(job, key, value):
    return next(s for s in job['steps'] if s.get(key) == value)


def index(job, key, value):
    return job['steps'].index(step(job, key, value))


class Sandbox:
    """A temp checkout with scripts/ linked in, fake executables first on PATH, and a GITHUB_OUTPUT file."""

    def __init__(self, test, plan_exit=0, tfvars=False, manifest=(OTHER, TEMPLATE)):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / 'scripts').symlink_to(ROOT / 'scripts')
        (self.dir / 'storage').mkdir()
        if tfvars:
            (self.dir / 'storage/terraform.tfvars').write_text('vm_id = 250\n')
        if manifest is not None:
            (self.dir / 'packer-manifest.json').write_text(json.dumps({'builds': list(manifest)}))
        self.bin = self.dir / 'bin'
        self.bin.mkdir()
        self.fake('terraform', 'echo "$*" >> "$CALLS"\ncase " $* " in *" plan "*)\n'
                  '  echo "$TF_VAR_storage_template_vmid $TF_VAR_storage_template_name" >> "$CALLS.vars"\n'
                  '  echo fake plan; exit %d;; esac\n' % plan_exit)
        self.fake('apk', 'exit 0\n')
        self.fake('python3', 'if [ "$1" = -c ]; then exec %s "$@"; fi\necho "$*" >> "$CALLS"\n' % sys.executable)
        self.output = self.dir / 'output'
        self.output.touch()
        self.calls = self.dir / 'calls'
        self.calls.touch()

    def fake(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/sh\n' + body)
        path.chmod(0o755)

    def run(self, script, env):
        env = dict(env, PATH=str(self.bin) + os.pathsep + os.environ['PATH'], GITHUB_OUTPUT=str(self.output),
                   CALLS=str(self.calls), HOME=str(self.dir))
        return subprocess.run(['sh', '-e', '-c', script], cwd=self.dir, env=env, text=True, capture_output=True)

    def outputs(self):
        return dict(line.split('=', 1) for line in self.output.read_text().splitlines())

    def read(self, name):
        return (self.dir / name).read_text()


def workflow_env(job, step_):
    """The env a workflow gives one step (job env, then step env), with expressions replaced by fakes."""
    fakes = {'secrets.PM_API_URL': FAKE_PM['PM_API_URL'], 'secrets.PM_API_TOKEN_ID': FAKE_PM['PM_API_TOKEN_ID'],
             'secrets.PM_API_TOKEN_SECRET': FAKE_PM['PM_API_TOKEN_SECRET'], 'vars.TF_VAR_SSH_PUB_KEY': KEY1}
    env = {}
    for key, value in {**job.get('env', {}), **step_.get('env', {})}.items():
        match = re.fullmatch(r'\$\{\{\s*([\w.]+)\s*\}\}', str(value))
        env[key] = fakes.get(match.group(1), 'fake') if match else str(value)
    return env


def offline_env(**extra):
    return {**FAKE_PM, 'TF_VAR_ssh_pub_key': KEY1, **extra}


class BpgEnv(unittest.TestCase):
    def source(self, **env):
        base = {k: v for k, v in os.environ.items() if not k.startswith(('PM_', 'PROXMOX_VE_', 'TF_VAR_'))}
        return subprocess.run(['sh', '-c', '. scripts/bpg_env.sh; env -0'], cwd=ROOT, env={**base, **env},
                              text=True, capture_output=True)

    def test_bpg_env_maps_existing_secrets(self):
        for url in (FAKE_PM['PM_API_URL'], 'https://192.0.2.5:8006', 'https://192.0.2.5:8006/api2/json/'):
            with self.subTest(url=url):
                run = self.source(**dict(FAKE_PM, PM_API_URL=url), TF_VAR_ssh_pub_key=KEY1 + '\n\n' + KEY2 + '\n')
                self.assertEqual(run.returncode, 0, run.stderr)
                env = dict(item.split('=', 1) for item in run.stdout.split('\0') if item)
                self.assertEqual(env['PROXMOX_VE_ENDPOINT'], 'https://192.0.2.5:8006/')
                self.assertEqual(env['PROXMOX_VE_API_TOKEN'], 'ci@pve!t=s')
                self.assertEqual(json.loads(env['TF_VAR_ssh_pub_keys']), [KEY1, KEY2])
                self.assertEqual(env['PROXMOX_VE_INSECURE'], 'true')
                self.assertNotIn('s', run.stderr.split())  # never echoes, no set -x
        self.assertNotRegex((ROOT / 'scripts/bpg_env.sh').read_text(), r'(?m)^\s*set\s+-\w*x')

    def test_empty_key_fails(self):
        for key in ('', '\n  \n'):
            with self.subTest(key=key):
                self.assertNotEqual(self.source(**FAKE_PM, TF_VAR_ssh_pub_key=key).returncode, 0)
        self.assertNotEqual(self.source(**FAKE_PM).returncode, 0)


class PlanWorkflow(unittest.TestCase):
    def setUp(self):
        self.job = workflow('validate-and-plan.yml')['jobs']['validate-and-plan']
        self.plan = step(self.job, 'id', 'storage-plan')

    def run_plan(self, k3s_exit, storage_exit, tfvars=True, **manifest):
        box = Sandbox(self, plan_exit=storage_exit, tfvars=tfvars, **manifest)
        (box.dir / 'plan.exitcode').write_text('%d\n' % k3s_exit)
        (box.dir / 'plan.log').write_text('k3s plan\n')
        return box, box.run(self.plan['run'], workflow_env(self.job, self.plan))

    def test_k3s_plan_prefix_unchanged(self):
        upload = step(self.job, 'name', 'Upload Terraform plan and exitcode to S3')
        self.assertTrue(upload['with']['destination'].endswith(PR), upload['with']['destination'])
        self.assertEqual(upload['with']['source'].split(), ['plan.tfplan', 'plan.exitcode'])
        download = step(workflow('apply.yml')['jobs']['apply'], 'name', 'Download Terraform plan and exitcode from S3')
        self.assertTrue(download['with']['source'].strip().endswith(PR))
        self.assertEqual(download['with']['recursive'], 'true')

    def test_storage_plan_skips_without_tfvars(self):
        box, run = self.run_plan(2, 2, tfvars=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(box.outputs(), {'planned': 'false'})
        self.assertIn('storage root has no terraform.tfvars: not planned', box.read('plan.log').splitlines())
        self.assertEqual(base64.b64decode(box.read('plan.log.b64')).decode(), box.read('plan.log'))
        calls = box.read('calls').splitlines()
        self.assertIn('-chdir=storage validate', calls)
        self.assertFalse([c for c in calls if ' plan ' in ' %s ' % c])

    def test_storage_plan_uploads_to_its_own_prefix(self):
        self.assertLess(index(self.job, 'name', 'Create Terraform plan'), index(self.job, 'id', 'storage-plan'))
        box, run = self.run_plan(0, 2)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(box.outputs(), {'planned': 'true'})
        self.assertEqual(box.read('storage/plan.exitcode').strip(), '2')
        self.assertIn('-chdir=storage plan -no-color -input=false -detailed-exitcode -out=plan.tfplan',
                      box.read('calls').splitlines())
        log = box.read('plan.log')
        self.assertIn('## storage root', log)
        self.assertLess(log.index('k3s plan'), log.index('fake plan'))
        self.assertEqual(base64.b64decode(box.read('plan.log.b64')).decode(), log)
        uploads = [s for s in self.job['steps'] if s.get('uses') == './.github/actions/s3-cp']
        self.assertEqual(len(uploads), 2)
        storage = uploads[1]
        self.assertEqual(storage['if'], "steps.storage-plan.outputs.planned == 'true'")
        self.assertTrue(storage['with']['destination'].endswith(PR + 'storage/'), storage['with']['destination'])
        self.assertEqual(storage['with']['source'].split(), ['storage/plan.tfplan', 'storage/plan.exitcode'])
        self.assertLess(index(self.job, 'id', 'storage-plan'), self.job['steps'].index(storage))
        box, run = self.run_plan(0, 1)
        self.assertNotEqual(run.returncode, 0)

    def test_stale_storage_plan_removed_when_not_planned(self):
        # A plan uploaded by an earlier push must not survive a push that no longer plans storage: apply would use it.
        name = 'Remove stale storage root plan from S3'
        remove = step(self.job, 'name', name)
        self.assertEqual(remove['if'], "steps.storage-plan.outputs.planned == 'false'")
        upload = step(self.job, 'name', 'Upload Terraform plan and exitcode to S3')
        for key in ('aws_access_key_id', 'aws_secret_access_key', 'aws_endpoint_url'):
            self.assertIn(upload['with'][key], remove['env'].values(), key)
        prefix = remove['env']['STORAGE_PLAN_PREFIX']
        self.assertEqual(prefix, upload['with']['destination'] + 'storage/')
        self.assertTrue(prefix.endswith(PR + 'storage/'), prefix)  # never the k3s pr-<n>/plan.*
        steps = self.job['steps']
        at = steps.index(remove)
        self.assertLess(index(self.job, 'id', 'storage-plan'), at)
        self.assertLess(index(self.job, 'name', 'Install AWS CLI and bash for the local S3 composite action'), at)
        self.assertLess(at, len(steps) - 1)  # not after the job's last step
        box = Sandbox(self)
        box.fake('aws', 'echo "aws $*" >> "$CALLS"\n')
        run = box.run(remove['run'], workflow_env(self.job, remove))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(box.read('calls').splitlines(), ['aws s3 rm %s --recursive --quiet' % prefix])

    def test_one_root_per_pr(self):
        for k3s in (0, 2):
            for storage in (0, 2):
                with self.subTest(k3s=k3s, storage=storage):
                    _, run = self.run_plan(k3s, storage)
                    if k3s == storage == 2:
                        self.assertNotEqual(run.returncode, 0)
                        self.assertIn('changes both the k3s root and the storage root', run.stdout + run.stderr)
                    else:
                        self.assertEqual(run.returncode, 0, run.stderr)
        _, run = self.run_plan(2, 2, tfvars=False)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_storage_plan_reads_manifest_only_with_tfvars(self):
        box, run = self.run_plan(0, 2, tfvars=False, manifest=None)  # main has no storage entry before Task 3.3
        self.assertEqual((run.returncode, box.outputs()), (0, {'planned': 'false'}), run.stderr)
        box, run = self.run_plan(0, 2, manifest=[OTHER])
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse([c for c in box.read('calls').splitlines() if c.startswith('-chdir=storage plan')])
        self.assertNotIn('planned', box.outputs())
        box, run = self.run_plan(0, 2)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(box.read('calls.vars'), '902 packer-storage-13.7.0-1\n')

    def test_maintenance_tests_job_runs_this_file(self):
        runs = [s.get('run', '') for s in workflow('validate-and-plan.yml')['jobs']['maintenance-tests']['steps']]
        self.assertIn('python3 scripts/tests/test-storage-ci.py', runs)


class ApplyWorkflow(unittest.TestCase):
    def setUp(self):
        self.doc = workflow('apply.yml')
        self.job = self.doc['jobs']['apply']
        self.storage = step(self.job, 'id', 'apply-storage')
        self.download = step(self.job, 'name', 'Download Terraform plan and exitcode from S3')

    def test_storage_apply_never_dispatches(self):
        self.assertEqual(self.job['outputs'], {'has_changes': '${{ steps.apply.outputs.has_changes }}',
                                               'drained_nodes': '${{ steps.apply.outputs.drained_nodes }}'})
        dispatch = json.dumps(self.doc['jobs']['trigger-ansible'])
        self.assertNotIn('apply-storage', dispatch)
        # Not the bare word: the event type is infrastructure-changed.
        self.assertNotIn('outputs.changed', dispatch)
        self.assertIn('python3 scripts/maintenance_apply.py --mode normal --saved-plan --chdir storage', self.storage['run'])
        self.assertEqual(self.storage['if'], self.download['if'])
        self.assertLess(index(self.job, 'id', 'apply'), index(self.job, 'id', 'apply-storage'))

    def test_storage_apply_skips_only_when_unconfigured(self):
        box = Sandbox(self)
        run = box.run(self.storage['run'], offline_env())
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('storage root not configured', run.stdout)
        self.assertEqual(box.read('calls'), '')
        for tfvars in (False, True):
            with self.subTest(tfvars=tfvars):
                box = Sandbox(self, tfvars=tfvars)
                (box.dir / 'storage/plan.exitcode').write_text('2\n')
                run = box.run(self.storage['run'], offline_env())
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(box.read('calls').splitlines(),
                                 ['scripts/maintenance_apply.py --mode normal --saved-plan --chdir storage'])

    def test_missing_storage_plan_refused_before_any_mutation(self):
        require = step(self.job, 'name', 'Require the storage plan')
        first = next(s for s in self.job['steps'] if 'maintenance_apply.py' in s.get('run', ''))
        self.assertLess(self.job['steps'].index(self.download), self.job['steps'].index(require))
        self.assertLess(self.job['steps'].index(require), self.job['steps'].index(first))
        self.assertEqual(require['if'], self.download['if'])
        for tfvars in (False, True):
            for planned in (False, True):
                with self.subTest(tfvars=tfvars, planned=planned):
                    box = Sandbox(self, tfvars=tfvars)
                    if planned:
                        (box.dir / 'storage/plan.exitcode').write_text('0\n')
                    run = box.run(require['run'], {})
                    if tfvars and not planned:
                        self.assertEqual(run.returncode, 1)
                        self.assertIn('storage/terraform.tfvars exists but the PR has no storage plan; '
                                      're-run the PR plan', run.stdout + run.stderr)
                    else:
                        self.assertEqual(run.returncode, 0, run.stderr)


class DriftWorkflow(unittest.TestCase):
    def setUp(self):
        self.job = workflow('drift.yml')['jobs']['check']
        self.storage = step(self.job, 'id', 'storage-plan')

    def run_drift(self, storage_exit, tfvars=True, **manifest):
        box = Sandbox(self, plan_exit=storage_exit, tfvars=tfvars, **manifest)
        (box.dir / 'drift.log').write_text('k3s plan\n')
        return box, box.run(self.storage['run'], workflow_env(self.job, self.storage))

    def test_drift_covers_storage(self):
        combined = "steps.plan.outputs.drift == 'true' || steps.storage-plan.outputs.drift == 'true'"
        self.assertLess(index(self.job, 'id', 'plan'), index(self.job, 'id', 'storage-plan'))
        self.assertEqual(self.job['outputs']['drift_detected'], '${{ ' + combined + ' }}')
        summary = step(self.job, 'name', 'Publish job summary')
        self.assertIn('${{ ' + combined + ' }}', summary['run'])
        self.assertEqual(step(self.job, 'name', 'Upload drift log')['if'], combined)
        for job, step_ in ((self.job, self.storage),
                           (workflow('validate-and-plan.yml')['jobs']['validate-and-plan'], None)):
            step_ = step_ or step(job, 'id', 'storage-plan')
            self.assertEqual({**job.get('env', {}), **step_.get('env', {})}.get('TF_VAR_ssh_pub_key'),
                             '${{ vars.TF_VAR_SSH_PUB_KEY }}')
        box, run = self.run_drift(2, tfvars=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(box.outputs(), {'drift': 'false'})
        self.assertEqual(box.read('calls'), '')
        box, run = self.run_drift(2)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(box.outputs(), {'drift': 'true'})
        log = box.read('drift.log')
        self.assertIn('## storage root', log)
        self.assertLess(log.index('k3s plan'), log.index('fake plan'))
        self.assertEqual(base64.b64decode(box.read('drift.log.b64')).decode(), log)
        box, run = self.run_drift(0)
        self.assertEqual((run.returncode, box.outputs()), (0, {'drift': 'false'}))
        _, run = self.run_drift(1)
        self.assertNotEqual(run.returncode, 0)


    def test_drift_reads_manifest_only_with_tfvars(self):
        box, run = self.run_drift(2, tfvars=False, manifest=None)
        self.assertEqual((run.returncode, box.outputs()), (0, {'drift': 'false'}), run.stderr)
        box, run = self.run_drift(2, manifest=[OTHER])
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse([c for c in box.read('calls').splitlines() if c.startswith('-chdir=storage plan')])
        self.assertNotIn('drift', box.outputs())
        box, run = self.run_drift(2)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(box.read('calls.vars'), '902 packer-storage-13.7.0-1\n')


class StorageTemplateEnv(unittest.TestCase):
    def test_storage_template_env(self):
        newer = dict(TEMPLATE, artifact_id='903', custom_data={'vm_name': 'packer-storage-13.7.0-2'})
        good = [([OTHER, TEMPLATE], ('902', 'packer-storage-13.7.0-1')),
                ([TEMPLATE, OTHER, newer], ('903', 'packer-storage-13.7.0-2'))]  # two storage entries: the last
        bad = [[OTHER], [{k: v for k, v in TEMPLATE.items() if k != 'artifact_id'}],
               [dict(TEMPLATE, artifact_id='abc')], [dict(TEMPLATE, custom_data={})]] + [
               [dict(TEMPLATE, artifact_id=v)] for v in ('902.5', '-1', '0')]  # a VMID is a positive integer
        base = {k: v for k, v in os.environ.items() if not k.startswith('TF_VAR_')}
        for builds, want in good + [(b, None) for b in bad]:
            with self.subTest(builds=builds):
                box = Sandbox(self, manifest=builds)
                run = box.run('. scripts/storage_template_env.sh; env | sed -n /^TF_VAR_storage_template_/p; echo "left=${entry+set}"', base)
                lines = [l for l in run.stdout.splitlines() if l.startswith('TF_VAR_storage_template_')]
                if want is None:
                    self.assertNotEqual(run.returncode, 0)
                    self.assertEqual(lines, [])
                else:
                    self.assertEqual(run.returncode, 0, run.stderr)
                    self.assertEqual(sorted(lines), ['TF_VAR_storage_template_name=' + want[1],
                                                     'TF_VAR_storage_template_vmid=' + want[0]])
                    self.assertIn('\nleft=\n', '\n' + run.stdout + '\n')  # sourced: no helper left in the step's shell


class Shared(unittest.TestCase):
    def test_manifest_selected_by_name(self):
        text = lambda wf: (WORKFLOWS / wf).read_text()
        for wf in ('apply.yml', 'validate-and-plan.yml', 'drift.yml'):
            self.assertFalse('builds[0]' in text(wf), wf)
        expr = JQ.search(text('drift.yml')).group(1)
        self.assertEqual(JQ.search(text('validate-and-plan.yml')).group(1), expr)
        jq = lambda builds: subprocess.run(['jq', '-er', expr], input=json.dumps({'builds': builds}),
                                           text=True, capture_output=True)
        for builds in ([K3S, OLD, STORAGE], [STORAGE, OLD, K3S]):
            self.assertEqual(jq(builds).stdout.strip(), K3S_NAME)
        self.assertNotEqual(jq([OLD]).returncode, 0)  # a manifest without k3s fails loudly
        steps = [step(workflow('apply.yml')['jobs']['apply'], 'name', 'Prepare reviewed image and private kubeconfig')]
        steps += [step(workflow(wf)['jobs'][job], 'name', 'Read manifest file')
                  for wf, job in (('validate-and-plan.yml', 'validate-and-plan'), ('drift.yml', 'check'))]
        for step_ in steps:
            for builds in ([K3S, OLD, STORAGE], [STORAGE, OLD, K3S], [OLD]):
                with self.subTest(step=step_['name'], builds=[b['name'] for b in builds]):
                    box = Sandbox(self)
                    box.fake('python3', 'exec %s "$@"\n' % sys.executable)
                    (box.dir / 'packer-manifest.json').write_text(json.dumps({'builds': builds}))
                    (box.dir / 'env').touch()
                    run = box.run(step_['run'], dict(GITHUB_ENV=str(box.dir / 'env'), KUBECONFIG_RAW='kube',
                                                     KUBECONFIG=str(box.dir / 'kubeconfig')))
                    if builds == [OLD]:
                        self.assertNotEqual(run.returncode, 0)
                        self.assertNotIn('TF_VAR_base_vm_name', box.read('env'))
                    else:
                        self.assertEqual(run.returncode, 0, run.stderr)
                        self.assertEqual(box.read('env'), 'TF_VAR_base_vm_name=%s\n' % K3S_NAME)
        for wf, step_ in (('validate-and-plan.yml', 'Create Terraform plan'), ('drift.yml', 'Check for drift')):
            job = next(iter(j for j in workflow(wf)['jobs'].values() if step_ in [s.get('name') for s in j['steps']]))
            self.assertNotIn('TF_VAR_base_vm_name', step(job, 'name', step_).get('env', {}), wf)

    def test_no_base_image_var_reaches_storage(self):
        found = 0
        for name in ('validate-and-plan.yml', 'apply.yml', 'drift.yml'):
            for job in workflow(name)['jobs'].values():
                for step_ in job.get('steps', []):
                    if '-chdir=storage' in step_.get('run', '') or '--chdir storage' in step_.get('run', ''):
                        found += 1
                        self.assertNotIn('TF_VAR_base_vm_name', step_.get('env', {}), name)
        self.assertEqual(found, 3)
        # The converse: the k3s plan, drift and apply steps never see the storage template.
        for name, step_name in (('validate-and-plan.yml', 'Create Terraform plan'), ('drift.yml', 'Check for drift'),
                                ('apply.yml', 'Acquire ownership and execute infrastructure operation')):
            job = next(j for j in workflow(name)['jobs'].values() if step_name in [s.get('name') for s in j['steps']])
            self.assertNotIn('storage_template', json.dumps(step(job, 'name', step_name)), name)

    def test_storage_files_removed(self):
        for name, job in (('validate-and-plan.yml', 'validate-and-plan'), ('apply.yml', 'apply')):
            remove = step(workflow(name)['jobs'][job], 'name', 'Remove saved plan from the runner workspace')
            self.assertEqual(remove['if'], 'always()')
            for path in ('storage/plan.tfplan', 'storage/plan.exitcode'):
                self.assertIn(path, remove['run'].split(), name)


if __name__ == '__main__':
    unittest.main()

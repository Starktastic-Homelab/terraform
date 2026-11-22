"""Run the real orchestration against fake executables and the real durable lock."""
from contextlib import redirect_stdout
import importlib.util
import io
import itertools
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
from maintenance_apply import execute
from csi_wwn_guard import ACK_TEXT, scsi_updates

def vm(address,actions,slot=None):
    # Shape of `terraform show -json` for telmate/proxmox 3.0.2-rc10 (disks/scsi/scsiN/{disk,passthrough,...}).
    scsi={'scsi%d'%n:[] for n in range(31)}
    if slot:scsi['scsi1']=[{'cdrom':[],'cloudinit':[],'disk':[],'passthrough':[],**slot}]
    before={'name':address,'disks':[{'scsi':[scsi],'virtio':[{'virtio0':[{'disk':[{'size':'32G'}]}]}]}]}
    return {'address':address,'type':'proxmox_vm_qemu','change':{'actions':actions,'before':None if actions==['create'] else before}}

CSI_DISK={'disk':[{'storage':'k3s-block','size':'8G','serial':'','wwn':''}]}
WORKER='module.workers.proxmox_vm_qemu.vm["kube-worker-01"]'

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.lock=self.root/'lock';self.lock.mkdir()
        (self.lock/'runner-instance').write_text('runner')
        self.env=patch.dict(os.environ,{'HOMELAB_RUNNER_INSTANCE':'runner','MAINTENANCE_OWNER':'terraform/test/1'})
        self.env.start();self.addCleanup(self.env.stop)
        self.commands=[];self.calls=[];self.fail=None
        self.plan={'resource_changes':[]}
        self.output=self.root/'output'
        self.output_env=patch.dict(os.environ,{'GITHUB_OUTPUT':str(self.output)})
        self.output_env.start();self.addCleanup(self.output_env.stop)
        self.cwd=os.getcwd();os.chdir(self.root);self.addCleanup(os.chdir,self.cwd)

    def run_command(self,args,**kwargs):
        self.commands.append(args);self.calls.append((kwargs.get('cwd'),args))
        # Every actual mutation sees the real persisted ownership record.
        self.assertTrue((self.lock/'operation.json').exists())
        if self.fail and self.fail in ' '.join(args):raise subprocess.CalledProcessError(1,args)
        if args[:3]==['terraform','show','-json']:return subprocess.CompletedProcess(args,0,stdout=json.dumps(self.plan))
        stdout=json.dumps({'items':[{'metadata':{'name':'worker','labels':{}}},
                                   {'metadata':{'name':'master','labels':{'node-role.kubernetes.io/control-plane':'true'}}},
                                   {'metadata':{'name':'held-worker','labels':{}},'spec':{'unschedulable':True}}]})
        return subprocess.CompletedProcess(args,0,stdout=stdout)

    def test_every_mode_acquires_before_mutation_and_releases_on_success(self):
        for mode in ['normal','drain','destroy']:
            with self.subTest(mode=mode),patch('maintenance_apply.subprocess.run',side_effect=self.run_command):
                self.commands=[];execute(mode,False,self.lock)
                self.assertFalse((self.lock/'operation.json').exists())
                apply=['terraform','apply','-auto-approve' if mode=='destroy' else 'plan.tfplan']
                self.assertIn(apply,self.commands)
                if mode=='destroy':self.assertIn(['terraform','destroy','-auto-approve'],self.commands)
                else:
                    # A dispatch without a PR plan still applies only a plan the CSI guard has read.
                    self.assertLess(self.commands.index(['terraform','plan','-out=plan.tfplan']),self.commands.index(['terraform','show','-json','plan.tfplan']))
                    self.assertLess(self.commands.index(['terraform','show','-json','plan.tfplan']),self.commands.index(apply))
                if mode=='drain':
                    self.assertLess(self.commands.index(['terraform','show','-json','plan.tfplan']),self.commands.index(['kubectl','cordon','worker']))
                    self.assertLess(self.commands.index(['kubectl','cordon','worker']),self.commands.index(apply))
                    self.assertFalse(any(cmd[0]=='kubectl' for cmd in self.commands[self.commands.index(apply)+1:]))
                output=dict(line.split('=',1) for line in self.output.read_text().splitlines())
                self.assertEqual(json.loads(output['drained_nodes']), ['worker','master'] if mode=='drain' else [])

    def test_failed_mutations_keep_lock_and_skip_later_stages(self):
        for fail in ['cordon','drain','terraform init','terraform apply']:
            with self.subTest(fail=fail),patch('maintenance_apply.subprocess.run',side_effect=self.run_command):
                self.fail=fail;self.commands=[]
                with self.assertRaises(subprocess.CalledProcessError):execute('drain',False,self.lock)
                self.assertTrue((self.lock/'operation.json').exists())
                self.assertFalse(self.output.exists())
                count=len(self.commands)
                with self.assertRaises(FileExistsError):execute('normal',False,self.lock)
                self.assertEqual(len(self.commands),count)
                (self.lock/'operation.json').unlink()

    def test_missing_marker_blocks_all_commands(self):
        (self.lock/'runner-instance').unlink()
        with patch('maintenance_apply.subprocess.run',side_effect=self.run_command):
            with self.assertRaises(FileNotFoundError):execute('destroy',False,self.lock)
        self.assertEqual(self.commands,[])

    def test_invalid_or_missing_pr_plan_refused(self):
        for value in [None,'1','bogus']:
            if value is not None:Path('plan.exitcode').write_text(value)
            with self.subTest(value=value),patch('maintenance_apply.subprocess.run',side_effect=self.run_command):
                with self.assertRaises(ValueError):execute('normal',True,self.lock)
                self.assertEqual(self.commands,[])

    def test_saved_plan_used_and_no_changes_skips_mutation(self):
        Path('plan.tfplan').write_bytes(b'fixture');Path('plan.exitcode').write_text('2')
        with patch('maintenance_apply.subprocess.run',side_effect=self.run_command):
            self.assertTrue(execute('normal',True,self.lock))
        self.assertIn(['terraform','apply','plan.tfplan'],self.commands)
        self.commands=[];Path('plan.exitcode').write_text('0')
        with patch('maintenance_apply.subprocess.run',side_effect=self.run_command):
            self.assertFalse(execute('drain',True,self.lock))
        self.assertEqual(self.commands,[])
        output=dict(line.split('=',1) for line in self.output.read_text().splitlines())
        self.assertEqual(output['has_changes'],'false')
        self.assertEqual(json.loads(output['drained_nodes']),[])

    def test_csi_wwn_guard_refuses_before_any_mutation_and_releases_lock(self):
        Path('plan.tfplan').write_bytes(b'fixture');Path('plan.exitcode').write_text('2')
        self.plan={'resource_changes':[vm(WORKER,['update'],CSI_DISK)]}
        for mode,saved in [('normal',True),('drain',True),('normal',False),('drain',False)]:
            with self.subTest(mode=mode,saved=saved),patch('maintenance_apply.subprocess.run',side_effect=self.run_command):
                self.commands=[]
                with self.assertRaisesRegex(SystemExit,re.escape(WORKER)):execute(mode,saved,self.lock)
                self.assertFalse(any(cmd[0]=='kubectl' or cmd[:2]==['terraform','apply'] for cmd in self.commands))
                # Nothing changed, so the refusal must not strand the maintenance lock.
                self.assertFalse((self.lock/'operation.json').exists())
                self.assertFalse(self.output.exists())

    def test_ack_read_by_maintenance_apply(self):
        Path('plan.tfplan').write_bytes(b'fixture');Path('plan.exitcode').write_text('2')
        self.plan={'resource_changes':[vm(WORKER,['update'],CSI_DISK)]}
        ticked='- [x] '+ACK_TEXT
        for event,extra,allowed in [
                ('pull_request_target',{'PR_BODY':'Intro\n'+ticked+'\n'},True),
                ('pull_request_target',{'PR_BODY':'- [ ] '+ACK_TEXT},False),
                ('pull_request_target',{'PR_BODY':'Do not '+ticked+' yet'},False),
                ('pull_request_target',{},False),
                ('pull_request_target',{'PR_BODY':'Intro\r\n'+ticked+'\r\nEnd\r\n'},True),
                ('workflow_dispatch',{'ACCEPT_CSI_WWN_LOSS':'true'},True),
                ('workflow_dispatch',{'ACCEPT_CSI_WWN_LOSS':'TRUE'},False),
                ('workflow_dispatch',{'ACCEPT_CSI_WWN_LOSS':'false'},False),
                ('workflow_dispatch',{'PR_BODY':ticked},False),  # each event reads only its own ack
                ('pull_request_target',{'ACCEPT_CSI_WWN_LOSS':'true'},False)]:
            with self.subTest(event=event,extra=extra),patch.dict(os.environ,{'EVENT_NAME':event,**extra}),patch('maintenance_apply.subprocess.run',side_effect=self.run_command):
                for key in {'PR_BODY','ACCEPT_CSI_WWN_LOSS'}-extra.keys():os.environ.pop(key,None)
                self.commands=[];self.output.unlink(missing_ok=True)
                if allowed:
                    self.assertTrue(execute('normal',True,self.lock))
                    self.assertIn(['terraform','apply','plan.tfplan'],self.commands)
                else:
                    with self.assertRaisesRegex(SystemExit,re.escape(WORKER)):execute('normal',True,self.lock)
                    self.assertFalse(any(cmd[0]=='kubectl' or cmd[:2]==['terraform','apply'] for cmd in self.commands))
                    self.assertFalse((self.lock/'operation.json').exists())
                    self.assertFalse(self.output.exists())

    def test_capture_is_explicit(self):
        captured=[]
        def record(args,**kwargs):
            captured.append((tuple(args),kwargs['capture_output']))
            return self.run_command(args,**kwargs)
        with patch('maintenance_apply.subprocess.run',side_effect=record):execute('drain',False,self.lock)
        self.assertEqual({args for args,capture in captured if capture is True},
                         {('kubectl','get','nodes','-o','json'),('terraform','show','-json','plan.tfplan')})
        self.assertTrue(all(capture in (True,False) for _,capture in captured))

    def test_destroy_mode_skips_the_guard(self):
        self.plan={'resource_changes':[vm(WORKER,['update'],CSI_DISK)]}
        with patch('maintenance_apply.subprocess.run',side_effect=self.run_command):execute('destroy',False,self.lock)
        self.assertNotIn(['terraform','show','-json','plan.tfplan'],self.commands)

    def storage_plan(self,code='2'):
        Path('storage').mkdir(exist_ok=True)
        Path('storage/plan.tfplan').write_bytes(b'fixture');Path('storage/plan.exitcode').write_text(code)

    def test_chdir_runs_in_the_root_and_never_dispatches(self):
        self.storage_plan()
        with patch('maintenance_apply.subprocess.run',side_effect=self.run_command):
            self.assertTrue(execute('normal',True,self.lock,workdir='storage'))
        self.assertTrue(all(cwd=='storage' for cwd,a in self.calls if a[0]=='terraform'))
        self.assertIn(('storage',['terraform','apply','plan.tfplan']),self.calls)
        out=dict(l.split('=',1) for l in self.output.read_text().splitlines())
        self.assertEqual(out,{'changed':'true'})  # no has_changes, no drained_nodes
        self.assertFalse((self.lock/'operation.json').exists())

    def test_chdir_refuses_other_modes_before_lock(self):
        self.storage_plan()
        for mode,saved in [('drain',True),('destroy',True),('normal',False)]:
            with self.subTest(mode=mode,saved=saved),patch('maintenance_apply.subprocess.run',side_effect=self.run_command):
                with self.assertRaises(ValueError):execute(mode,saved,self.lock,workdir='storage')
                self.assertEqual(self.commands,[])
                self.assertFalse((self.lock/'operation.json').exists())

    def test_chdir_reads_its_own_plan_files(self):
        Path('plan.tfplan').write_bytes(b'fixture');Path('plan.exitcode').write_text('2')
        Path('storage').mkdir();Path('storage/plan.tfplan').write_bytes(b'fixture')
        with patch('maintenance_apply.subprocess.run',side_effect=self.run_command):
            with self.assertRaisesRegex(ValueError,'PR plan or its successful exit status is missing'):
                execute('normal',True,self.lock,workdir='storage')
        self.assertEqual(self.commands,[])

    def storage_vm(self,actions):
        return {'resource_changes':[{'address':'proxmox_virtual_environment_vm.storage','type':'proxmox_virtual_environment_vm',
                                     'change':{'actions':actions,'before':None,'after':{'disk':[
                                         {'interface':'virtio1','datastore_id':'k3s-block','size':64},
                                         {'interface':'virtio0','datastore_id':'local-zfs','size':4}]}}}],
                'variables':{'storage_template_vmid':{'value':902}}}

    def test_clone_check_runs_only_for_a_create(self):
        self.storage_plan()
        for actions,called in [(['delete','create'],True),(['create'],True),(['update'],False),(['no-op'],False)]:
            self.plan=self.storage_vm(actions)
            with self.subTest(actions=actions),patch('maintenance_apply.subprocess.run',side_effect=self.run_command),\
                    patch('maintenance_apply.check_clone_source.main',return_value=0) as main:
                execute('normal',True,self.lock,workdir='storage')
                if called:main.assert_called_once_with(902,('local-zfs',4))
                else:main.assert_not_called()

    def assert_clone_refusal(self,plan=None,pattern='clone source 902',**main):
        self.storage_plan();self.plan=plan or self.storage_vm(['create'])
        with patch('maintenance_apply.subprocess.run',side_effect=self.run_command),\
                patch('maintenance_apply.check_clone_source.main',**main):
            with self.assertRaisesRegex(SystemExit,pattern):execute('normal',True,self.lock,workdir='storage')
        self.assertFalse(any(cmd[:2]==['terraform','apply'] for cmd in self.commands))
        # Nothing has changed yet, so a failed check must not leave a held lock.
        self.assertFalse((self.lock/'operation.json').exists())
        self.assertFalse(self.output.exists())

    def test_missing_clone_source_refuses_before_apply(self):
        self.assert_clone_refusal(return_value=1)

    def test_unreadable_clone_inputs_refuse_and_release_lock(self):
        no_vmid=self.storage_vm(['create']);del no_vmid['variables']
        no_root=self.storage_vm(['create'])
        no_root['resource_changes'][0]['change']['after']['disk']=[{'interface':'virtio1','datastore_id':'k3s-block','size':64}]
        for name,plan in [('no storage_template_vmid',no_vmid),('no virtio0',no_root)]:
            with self.subTest(name):
                # The check itself would pass: a missing input alone must refuse (fail closed).
                self.commands=[];self.assert_clone_refusal(plan,'Refusing apply: clone source',return_value=0)

    def test_clone_check_error_releases_lock(self):
        for error in [ssl.SSLError('bad certificate'),urllib.error.URLError('refused')]:
            with self.subTest(error=error):
                self.commands=[];self.assert_clone_refusal(side_effect=error)

class CsiWwnGuardTests(unittest.TestCase):
    def test_only_in_place_updates_of_vms_holding_scsi_disks_are_flagged(self):
        plan={'resource_changes':[
            vm('update-csi-disk',['update'],CSI_DISK),
            vm('update-passthrough',['update'],{'passthrough':[{'file':'/dev/sdb'}]}),
            vm('update-no-scsi',['update']),
            vm('update-scsi-cdrom-only',['update'],{'cdrom':[{'iso':'x.iso'}]}),
            vm('replace-csi-disk',['delete','create'],CSI_DISK),
            vm('create',['create']),
            vm('noop-csi-disk',['no-op'],CSI_DISK),
            {'address':'other','type':'null_resource','change':{'actions':['update'],'before':{}}}]}
        self.assertEqual(scsi_updates(plan),['update-csi-disk','update-passthrough'])
        self.assertEqual(scsi_updates({}),[])

    def test_cli_reports_without_failing(self):
        script=ROOT/'scripts/csi_wwn_guard.py'
        for plan,expect in [({'resource_changes':[vm(WORKER,['update'],CSI_DISK)]},'WARNING: '),({'resource_changes':[]},'No in-place')]:
            result=subprocess.run([sys.executable,'-I',str(script)],input=json.dumps(plan),text=True,capture_output=True,check=True)
            self.assertTrue(result.stdout.startswith(expect),result.stdout)

    def test_update_without_disks_key_is_flagged(self):
        # No `disks` key means a provider schema this guard cannot read, so it fails closed.
        plan={'resource_changes':[{'address':'x','type':'proxmox_vm_qemu','change':{'actions':['update'],'before':{'name':'x'}}}]}
        self.assertEqual(scsi_updates(plan),['x'])

    def test_ack_text_lives_in_the_guard_and_template(self):
        import yaml
        text=(ROOT/'.github/workflows/apply.yml').read_text()
        self.assertFalse(ACK_TEXT in text,'apply.yml repeats ACK_TEXT')
        self.assertFalse('csi_wwn_ack' in text.lower(),'apply.yml names csi_wwn_ack')
        doc=yaml.safe_load(text)
        env=doc['jobs']['apply']['env']
        self.assertEqual(env['PR_BODY'],'${{ github.event.pull_request.body }}')
        self.assertEqual(env['ACCEPT_CSI_WWN_LOSS'],'${{ inputs.accept_csi_wwn_loss }}')
        self.assertIn('accept_csi_wwn_loss',doc[True]['workflow_dispatch']['inputs'])
        # The body is attacker-controlled text: only env mappings may carry it, never a run script.
        for job in doc['jobs'].values():
            for step in job.get('steps',[]):self.assertNotIn('pull_request.body',step.get('run',''))
        self.assertIn('- [ ] '+ACK_TEXT,(ROOT/'.github/PULL_REQUEST_TEMPLATE.md').read_text())
        # A skip pattern in the PR body skips apply entirely, so the ack text must not contain one.
        self.assertNotRegex(ACK_TEXT,r'(?i)\[(skip ci|ci skip|no ci|skip actions|actions skip)\]')

    def test_plan_step_runs_the_guard(self):
        import yaml
        steps=yaml.safe_load((ROOT/'.github/workflows/validate-and-plan.yml').read_text())['jobs']['validate-and-plan']['steps']
        self.assertNotIn('Check for VM updates that strip Proxmox CSI wwn',[x.get('name') for x in steps])
        script=next(x for x in steps if x.get('name')=='Create Terraform plan')['run']
        plan=json.dumps({'resource_changes':[vm(WORKER,['update'],CSI_DISK)]})
        for code in [1,2]:
            with self.subTest(code=code),tempfile.TemporaryDirectory() as tmp:
                tmp=Path(tmp);bin=tmp/'bin';bin.mkdir();(tmp/'scripts').symlink_to(ROOT/'scripts')
                (bin/'terraform').write_text('#!/bin/sh\necho "$*" >> calls\n'
                                             'case "$1" in plan) echo planned; exit %d;; show) cat plan.json;; esac\n'%code)
                (bin/'apk').write_text('#!/bin/sh\nexit 0\n')
                for f in bin.iterdir():f.chmod(0o755)
                (tmp/'plan.json').write_text(plan)
                env={**os.environ,'PATH':str(bin)+os.pathsep+os.environ['PATH']}
                result=subprocess.run(['sh','-e','-c',script],cwd=tmp,env=env,text=True,capture_output=True)
                calls=(tmp/'calls').read_text()
                log=(tmp/'plan.log').read_text()
                self.assertEqual(subprocess.run(['base64','-d',str(tmp/'plan.log.b64')],capture_output=True,text=True,check=True).stdout,log)
                if code==1:
                    self.assertNotEqual(result.returncode,0)
                    self.assertNotIn('show',calls)
                else:
                    self.assertEqual(result.returncode,0,result.stderr)
                    self.assertIn('show -json plan.tfplan',calls)
                    self.assertIn('WARNING: ',log);self.assertIn(WORKER,log)

def plan_vm(index,actions,vmid,**extra):
    change=vm('module.worker_nodes[%d].proxmox_vm_qemu.vm'%index,actions)
    change['change']['before'].update(id='pve/qemu/%d'%vmid,**extra)
    return change
PLAN={'resource_changes':[plan_vm(1,['delete','create'],202,cipassword='SECRET'),plan_vm(0,['update'],201),
                          {'address':'null_resource.x','type':'null_resource','change':{'actions':['delete'],'before':{'id':'x'}}}]}
STATE={'values':{'root_module':{'resources':[],'child_modules':[
    {'resources':[{'type':'proxmox_vm_qemu','values':{'id':'pve/qemu/200'}}]},
    {'resources':[{'type':'proxmox_vm_qemu','values':{'id':'pve/qemu/201'}}],'child_modules':[]}]}}}

class GracefulShutdownTests(WorkflowTests):
    """Clean guest shutdown before Terraform's hard stop, failing open to today's behaviour."""
    def setUp(self):
        super().setUp()
        self.api=[];self.api_fail=None;self.show=None;self.states={}
        Path('plan.tfplan').write_bytes(b'fixture');Path('plan.exitcode').write_text('2');self.plan=PLAN
        for p in [patch('maintenance_apply.pve_request',side_effect=self.pve),patch('maintenance_apply.time.sleep')]:
            p.start();self.addCleanup(p.stop)

    def run_command(self,args,**kwargs):
        if args[:2]==['terraform','show']:
            self.commands.append(args);self.assertTrue(kwargs.get('capture_output'))
            if self.fail=='terraform show':raise subprocess.CalledProcessError(1,args)
            if self.show is not None:return subprocess.CompletedProcess(args,0,stdout=self.show)
            return subprocess.CompletedProcess(args,0,stdout=json.dumps(STATE if len(args)==3 else self.plan))
        return super().run_command(args,**kwargs)

    def pve(self,method,path,data=None):
        self.api.append((method,path));self.commands.append(['pve',method,path])
        self.assertTrue((self.lock/'operation.json').exists())
        if self.api_fail and self.api_fail in path:raise OSError('HTTP 403')
        vm=path.split('/')[4]
        if path.endswith('/shutdown'):self.states[vm]='stopping'
        if path.endswith('/current'):
            state=self.states.get(vm,'running')
            if state=='stopping':self.states[vm]='stopped'
            return {'status':state if state!='stopping' else 'running'}

    def execute(self,mode,saved=True):
        with patch('maintenance_apply.subprocess.run',side_effect=self.run_command):
            return execute(mode,saved,self.lock)

    def index(self,cmd):return self.commands.index(cmd)

    def test_only_deleted_vms_shut_down_after_init_and_before_apply(self):
        out=io.StringIO()
        with redirect_stdout(out):self.execute('normal')
        shut=[p for m,p in self.api if m=='POST']
        self.assertEqual(shut,['/nodes/pve/qemu/202/status/shutdown'])
        post=self.index(['pve','POST','/nodes/pve/qemu/202/status/shutdown'])
        self.assertLess(self.index(['terraform','init']),post)
        self.assertLess(self.index(['terraform','show','-json','plan.tfplan']),post)
        self.assertLess(self.commands.index(['pve','GET','/nodes/pve/qemu/202/status/current'],post+1),self.index(['terraform','apply','plan.tfplan']))
        self.assertIn('202@pve',out.getvalue());self.assertNotIn('SECRET',out.getvalue());self.assertNotIn('::warning',out.getvalue())
        self.assertFalse((self.lock/'operation.json').exists())

    def test_drain_shuts_down_after_draining(self):
        self.execute('drain')
        self.assertLess(self.index(['kubectl','drain','master','--ignore-daemonsets','--delete-emptydir-data','--disable-eviction','--timeout=120s']),
                        self.index(['pve','POST','/nodes/pve/qemu/202/status/shutdown']))

    def test_destroy_targets_every_vm_in_state_before_destroy(self):
        self.execute('destroy',False)
        self.assertEqual(sorted(p for m,p in self.api if m=='POST'),
                         ['/nodes/pve/qemu/200/status/shutdown','/nodes/pve/qemu/201/status/shutdown'])
        self.assertLess(self.index(['pve','POST','/nodes/pve/qemu/201/status/shutdown']),self.index(['terraform','destroy','-auto-approve']))

    def test_dispatch_plans_then_shuts_down(self):
        self.execute('normal',False)
        self.assertLess(self.index(['terraform','plan','-out=plan.tfplan']),self.index(['pve','POST','/nodes/pve/qemu/202/status/shutdown']))
        self.assertLess(self.index(['pve','POST','/nodes/pve/qemu/202/status/shutdown']),self.index(['terraform','apply','plan.tfplan']))

    def test_already_stopped_vm_is_not_shut_down(self):
        self.states['202']='stopped';self.execute('normal')
        self.assertEqual([p for m,p in self.api if m=='POST'],[])

    def test_every_failure_fails_open_and_still_applies(self):
        cases={'api error':('normal',dict(api_fail='/shutdown')),
               'no id':('normal',dict(plan={'resource_changes':[{'type':'proxmox_vm_qemu','change':{'actions':['delete'],'before':{}}}]})),
               'bad id':('normal',dict(plan={'resource_changes':[{'type':'proxmox_vm_qemu','change':{'actions':['delete'],'before':{'id':'202'}}}]})),
               'state show fails':('destroy',dict(fail='terraform show')),
               'bad state json':('destroy',dict(show='not json'))}
        for name,(mode,setup) in cases.items():
            with self.subTest(name):
                self.commands=[];self.api=[];self.states={};self.fail=None;self.api_fail=None;self.show=None;self.plan=PLAN
                for k,v in setup.items():setattr(self,k,v)
                out=io.StringIO()
                with redirect_stdout(out):self.execute(mode,mode!='destroy')
                self.assertIn('::warning::Graceful shutdown skipped',out.getvalue())
                self.assertIn(['terraform','apply','-auto-approve' if mode=='destroy' else 'plan.tfplan'],self.commands)
                self.assertFalse((self.lock/'operation.json').exists())

    def test_timeout_fails_open(self):
        # The VM never stops and the clock jumps past the deadline.
        with patch('maintenance_apply.time.monotonic',side_effect=itertools.chain([0],itertools.repeat(1000))),\
             patch('maintenance_apply.pve_request',side_effect=lambda m,p,d=None:self.api.append((m,p)) or {'status':'running'}):
            out=io.StringIO()
            with redirect_stdout(out):self.execute('normal')
        self.assertIn('TimeoutError: still running: 202@pve',out.getvalue())
        self.assertIn(['terraform','apply','plan.tfplan'],self.commands)

    def test_failed_apply_restarts_targets_and_keeps_lock(self):
        self.fail='terraform apply'
        with self.assertRaises(subprocess.CalledProcessError):self.execute('normal')
        self.assertGreater(self.index(['pve','POST','/nodes/pve/qemu/202/status/start']),self.index(['terraform','apply','plan.tfplan']))
        self.assertTrue((self.lock/'operation.json').exists())

    def test_lost_lock_aborts_before_any_api_call(self):
        def steal(args,**kwargs):
            if args[:2]==['terraform','show']:(self.lock/'operation.json').write_text('{}')
            return self.run_command(args,**kwargs)
        with patch('maintenance_apply.subprocess.run',side_effect=steal):
            with self.assertRaises(ValueError):execute('normal',True,self.lock)
        self.assertEqual(self.api,[]);self.assertNotIn(['terraform','apply','plan.tfplan'],self.commands)


# The parent's tests use their own fixtures; this class only adds the shutdown cases.
for _name in dir(WorkflowTests):
    if _name.startswith('test_') and _name not in GracefulShutdownTests.__dict__:
        setattr(GracefulShutdownTests,_name,None)

class PveRequestTests(unittest.TestCase):
    def test_request_shape(self):
        import maintenance_apply
        seen={}
        class Response(io.BytesIO):
            def __enter__(self):return self
            def __exit__(self,*a):pass
        def urlopen(request,context=None,timeout=None):
            seen.update(url=request.full_url,method=request.get_method(),body=request.data,
                        auth=request.get_header('Authorization'),verify=context.verify_mode,timeout=timeout)
            return Response(b'{"data":{"status":"running"}}')
        env={'PM_API_URL':'https://pve.test:8006/api2/json/','PM_API_TOKEN_ID':'tf@pve!ci','PM_API_TOKEN_SECRET':'s3cret'}
        with patch.dict(os.environ,env),patch('maintenance_apply.urllib.request.urlopen',side_effect=urlopen):
            self.assertEqual(maintenance_apply.pve_request('POST',maintenance_apply.vm_status('pve','202')+'/shutdown',{'timeout':240}),
                             {'status':'running'})
        self.assertEqual(seen['url'],'https://pve.test:8006/api2/json/nodes/pve/qemu/202/status/shutdown')
        self.assertEqual((seen['method'],seen['body'],seen['auth']),('POST',b'timeout=240','PVEAPIToken=tf@pve!ci=s3cret'))
        self.assertEqual(seen['timeout'],30)
        with patch.dict(os.environ,env),patch('maintenance_apply.urllib.request.urlopen',side_effect=urlopen):
            maintenance_apply.pve_request('GET','/nodes/pve/qemu/202/status/current')
        self.assertEqual((seen['method'],seen['body']),('GET',None))

class WiringTests(unittest.TestCase):
    def test_only_one_mutating_job_and_download_failure_cannot_reach_it(self):
        import yaml
        doc=yaml.safe_load((ROOT/'.github/workflows/apply.yml').read_text())
        job=doc['jobs']['apply']
        self.assertIn('/var/lib/homelab-maintenance:/maintenance',job['container']['volumes'])
        self.assertEqual(set(job['needs']),{'check-conditions'})
        condition=job['if']
        # Without a status function the implicit success() applies: a failed or cancelled check never applies.
        for gate in ['always()','cancelled()','failure()']:self.assertNotIn(gate,condition)
        for should_run in ['true','false']:
            allowed=eval(condition.replace('needs.check-conditions.outputs.should_run',repr(should_run)),{'__builtins__':{}},{})
            self.assertEqual(allowed,should_run=='true')
        # The saved plan comes from S3 inside this job, before the only mutation, so a failed download stops it.
        steps=job['steps']
        download=next(x for x in steps if x.get('name')=='Download Terraform plan and exitcode from S3')
        change=next(x for x in steps if 'maintenance_apply.py' in x.get('run',''))
        self.assertLess(steps.index(download),steps.index(change))
        self.assertNotIn('if',change)
        self.assertFalse(any(x.get('continue-on-error') for x in steps))
        for event in ['workflow_dispatch','pull_request_target']:
            for mode in ['normal','drain','destroy']:
                expression=download['if'].replace('&&',' and ').replace('env.APPLY_MODE',repr(mode)).replace('github.event_name',repr(event))
                # Downloaded exactly when the mutation step passes --saved-plan.
                self.assertEqual(eval(expression,{'__builtins__':{}},{}),event=='pull_request_target' and mode!='destroy',(event,mode))
        # Artifacts of this public repo are downloadable by anyone, and a saved plan holds prior state.
        # An expression in run is pasted into the step script on the runner's disk, so secrets go through env.
        secret=re.compile(r'\$\{\{[^}]*\bsecrets\b',re.I)
        for workflow in (ROOT/'.github/workflows').glob('*.y*ml'):
            for other in yaml.safe_load(workflow.read_text())['jobs'].values():
                for step in other.get('steps',[]):
                    self.assertNotRegex(step.get('run',''),secret,workflow.name)
                    if str(step.get('uses','')).startswith('actions/upload-artifact'):
                        self.assertNotIn('plan.tfplan',str(step['with'].get('path','')),workflow.name)
        # Composite inputs can carry secrets (the plan bucket), so no expression may reach a composite script.
        for action in (ROOT/'.github/actions').glob('*/action.yml'):
            for step in yaml.safe_load(action.read_text())['runs']['steps']:
                self.assertNotIn('${{',step.get('run',''),action)
        mutation=[(name,step) for name,j in doc['jobs'].items() for step in j.get('steps',[])
                  if 'maintenance_apply.py' in step.get('run','')]
        self.assertEqual([name for name,_ in mutation],['apply','apply'])
        # The storage root applies second, only where the k3s step applies a downloaded saved plan.
        storage=mutation[1][1]
        self.assertEqual(storage['id'],'apply-storage')
        self.assertLess(steps.index(change),steps.index(storage))
        self.assertEqual(storage['if'],download['if'])
        for event in ['workflow_dispatch','pull_request_target']:
            for mode in ['normal','drain','destroy']:
                expression=storage['if'].replace('&&',' and ').replace('env.APPLY_MODE',repr(mode)).replace('github.event_name',repr(event))
                self.assertEqual(eval(expression,{'__builtins__':{}},{}),event=='pull_request_target' and mode!='destroy',(event,mode))
        self.assertNotIn('always()',doc['jobs']['trigger-ansible']['if'])
        self.assertEqual(job['outputs']['drained_nodes'], '${{ steps.apply.outputs.drained_nodes }}')
        dispatch=doc['jobs']['trigger-ansible']
        payload=dispatch['steps'][0]['with']['client-payload']
        self.assertEqual(json.loads(payload.replace('${{ needs.apply.outputs.drained_nodes }}', '["worker"]')),
                         {'drained_nodes':['worker']})
        for result in ['success','failure','cancelled','skipped']:
            for changed in ['true','false']:
                condition=dispatch['if'].replace('!cancelled()','True').replace('&&',' and ')
                condition=condition.replace('needs.apply.result',repr(result)).replace('needs.apply.outputs.has_changes',repr(changed))
                self.assertEqual(eval(condition,{'__builtins__':{}},{}),result=='success' and changed=='true')

    def test_lock_helper_comes_from_ansible_main(self):
        # Same helper as ansible's deploy/storage workflows: ansible main, never this run's or a feature branch's copy.
        import yaml
        helpers=[(w.name,s['with']) for w in (ROOT/'.github/workflows').glob('*.y*ml')
                 for j in yaml.safe_load(w.read_text())['jobs'].values() for s in j.get('steps',[])
                 if s.get('with',{}).get('path')=='maintenance-helper']
        self.assertEqual(sorted(n for n,_ in helpers),['apply.yml','validate-and-plan.yml'])
        for name,w in helpers:
            self.assertEqual((w['repository'],w['ref'],w['persist-credentials']),('Starktastic-Homelab/ansible','main',False),name)

if __name__=='__main__':unittest.main()

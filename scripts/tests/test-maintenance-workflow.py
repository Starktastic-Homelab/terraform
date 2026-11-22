"""Run the real orchestration against fake executables and the real durable lock."""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
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
        self.commands=[];self.fail=None
        self.plan={'resource_changes':[]}
        self.output=self.root/'output'
        self.output_env=patch.dict(os.environ,{'GITHUB_OUTPUT':str(self.output)})
        self.output_env.start();self.addCleanup(self.output_env.stop)
        self.cwd=os.getcwd();os.chdir(self.root);self.addCleanup(os.chdir,self.cwd)

    def run_command(self,args,**kwargs):
        self.commands.append(args)
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
                ('workflow_dispatch',{'ACCEPT_CSI_WWN_LOSS':'false'},False)]:
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
        helper=next(x for x in job['steps'] if x.get('with',{}).get('path')=='maintenance-helper')
        self.assertRegex(helper['with']['ref'],r'^[0-9a-f]{40}$')
        mutation=[(name,step) for name,j in doc['jobs'].items() for step in j.get('steps',[])
                  if 'maintenance_apply.py' in step.get('run','')]
        self.assertEqual(len(mutation),1)
        self.assertEqual(mutation[0][0],'apply')
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

if __name__=='__main__':unittest.main()

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

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.lock=self.root/'lock';self.lock.mkdir()
        (self.lock/'runner-instance').write_text('runner')
        self.env=patch.dict(os.environ,{'HOMELAB_RUNNER_INSTANCE':'runner','MAINTENANCE_OWNER':'terraform/test/1'})
        self.env.start();self.addCleanup(self.env.stop)
        self.commands=[];self.fail=None
        self.output=self.root/'output'
        self.output_env=patch.dict(os.environ,{'GITHUB_OUTPUT':str(self.output)})
        self.output_env.start();self.addCleanup(self.output_env.stop)
        self.cwd=os.getcwd();os.chdir(self.root);self.addCleanup(os.chdir,self.cwd)

    def run_command(self,args,**kwargs):
        self.commands.append(args)
        # Every actual mutation sees the real persisted ownership record.
        self.assertTrue((self.lock/'operation.json').exists())
        if self.fail and self.fail in ' '.join(args):raise subprocess.CalledProcessError(1,args)
        stdout=json.dumps({'items':[{'metadata':{'name':'worker','labels':{}}},
                                   {'metadata':{'name':'master','labels':{'node-role.kubernetes.io/control-plane':'true'}}},
                                   {'metadata':{'name':'held-worker','labels':{}},'spec':{'unschedulable':True}}]})
        return subprocess.CompletedProcess(args,0,stdout=stdout)

    def test_every_mode_acquires_before_mutation_and_releases_on_success(self):
        for mode in ['normal','drain','destroy']:
            with self.subTest(mode=mode),patch('maintenance_apply.subprocess.run',side_effect=self.run_command):
                self.commands=[];execute(mode,False,self.lock)
                self.assertFalse((self.lock/'operation.json').exists())
                self.assertIn(['terraform','apply','-auto-approve'],self.commands)
                if mode=='destroy':self.assertIn(['terraform','destroy','-auto-approve'],self.commands)
                if mode=='drain':
                    self.assertLess(self.commands.index(['kubectl','cordon','worker']),self.commands.index(['terraform','apply','-auto-approve']))
                    self.assertFalse(any(cmd[0]=='kubectl' for cmd in self.commands[self.commands.index(['terraform','apply','-auto-approve'])+1:]))
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

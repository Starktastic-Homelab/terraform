"""Run the real orchestration against fake executables and the real durable lock."""
import importlib.util
import json
import os
from pathlib import Path
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
        self.cwd=os.getcwd();os.chdir(self.root);self.addCleanup(os.chdir,self.cwd)

    def run_command(self,args,**kwargs):
        self.commands.append(args)
        # Every actual mutation sees the real persisted ownership record.
        self.assertTrue((self.lock/'operation.json').exists())
        if self.fail and self.fail in ' '.join(args):raise subprocess.CalledProcessError(1,args)
        stdout=json.dumps({'items':[{'metadata':{'name':'worker','labels':{}}},
                                   {'metadata':{'name':'master','labels':{'node-role.kubernetes.io/control-plane':'true'}}}]})
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
                    self.assertGreater(self.commands.index(['kubectl','uncordon','worker']),self.commands.index(['terraform','apply','-auto-approve']))

    def test_failed_mutations_keep_lock_and_skip_later_stages(self):
        for fail in ['cordon','drain','terraform init','terraform apply','uncordon','kubectl wait']:
            with self.subTest(fail=fail),patch('maintenance_apply.subprocess.run',side_effect=self.run_command):
                self.fail=fail;self.commands=[]
                with self.assertRaises(subprocess.CalledProcessError):execute('drain',False,self.lock)
                self.assertTrue((self.lock/'operation.json').exists())
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

class WiringTests(unittest.TestCase):
    def test_only_one_mutating_job_and_download_failure_cannot_reach_it(self):
        import yaml
        doc=yaml.safe_load((ROOT/'.github/workflows/apply.yml').read_text())
        job=doc['jobs']['apply']
        self.assertIn('/var/lib/homelab-maintenance:/maintenance',job['container']['volumes'])
        self.assertEqual(set(job['needs']),{'check-conditions','download-plan'})
        condition=job['if']
        # Execute the actual boolean condition with GitHub result values.
        for event in ['workflow_dispatch','pull_request_target']:
            for mode in ['normal','drain','destroy']:
                for result in ['success','failure','cancelled','skipped']:
                    expression=condition.replace('!cancelled()', 'True').replace('&&',' and ').replace('||',' or ')
                    for key,value in {'needs.check-conditions.outputs.should_run':'true',
                                      'needs.check-conditions.outputs.apply_mode':mode,
                                      'needs.download-plan.result':result,'github.event_name':event}.items():
                        expression=expression.replace(key,repr(value))
                    allowed=eval(expression,{'__builtins__':{}},{})
                    expected=result=='success' or (result=='skipped' and (mode=='destroy' or event=='workflow_dispatch'))
                    self.assertEqual(allowed,expected,(event,mode,result))
        helper=next(x for x in job['steps'] if x.get('with',{}).get('path')=='maintenance-helper')
        self.assertRegex(helper['with']['ref'],r'^[0-9a-f]{40}$')
        mutation=[(name,step) for name,j in doc['jobs'].items() for step in j.get('steps',[])
                  if 'maintenance_apply.py' in step.get('run','')]
        self.assertEqual(len(mutation),1)
        self.assertEqual(mutation[0][0],'apply')
        self.assertNotIn('always()',doc['jobs']['trigger-ansible']['if'])

if __name__=='__main__':unittest.main()

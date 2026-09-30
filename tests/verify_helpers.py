"""Offline helper checks. Never contacts Kubernetes or prints generated credentials."""
import contextlib
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import unittest
from unittest.mock import patch
import yaml

ROOT = Path(__file__).resolve().parents[1]

class Helpers(unittest.TestCase):
    def test_kakaocloud_local_and_ingress(self):
        cmd = [sys.executable, str(ROOT/'scripts/render-ingress-values.py'),
               '-f', str(ROOT/'charts/fedops/tests/static-values.yaml'),
               '-f', str(ROOT/'profiles/common.yaml'),
               '-f', str(ROOT/'profiles/kakaocloud-local.yaml')]
        p = subprocess.run(cmd, text=True, capture_output=True, check=True)
        d = yaml.safe_load(p.stdout)
        ports = d['service']['ports']
        self.assertEqual(len(ports), 1001)
        self.assertEqual({x['nodePort'] for x in ports}, set(range(30080,31081)))
        self.assertEqual(d['service']['externalTrafficPolicy'], 'Cluster')
        invalid = subprocess.run(cmd+['--web-port','30081'], text=True, capture_output=True)
        self.assertNotEqual(invalid.returncode, 0)

    def bootstrap(self, mode, existing=False):
        created=[]
        def fake(cmd, **kw):
            if 'get' in cmd:
                items=[{'metadata':{'name':'fedops-web'}}] if existing else []
                return subprocess.CompletedProcess(cmd,0,json.dumps({'items':items}), '')
            created.append(json.loads(kw['input']))
            return subprocess.CompletedProcess(cmd,0,'','')
        argv=['bootstrap.py', mode, '--kubeconfig','/unused', '--context','offline', '--namespace','test']
        with patch.object(sys,'argv',argv), patch('subprocess.run',side_effect=fake), contextlib.redirect_stdout(io.StringIO()):
            runpy.run_path(str(ROOT/'scripts/bootstrap.py'),run_name='__main__')
        return created

    def test_bootstrap_secrets(self):
        self.assertEqual(len(self.bootstrap('secrets')),8)
        with self.assertRaises(SystemExit): self.bootstrap('secrets',True)

    def test_bootstrap_jobs(self):
        jobs=[d for d in self.bootstrap('jobs') if d['kind']=='Job']
        self.assertEqual(len(jobs),3)
        for job in jobs:
            self.assertNotIn('nodeSelector',job['spec']['template']['spec'])

if __name__ == '__main__': unittest.main(verbosity=2)

"""Offline rendering and local Task storage contract tests. No cluster writes."""
import ast
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
import yaml

ROOT=Path(__file__).resolve().parents[1]
CHART=ROOT/'charts/fedops'

def render(profile=None, values=(), success=True):
    cmd=[shutil.which('helm') or '/opt/homebrew/bin/helm','template','fedops',str(CHART),
         '-n','test','--kube-version','1.35.4','-f',str(CHART/'tests/static-values.yaml'),
         '-f',str(ROOT/'profiles/common.yaml')]
    if profile: cmd+=['-f',str(ROOT/'profiles'/f'{profile}.yaml')]
    for value in values: cmd+=['--set',value]
    r=subprocess.run(cmd,text=True,capture_output=True)
    if not success:
        assert r.returncode, 'Invalid values accepted'
        return r.stderr
    assert r.returncode==0,r.stderr
    return [d for d in yaml.safe_load_all(r.stdout) if d]

class Profiles(unittest.TestCase):
    def test_all_profiles(self):
        for name in ['onpremise-single-node','onpremise-multi-node','kakaocloud-local','kakaocloud-multi-node']:
            with self.subTest(profile=name):
                docs=render(name)
                pvs=[d for d in docs if d['kind']=='PersistentVolume']
                self.assertEqual(len(pvs),3)
                for pv in pvs:
                    self.assertIn('local',pv['spec'])
                    self.assertEqual(pv['spec']['storageClassName'],'')
                    self.assertEqual(pv['spec']['nodeAffinity']['required']['nodeSelectorTerms'][0]['matchExpressions'][0]['values'],['f-static-check'])
                for d in [d for d in docs if d['kind']=='Deployment']:
                    spec=d['spec']['template']['spec']
                    role=d['spec']['template']['metadata']['labels']['app.kubernetes.io/component']
                    if 'multi-node' not in name or role in ['mongo','registryMongo','minio']:
                        self.assertEqual(spec['nodeSelector'],{'kubernetes.io/hostname':'f-static-check'})
                    else:
                        self.assertNotIn('nodeSelector',spec)
                        self.assertEqual(spec['topologySpreadConstraints'][0]['whenUnsatisfiable'],'ScheduleAnyway')
                cm=next(d['data'] for d in docs if d['kind']=='ConfigMap' and d['metadata']['name']=='fedops-manager')
                self.assertEqual(cm['FEDOPS_TASK_STORAGE_MODE'],'local')
                self.assertEqual(json.loads(cm['FEDOPS_TASK_NODE_SELECTOR']),{'kubernetes.io/hostname':'f-static-check'})
                self.assertEqual((cm['FL_SERVER_PORT_MIN'],cm['FL_SERVER_PORT_MAX']),('30081','31080'))

    def test_set_equivalent(self):
        self.assertEqual(render('kakaocloud-multi-node'),render(values=['deployment.provider=kakaocloud','deployment.topology=multi-node']))

    def test_app_selectors_do_not_move_storage(self):
        docs=render('kakaocloud-multi-node',['deployment.nodeSelector.pool=app-workers','workloads.frontend.nodeSelector.pool=frontend'])
        for d in [d for d in docs if d['kind']=='Deployment']:
            spec=d['spec']['template']['spec']
            role=d['spec']['template']['metadata']['labels']['app.kubernetes.io/component']
            expected={'kubernetes.io/hostname':'f-static-check'} if role in ['mongo','registryMongo','minio'] else {'pool':'frontend' if role=='frontend' else 'app-workers'}
            self.assertEqual(spec['nodeSelector'],expected)

    def test_existing_claim(self):
        docs=render('kakaocloud-multi-node',['persistence.mongo.existingClaim=existing-mongo'])
        self.assertEqual(len([d for d in docs if d['kind']=='PersistentVolumeClaim']),2)

    def test_reject_invalid(self):
        for args in [['nodeName='],['deployment.storageClass=unexpected'],['deployment.storageMode=storage-class'],['task.portMax=31081'],['deployment.topology=unknown']]:
            with self.subTest(args=args): render('kakaocloud-multi-node',args,False)

    def test_local_task_contract(self):
        api=Mock()
        class ApiException(Exception): pass
        class Client:
            exceptions=NS(ApiException=ApiException)
            def CoreV1Api(self): return api
            def __getattr__(self,name): return lambda **kw:NS(**kw)
        tree=ast.parse((CHART/'files/manager-network/scalable_server_operator.py').read_text())
        selected=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ['_hostpath_node_affinity','_task_pod_scheduling','create_persistent_volume_and_claim']]
        env={'client':Client(),'os':os,'json':json,'logging':logging,'TARGET_NAMESPACE':'test',
             'hostpath_storage_node':lambda:'data-node','task_storage_path':lambda task:'/data/tasks/'+task,'task_storage_capacity':lambda:'2Gi'}
        exec(compile(ast.Module(body=selected,type_ignores=[]),'manager','exec'),env)
        with patch.dict(os.environ,{'FEDOPS_DEPLOYMENT_TOPOLOGY':'multi-node','FEDOPS_TASK_NODE_SELECTOR':'{"kubernetes.io/hostname":"data-node"}'}):
            self.assertEqual(env['create_persistent_volume_and_claim']('test-task'),'fl-data-test-task')
            self.assertEqual(env['_task_pod_scheduling']()['node_selector'],{'kubernetes.io/hostname':'data-node'})
        pv=api.create_persistent_volume.call_args.args[0]
        self.assertEqual(pv.spec.host_path.path,'/data/tasks/test-task')
        self.assertEqual(pv.spec.node_affinity.required.node_selector_terms[0].match_expressions[0].values,['data-node'])

if __name__=='__main__': unittest.main(verbosity=2)

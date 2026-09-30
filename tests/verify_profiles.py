"""Offline profile and Manager storage contract tests; no cluster access."""
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

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts/fedops"
HELM = shutil.which("helm") or "/opt/homebrew/bin/helm"

def render(profile=None, overrides=(), success=True):
    cmd = [HELM, "template", "fedops", str(CHART), "-n", "profile-test",
           "--kube-version", "1.35.4", "-f", str(CHART/"tests/static-values.yaml"),
           "-f", str(ROOT/"profiles/common.yaml")]
    if profile:
        cmd += ["-f", str(ROOT/"profiles"/(profile+".yaml"))]
    for item in overrides:
        cmd += ["--set", item]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if success:
        assert p.returncode == 0, p.stderr
        return [d for d in yaml.safe_load_all(p.stdout) if d]
    assert p.returncode != 0, "invalid values unexpectedly accepted"
    return p.stderr

class ProfileTests(unittest.TestCase):
    def test_three_profiles(self):
        for name in ("onpremise-single-node", "onpremise-multi-node", "kakaocloud-multi-node"):
            multi = "multi" in name
            docs = render(name, ["deployment.storageClass=test-csi"] if multi else [])
            deploys = [d for d in docs if d["kind"]=="Deployment"]
            self.assertEqual(len(deploys),9)
            self.assertEqual(len([d for d in docs if d["kind"]=="PersistentVolume"]),0 if multi else 3)
            claims=[d for d in docs if d["kind"]=="PersistentVolumeClaim"]
            self.assertEqual(len(claims),3)
            for d in deploys:
                spec=d["spec"]["template"]["spec"]
                self.assertEqual(d["spec"]["replicas"],1)
                if multi:
                    self.assertNotIn("nodeSelector",spec)
                    self.assertEqual(spec["topologySpreadConstraints"][0]["whenUnsatisfiable"],"ScheduleAnyway")
                else:
                    self.assertEqual(spec["nodeSelector"],{"kubernetes.io/hostname":"f-static-check"})
            for claim in claims:
                if multi:
                    self.assertEqual(claim["spec"]["storageClassName"],"test-csi")
                    self.assertNotIn("volumeName",claim["spec"])
            manager=next(d["data"] for d in docs if d["kind"]=="ConfigMap" and d["metadata"]["name"]=="fedops-manager")
            self.assertEqual(manager["FEDOPS_TASK_STORAGE_MODE"],"storage-class" if multi else "local")
            self.assertEqual(manager["FL_SERVER_PORT_MIN"],"30081")
            self.assertEqual(manager["FL_SERVER_PORT_MAX"],"31080")
            self.assertEqual(json.loads(manager["FEDOPS_TASK_NODE_SELECTOR"]),{} if multi else {"kubernetes.io/hostname":"f-static-check"})

    def test_set_equivalent(self):
        a=render("kakaocloud-multi-node",["deployment.storageClass=test-csi"])
        b=render(overrides=["nodeName=","deployment.provider=kakaocloud","deployment.topology=multi-node","deployment.storageClass=test-csi"])
        self.assertEqual(a,b)

    def test_selectors_and_claim_overrides(self):
        docs=render("onpremise-multi-node",["deployment.storageClass=test-csi",
                    "deployment.nodeSelector.pool=workers","workloads.mongo.nodeSelector.pool=database",
                    "task.nodeSelector.pool=training","persistence.mongo.existingClaim=existing-mongo",
                    "persistence.minio.storageClass=object-csi"])
        mongo=next(d for d in docs if d["kind"]=="Deployment" and d["metadata"]["name"]=="fedops-mongo-deploy")
        self.assertEqual(mongo["spec"]["template"]["spec"]["nodeSelector"],{"pool":"database"})
        self.assertEqual(len([d for d in docs if d["kind"]=="PersistentVolumeClaim"]),2)
        manager=next(d["data"] for d in docs if d["kind"]=="ConfigMap" and d["metadata"]["name"]=="fedops-manager")
        self.assertEqual(json.loads(manager["FEDOPS_TASK_NODE_SELECTOR"]),{"pool":"training"})
        claim=next(d for d in docs if d["kind"]=="PersistentVolumeClaim" and d["metadata"]["name"].endswith("minio-data"))
        self.assertEqual(claim["spec"]["storageClassName"],"object-csi")

    def test_reject_invalid(self):
        cases=[
            ("onpremise-single-node",["nodeName="],"single-node requires"),
            ("onpremise-multi-node",[],"storageClass"),
            ("onpremise-multi-node",["deployment.storageMode=local"],"requires storage-class"),
            ("onpremise-multi-node",["nodeName=old-node"],"empty nodeName"),
            ("onpremise-single-node",["deployment.provider=unknown"],"provider"),
            ("onpremise-single-node",["deployment.topology=multinode"],"topology"),
            ("onpremise-single-node",["task.portMax=31081"],"1000"),
            ("onpremise-single-node",["task.serviceType=ExternalName"],"serviceType"),
        ]
        for profile,values,error in cases:
            with self.subTest(values=values):
                self.assertIn(error,render(profile,values,False))

class ApiException(Exception):
    def __init__(self,status):
        self.status=status

class FakeClient:
    exceptions=NS(ApiException=ApiException)
    def __init__(self,api):
        self.api=api
    def CoreV1Api(self):
        return self.api
    def __getattr__(self,name):
        if name.startswith("V1"):
            return lambda **kwargs: NS(**kwargs)
        raise AttributeError(name)

class TaskStorageTests(unittest.TestCase):
    def setUp(self):
        self.api=Mock()
        source=CHART/"files/manager-network/scalable_server_operator.py"
        tree=ast.parse(source.read_text())
        selected=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in
                  ("_hostpath_node_affinity","_task_pod_scheduling","create_persistent_volume_and_claim")]
        self.g={"client":FakeClient(self.api),"os":os,"json":json,"logging":logging,"ApiException":ApiException,
                "TARGET_NAMESPACE":"test","hostpath_storage_node":lambda:"node-a",
                "task_storage_path":lambda task:"/data/tasks/"+task,"task_storage_capacity":lambda:"2Gi"}
        exec(compile(ast.Module(body=selected,type_ignores=[]),str(source),"exec"),self.g)

    def test_dynamic_does_not_create_pv_or_wait_for_binding(self):
        with patch.dict(os.environ,{"FEDOPS_TASK_STORAGE_MODE":"storage-class","FEDOPS_TASK_STORAGE_CLASS":"test-csi"}):
            self.assertEqual(self.g["create_persistent_volume_and_claim"]("task-a"),"fl-data-task-a")
        self.api.create_persistent_volume.assert_not_called()
        self.api.read_namespaced_persistent_volume_claim.assert_not_called()
        pvc=self.api.create_namespaced_persistent_volume_claim.call_args.args[1]
        self.assertEqual(pvc.spec.storage_class_name,"test-csi")
        self.assertEqual(pvc.spec.access_modes,["ReadWriteOnce"])

    def test_existing_dynamic_claim_must_match(self):
        self.api.create_namespaced_persistent_volume_claim.side_effect=ApiException(409)
        self.api.read_namespaced_persistent_volume_claim.return_value=NS(metadata=NS(labels={"task_id":"task-a"}),spec=NS(storage_class_name="test-csi"))
        with patch.dict(os.environ,{"FEDOPS_TASK_STORAGE_MODE":"storage-class","FEDOPS_TASK_STORAGE_CLASS":"test-csi"}):
            self.g["create_persistent_volume_and_claim"]("task-a")
            self.api.read_namespaced_persistent_volume_claim.return_value.spec.storage_class_name="different"
            with self.assertRaises(ValueError):
                self.g["create_persistent_volume_and_claim"]("task-a")

    def test_dynamic_missing_class_and_api_failure(self):
        with patch.dict(os.environ,{"FEDOPS_TASK_STORAGE_MODE":"storage-class","FEDOPS_TASK_STORAGE_CLASS":""}):
            with self.assertRaises(ValueError): self.g["create_persistent_volume_and_claim"]("task-a")
        self.api.create_namespaced_persistent_volume_claim.side_effect=ApiException(403)
        with patch.dict(os.environ,{"FEDOPS_TASK_STORAGE_MODE":"storage-class","FEDOPS_TASK_STORAGE_CLASS":"test-csi"}):
            with self.assertRaises(ApiException): self.g["create_persistent_volume_and_claim"]("task-a")

    def test_local_compatibility(self):
        with patch.dict(os.environ,{"FEDOPS_TASK_STORAGE_MODE":"local"}):
            self.g["create_persistent_volume_and_claim"]("task-a")
        pv=self.api.create_persistent_volume.call_args.args[0]
        self.assertEqual(pv.spec.host_path.path,"/data/tasks/task-a")
        self.assertEqual(pv.spec.node_affinity.required.node_selector_terms[0].match_expressions[0].values,["node-a"])

    def test_task_scheduling(self):
        with patch.dict(os.environ,{"FEDOPS_DEPLOYMENT_TOPOLOGY":"multi-node","FEDOPS_TASK_NODE_SELECTOR":'{"pool":"training"}'}):
            spec=self.g["_task_pod_scheduling"]()
            self.assertEqual(spec["node_selector"],{"pool":"training"})
            self.assertEqual(spec["topology_spread_constraints"][0].when_unsatisfiable,"ScheduleAnyway")
        with patch.dict(os.environ,{"FEDOPS_TASK_NODE_SELECTOR":"[]"}):
            with self.assertRaises(ValueError): self.g["_task_pod_scheduling"]()

if __name__=="__main__":
    unittest.main(verbosity=2)

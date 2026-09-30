"""Generate separate Istio gateway values from the rendered FedOps Gateway. No apply."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import yaml

p=argparse.ArgumentParser()
p.add_argument("-f","--values",action="append",default=[])
p.add_argument("--set",action="append",default=[])
p.add_argument("--namespace",default="fedops")
p.add_argument("--release",default="fedops")
p.add_argument("--service-type",choices=["NodePort","LoadBalancer"],default="NodePort")
p.add_argument("--web-port",type=int,default=30080,help="Must match access.publicPort (80/443 when omitted)")
p.add_argument("--kube-version",default="1.35.4")
a=p.parse_args()
root=Path(__file__).resolve().parents[1]
cmd=[shutil.which("helm") or "/opt/homebrew/bin/helm","template",a.release,str(root/"charts/fedops"),
     "-n",a.namespace,"--kube-version",a.kube_version]
for v in a.values: cmd+=["-f",v]
for v in a.set: cmd+=["--set",v]
r=subprocess.run(cmd,capture_output=True,text=True)
if r.returncode: sys.exit(r.stderr)
docs=[d for d in yaml.safe_load_all(r.stdout) if d]
gateway=next(d for d in docs if d["kind"]=="Gateway")
listeners=gateway["spec"]["servers"]
web=next(s["port"] for s in listeners if s["port"]["protocol"] in ("HTTP","HTTPS"))
ports=[{"name":"https" if web["protocol"]=="HTTPS" else "http2","port":a.web_port,"targetPort":web["number"]}]
for s in listeners:
    if s["port"]["protocol"]=="TCP":
        n=s["port"]["number"]
        ports.append({"name":f"tcp-fl-{n}","port":n,"targetPort":n})
if len({x["port"] for x in ports})!=len(ports):
    sys.exit("Web and Task ports overlap")
if not all(1 <= x["port"] <= 65535 for x in ports): sys.exit("Invalid service port")
if a.service_type=="NodePort":
    for port in ports:
        if not 30000 <= port["port"] <= 32767: sys.exit("NodePort outside standard 30000-32767 range")
        port["nodePort"]=port["port"]
out={"replicaCount":1,"autoscaling":{"enabled":False},
     "labels":gateway["spec"]["selector"],
     "service":{"type":a.service_type,"externalTrafficPolicy":"Cluster","ports":ports}}
print("# Generated from the FedOps Gateway. Apply only to the separate Istio gateway chart.")
print("# Cluster traffic policy allows ingress Pods on another node; client source IP may be SNATed.")
print(yaml.safe_dump(out,sort_keys=False),end="")

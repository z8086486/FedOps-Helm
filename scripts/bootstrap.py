"""Fresh-install bootstrap using default chart Secret/bucket names. Secrets go directly to Kubernetes, never files/logs.

Run with `secrets`, then install Helm, then run with `jobs` after DBs are Ready.
Re-running secrets refuses partial/existing credentials; it never rotates them.
"""
import json
import secrets
import subprocess
import sys
import os

import argparse
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('mode', choices=['secrets','jobs'])
parser.add_argument('--kubeconfig', required=True)
parser.add_argument('--context', required=True)
parser.add_argument('--namespace', required=True)
parser.add_argument('--node', default='')
args = parser.parse_args()
NS = args.namespace
NODE = args.node
K = ['kubectl', '--kubeconfig='+args.kubeconfig, '--context='+args.context, '-n', NS]
MONGO = 'docker.io/library/mongo@sha256:03cda579c8caad6573cb98c2b3d5ff5ead452a6450561129b89595b4b9c18de2'
MC = 'cgr.dev/chainguard/minio-client@sha256:b2bd7824d23d3e3b15bedd7e87fbc3be29d2e213307b4f901e4a1d92356dc20f'

def create(obj):
    p = subprocess.run(K + ['create', '-f', '-'], input=json.dumps(obj), text=True, capture_output=True)
    if p.returncode:
        raise RuntimeError('Creation failed for '+obj['kind']+'/'+obj['metadata']['name']+'; inspect resource state (secret output withheld).')
    print(obj['kind']+'/'+obj['metadata']['name']+' created')

def resource(kind, name, **kwargs):
    return {'apiVersion':'v1', 'kind':kind, 'metadata':{'name':name,'namespace':NS}, **kwargs}

def env(name, secret, key=None):
    return {'name':name,'valueFrom':{'secretKeyRef':{'name':secret,'key':key or name}}}

def job(name, containers, volumes=None):
    return {'apiVersion':'batch/v1','kind':'Job','metadata':{'name':name,'namespace':NS},
            'spec':{'backoffLimit':2,'activeDeadlineSeconds':600,'template':{'metadata':{
                'annotations':{'sidecar.istio.io/inject':'false'}},'spec':{
                    'restartPolicy':'Never','automountServiceAccountToken':False,
                    **({'nodeSelector':{'kubernetes.io/hostname':NODE}} if NODE else {}),
                    'initContainers':containers[:-1], 'containers':containers[-1:],
                    'volumes':volumes or []}}}}

if args.mode == 'secrets':
    names = ['fedops-web','fedops-performance','fedops-registry','fedops-mongo',
             'fedops-registry-mongo','fedops-minio','fedops-task-storage','fedops-bootstrap']
    existing = subprocess.run(K+['get','secrets','-o','json'],capture_output=True,text=True,check=True)
    if set(names) & {x['metadata']['name'] for x in json.loads(existing.stdout)['items']}:
        raise SystemExit('Existing lab secrets found; refusing regeneration.')
    pvcs = subprocess.run(K+['get','pvc','-o','json'],capture_output=True,text=True,check=True)
    if json.loads(pvcs.stdout)['items']:
        raise SystemExit('Existing PVCs found; refusing fresh-install credential generation.')
    pw = lambda: secrets.token_hex(24)
    mongo_root, registry_root, web_pw, registry_pw, minio_pw, task_pw, object_pw = [pw() for _ in range(7)]
    web_uri = f'mongodb://fedops_app:{web_pw}@fedops-mongo-np:5000/fedops?authSource=fedops'
    reg_uri = f'mongodb://registry_app:{registry_pw}@fedops1-registry-mongo-svc:27017/fedops-registry?authSource=fedops-registry'
    entries = [
        {'MONGO_URI':web_uri,'JWT_SECRET':pw()},
        {'MONGODB_URI':web_uri},
        {'MONGODB_URI':reg_uri,'AWS_ACCESS_KEY_ID':'registry-app','AWS_SECRET_ACCESS_KEY':object_pw},
        {'MONGO_INITDB_ROOT_USERNAME':'fedops_admin','MONGO_INITDB_ROOT_PASSWORD':mongo_root},
        {'MONGO_INITDB_ROOT_USERNAME':'registry_admin','MONGO_INITDB_ROOT_PASSWORD':registry_root},
        {'MINIO_ROOT_USER':'lab-admin','MINIO_ROOT_PASSWORD':minio_pw},
        {'ACCESS_KEY_ID':'task-app','ACCESS_SECRET_KEY':task_pw,'BUCKET_NAME':'global-model'},
        {'MC_HOST_lab':f'http://lab-admin:{minio_pw}@fedops1-registry-minio-svc:9000',
         'TASK_KEY':'task-app','TASK_SECRET':task_pw,'REGISTRY_KEY':'registry-app','REGISTRY_SECRET':object_pw},
    ]
    for name, data in zip(names, entries):
        create(resource('Secret',name,type='Opaque',stringData=data))
elif args.mode == 'jobs':
    script = '''const admin=db.getSiblingDB('admin');
if (!admin.auth(process.env.MONGO_INITDB_ROOT_USERNAME,process.env.MONGO_INITDB_ROOT_PASSWORD).ok) throw Error('admin authentication failed');
const uri=new URL(process.env.APP_URI); const target=db.getSiblingDB(uri.pathname.slice(1));
const username=decodeURIComponent(uri.username);
if(!target.getUser(username)) target.createUser({user:username,pwd:decodeURIComponent(uri.password),roles:[{role:'readWrite',db:target.getName()}]});
print('application user ready');'''
    for name,host,port,root,app,key in [
        ('bootstrap-web-db','fedops-mongo-np',5000,'fedops-mongo','fedops-web','MONGO_URI'),
        ('bootstrap-registry-db','fedops1-registry-mongo-svc',27017,'fedops-registry-mongo','fedops-registry','MONGODB_URI')]:
        create(job(name,[{'name':'init','image':MONGO,'command':['mongosh','--quiet','--host',host,'--port',str(port),'--eval',script],
                         'env':[env('MONGO_INITDB_ROOT_USERNAME',root),env('MONGO_INITDB_ROOT_PASSWORD',root),env('APP_URI',app,key)]}]))
    def policy(buckets):
        return json.dumps({'Version':'2012-10-17','Statement':[
            {'Effect':'Allow','Action':['s3:GetBucketLocation','s3:ListBucket','s3:ListBucketMultipartUploads'],
             'Resource':['arn:aws:s3:::'+b for b in buckets]},
            {'Effect':'Allow','Action':['s3:GetObject','s3:PutObject','s3:DeleteObject','s3:AbortMultipartUpload','s3:ListMultipartUploadParts'],
             'Resource':['arn:aws:s3:::'+b+'/*' for b in buckets]}]})
    create(resource('ConfigMap','fedops-storage-policies',data={
        'task.json':policy(['global-model','global-model-xai']),
        'registry.json':policy(['fedops-llms','fedops-tasks','fedops-models'])}))
    cmds = [
        ['mb','--ignore-existing']+['lab/'+b for b in ['global-model','global-model-xai','fedops-llms','fedops-tasks','fedops-models']],
        ['admin','user','add','lab','$(TASK_KEY)','$(TASK_SECRET)'],
        ['admin','user','add','lab','$(REGISTRY_KEY)','$(REGISTRY_SECRET)'],
        ['admin','policy','create','lab','task-app','/policies/task.json'],
        ['admin','policy','create','lab','registry-app','/policies/registry.json'],
        ['admin','policy','attach','lab','task-app','--user','$(TASK_KEY)'],
        ['admin','policy','attach','lab','registry-app','--user','$(REGISTRY_KEY)'],
    ]
    containers = [{'name':'step-'+str(i),'image':MC,'args':args,
                   'envFrom':[{'secretRef':{'name':'fedops-bootstrap'}}],
                   'volumeMounts':[{'name':'policies','mountPath':'/policies','readOnly':True}]} for i,args in enumerate(cmds)]
    create(job('bootstrap-object-storage',containers,[{'name':'policies','configMap':{'name':'fedops-storage-policies'}}]))
else:
    raise SystemExit('Use secrets or jobs')

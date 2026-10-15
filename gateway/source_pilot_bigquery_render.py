"""Render exact per-context WIF Cube and metadata-only observer workloads."""
import copy
import hashlib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from pydantic import Field,field_validator,model_validator
from gateway.contracts.canonical import Array,Identifier,StrictModel
from gateway.product_packages import VersionOne
from gateway.adapters.bigquery_runtime import ServiceAccountEmail
from gateway.source_pilot_builder import encoded
from gateway.source_pilot_render import DeploymentPlan,Workload,_render_base,_asset_bytes


class BigQueryWorkload(StrictModel):
    source_id: Identifier
    revision: int = Field(ge=1)
    runtime: Workload
    observer: Workload
    runtime_login: str = Field(pattern=r'^[a-z][a-z0-9_]{0,62}$')
    observer_login: str = Field(pattern=r'^[a-z][a-z0-9_]{0,62}$')
    observer_principal: ServiceAccountEmail


class BigQueryDeploymentPlan(VersionOne):
    base: DeploymentPlan
    bigquery_image: str
    metadata_route: Literal['gke-dataplane-v2','gke-legacy']
    sources: Array[BigQueryWorkload] = Field(min_length=2,max_length=20)

    @field_validator('bigquery_image')
    @classmethod
    def pinned(cls,value):return DeploymentPlan.pinned_image(value)

    @model_validator(mode='after')
    def distinct(self):
        if len({(s.source_id,s.revision) for s in self.sources})!=len(self.sources):raise ValueError('Duplicate source deployment')
        runtime={s.runtime.service_account for s in self.sources};observer={s.observer.service_account for s in self.sources}
        base={s.service_account for s in self.base.workloads.values()}
        if len(runtime)!=len(self.sources) or runtime&observer or (runtime|observer)&base:
            raise ValueError('Source workload identity partition differs')
        return self


def example_bigquery_deployment(config,assets,base):
    rows=[]
    for source in assets.sources.sources:
        if source.provider!='bigquery':continue
        name='consumer' if source.context=='consumer' else 'fixture'
        role='worker' if source.context=='consumer' else 'api'
        signing_ref=assets.bundles.resolve(source.model_bundle_digest).secret_ref
        signing=next(s for s in base['workloads'][role]['secrets'] if s['target']==signing_ref)
        login=lambda kind:next(r.login_name for r in config.source_role_mappings if (r.source_id,r.revision,r.context,r.role_kind)==(source.source_id,source.revision,source.context,kind))
        rows.append({'source_id':source.source_id,'revision':source.revision,'runtime_login':login('runtime'),'observer_login':login('observer'),
            'observer_principal':'usage-observer@replace-project.iam.gserviceaccount.com',
            'runtime':{'service_account':'spaceport-bq-'+name,'secrets':[
                {'name':'spaceport-bq-'+name+'-v1','key':'registry_password','target':'/run/source/secrets/registry_password'},
                {**signing,'target':'/run/source/secrets/cube_signing_key'},
                {'name':'spaceport-bq-'+name+'-tls-v1','key':'tls.crt','target':'/run/edge-tls/tls.crt'},
                {'name':'spaceport-bq-'+name+'-tls-v1','key':'tls.key','target':'/run/edge-tls/tls.key'}]},
            'observer':{'service_account':'spaceport-bq-observer','secrets':[
                {'name':'spaceport-bq-observer-v1','key':'registry_password','target':'/run/source/secrets/registry_password'}]}})
    return {'schema_version':1,'base':base,'bigquery_image':'example.invalid/spaceport-bigquery@sha256:'+'0'*64,
        'metadata_route':'gke-dataplane-v2','sources':rows}


def _tls(host):
    return f'''worker_processes 1;
error_log /dev/stderr crit;
pid /tmp/nginx.pid;
events {{ worker_connections 64; }}
http {{
 access_log off;
 client_body_temp_path /tmp/client_temp;
 proxy_temp_path /tmp/proxy_temp;
 map $http_host $authority {{ default 0; "{host}" 1; "{host}:443" 1; }}
 server {{ listen 8443 ssl; server_name {host}; server_tokens off;
  if ($authority = 0) {{ return 444; }}
  ssl_certificate /run/edge-tls/tls.crt; ssl_certificate_key /run/edge-tls/tls.key;
  ssl_protocols TLSv1.2 TLSv1.3; client_max_body_size 64k;
  proxy_set_header Host {host}; proxy_set_header Forwarded "";
  proxy_set_header X-Forwarded-For ""; proxy_set_header X-Forwarded-Host ""; proxy_set_header X-Forwarded-Proto "";
  proxy_http_version 1.1; proxy_set_header Connection ""; proxy_buffering off; proxy_read_timeout 125s;
  location = /cubejs-api/v1/meta {{ proxy_pass http://127.0.0.1:4000; }}
  location = /cubejs-api/v1/load {{ proxy_pass http://127.0.0.1:4000; }}
  location / {{ return 404; }}
 }}
}}
'''


def render_bigquery(config,assets,value,*,root):
    plan=BigQueryDeploymentPlan.model_validate(value);base=plan.base
    descriptors={(s.source_id,s.revision):s for s in assets.sources.sources if s.provider=='bigquery'}
    if set(descriptors)!={(s.source_id,s.revision) for s in plan.sources}:raise ValueError('Complete exact source workload inventory required')
    submitters={s.auth.expected_principal for s in descriptors.values()}
    mappings={(r.source_id,r.revision,r.context,r.role_kind):r.login_name for r in config.source_role_mappings}
    for row in plan.sources:
        source=descriptors[(row.source_id,row.revision)]
        if (row.runtime_login!=mappings[(source.source_id,source.revision,source.context,'runtime')]
            or row.observer_login!=mappings[(source.source_id,source.revision,source.context,'observer')]
            or row.observer_principal in submitters):raise ValueError('Exact separated source role authority required')
        runtime_secrets={s.target:s for s in row.runtime.secrets};observer_secrets={s.target:s for s in row.observer.secrets}
        if (set(runtime_secrets)!={'/run/source/secrets/registry_password','/run/source/secrets/cube_signing_key','/run/edge-tls/tls.crt','/run/edge-tls/tls.key'}
            or set(observer_secrets)!={'/run/source/secrets/registry_password'}):raise ValueError('Source secret inventory differs')
        gateway=base.workloads['worker' if source.context=='consumer' else 'api']
        signing=next(s for s in gateway.secrets if s.target==assets.bundles.resolve(source.model_bundle_digest).secret_ref)
        mounted=runtime_secrets['/run/source/secrets/cube_signing_key']
        if (signing.name,signing.key)!=(mounted.name,mounted.key):raise ValueError('Gateway/Cube signing references differ')
    result=_render_base(config,assets,base,root=root);items=result['base.json']['items']
    known={item['metadata']['name'] for item in items if item['kind']=='ConfigMap'}
    ca=_asset_bytes(root,base.registry_ca_ref)
    def configmap(data):
        name='spaceport-input-'+hashlib.sha256(data).hexdigest()[:24]
        if len(data)>900000:raise ValueError('ConfigMap too large')
        if name not in known:
            items.append({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':name,'namespace':base.namespace},'immutable':True,'data':{'payload':data.decode('utf-8')}});known.add(name)
        return name
    ca_map=configmap(ca)
    labels=lambda name:{'app.kubernetes.io/part-of':'spaceport-source-pilot','spaceport-role':name}
    peers=lambda values:[{'ipBlock':{'cidr':v}} for v in values]
    metadata=('169.254.169.254/32',80) if plan.metadata_route=='gke-dataplane-v2' else ('169.254.169.252/32',988)
    routes=[]
    for row in plan.sources:
        source=descriptors[(row.source_id,row.revision)]
        token=hashlib.sha256((source.source_id+':'+str(source.revision)).encode()).hexdigest()[:12]
        service='spaceport-bq-'+token;routes.append({'origin':source.cube_endpoint,'service':service,'port':443})
        for observer in (False,True):
            name=service+('-observer' if observer else '')
            workload=row.observer if observer else row.runtime
            envelope={'schema_version':1,'profile':source.model_dump(mode='json'),'deployment_epoch':config.deployment_epoch,
                'bundle_ref':{'digest':source.model_bundle_digest,'context':source.context},
                'ledger':{'host':config.registry.host,'port':config.registry.port,'database':config.registry.database,
                    'user':row.observer_login if observer else row.runtime_login,'password_file':'/run/source/secrets/registry_password',
                    'ca_file':'/run/source/config/registry-ca.pem'}}
            if observer:envelope['observer_auth']={'mode':'gke-workload-identity','expected_principal':row.observer_principal}
            else:envelope.update(signing_key_file='/run/source/secrets/cube_signing_key');envelope['bundle_ref']['root']='/run/source/bundle'
            volumes=[];mounts=[];tls_mounts=[]
            def mount_map(map_name,target,destination):
                volume='file-'+str(len(volumes));volumes.append({'name':volume,'configMap':{'name':map_name,'defaultMode':0o440}})
                destination.append({'name':volume,'mountPath':target,'subPath':'payload','readOnly':True})
            mount_map(configmap(encoded(envelope)),'/run/source/config/runtime.json',mounts)
            mount_map(ca_map,'/run/source/config/registry-ca.pem',mounts)
            if not observer:
                bundle=assets.cube_bundles[source.model_bundle_digest]
                mount_map(configmap(bundle.security_content.encode()),'/run/source/bundle/security.json',mounts)
                for path,data in bundle.model_files.items():mount_map(configmap(data.encode()),'/run/source/bundle/model/'+path,mounts)
                mount_map(configmap(_tls(urlsplit(source.cube_endpoint).hostname).encode()),'/etc/nginx/nginx.conf',tls_mounts)
            for secret in workload.secrets:
                volume='secret-'+str(len(volumes));volumes.append({'name':volume,'secret':{'secretName':secret.name,'defaultMode':0o440,'items':[{'key':secret.key,'path':'payload'}]}})
                (tls_mounts if secret.target.startswith('/run/edge-tls/') else mounts).append({'name':volume,'mountPath':secret.target,'subPath':'payload','readOnly':True})
            volumes.append({'name':'tmp','emptyDir':{'medium':'Memory','sizeLimit':'64Mi'}})
            mounts.append({'name':'tmp','mountPath':'/tmp'});tls_mounts.append({'name':'tmp','mountPath':'/tmp'})
            security={'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}}
            node={'name':'observer' if observer else 'cube','image':plan.bigquery_image,'imagePullPolicy':'IfNotPresent','workingDir':'/cube/conf',
                'args':['/cube/bigquery-runtime/'+('reconciler.cjs' if observer else 'server.cjs')],
                'env':[{'name':'GATEWAY_BIGQUERY_RUNTIME_CONFIG_FILE','value':'/run/source/config/runtime.json'}],
                'securityContext':security,'volumeMounts':mounts,
                'resources':{'requests':{'cpu':'100m','memory':'256Mi'},'limits':{'cpu':'1','memory':'1Gi'}}}
            containers=[node]
            if not observer:
                node['env']+=[{'name':'NODE_ENV','value':'production'},{'name':'CUBEJS_DEV_MODE','value':'false'},{'name':'PORT','value':'4000'}]
                node['readinessProbe']={'tcpSocket':{'port':4000},'periodSeconds':10}
                containers.append({'name':'tls','image':base.publisher_image,'command':['nginx','-g','daemon off;'],'volumeMounts':tls_mounts,
                    'securityContext':{**security,'runAsUser':101,'runAsGroup':101},'ports':[{'name':'https','containerPort':8443}],
                    'resources':{'requests':{'cpu':'50m','memory':'32Mi'},'limits':{'cpu':'200m','memory':'128Mi'}}})
            items.append({'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':name,'namespace':base.namespace},'spec':{
                'replicas':0,'revisionHistoryLimit':2,'strategy':{'type':'Recreate'},'selector':{'matchLabels':labels(name)},
                'template':{'metadata':{'labels':labels(name)},'spec':{'serviceAccountName':workload.service_account,
                    'automountServiceAccountToken':False,'enableServiceLinks':False,'terminationGracePeriodSeconds':config.runtime.shutdown_seconds,
                    'securityContext':{'runAsNonRoot':True,'runAsUser':65532,'runAsGroup':65532,'fsGroup':65532,'seccompProfile':{'type':'RuntimeDefault'}},
                    'containers':containers,'volumes':volumes}}}})
            ingress=[] if observer else [{'from':[{'podSelector':{'matchLabels':labels('worker' if source.context=='consumer' else 'api')}}],
                'ports':[{'protocol':'TCP','port':8443}]}]
            items.append({'apiVersion':'networking.k8s.io/v1','kind':'NetworkPolicy','metadata':{'name':name,'namespace':base.namespace},
                'spec':{'podSelector':{'matchLabels':labels(name)},'policyTypes':['Ingress','Egress'],'ingress':ingress,'egress':[
                    {'to':peers(base.egress.dns_cidrs),'ports':[{'protocol':'UDP','port':53},{'protocol':'TCP','port':53}]},
                    {'to':peers(base.egress.registry_cidrs),'ports':[{'protocol':'TCP','port':config.registry.port}]},
                    {'to':peers(base.egress.destination_cidrs),'ports':[{'protocol':'TCP','port':443}]},
                    {'to':peers([metadata[0]]),'ports':[{'protocol':'TCP','port':metadata[1]}]}]}})
            if not observer:
                items.append({'apiVersion':'v1','kind':'Service','metadata':{'name':name,'namespace':base.namespace},
                    'spec':{'type':'ClusterIP','selector':labels(name),'ports':[{'port':443,'targetPort':'https','protocol':'TCP'}]}})
                items.append({'apiVersion':'networking.k8s.io/v1','kind':'NetworkPolicy','metadata':{'name':name+'-client','namespace':base.namespace},
                    'spec':{'podSelector':{'matchLabels':labels('worker' if source.context=='consumer' else 'api')},'policyTypes':['Egress'],
                        'egress':[{'to':[{'podSelector':{'matchLabels':labels(name)}}],'ports':[{'protocol':'TCP','port':8443}]}]}})
    active=copy.deepcopy(items)
    for item in active:
        if item['kind']=='Deployment':item['spec']['replicas']=1
    result['activation.json']['items']=active
    result['render-status.json']['bigquery_private_routes']=routes
    result['render-status.json']['prerequisites']+=['existing WIF associations for separate source GSAs and metadata-only observer GSA',
        'private DNS and trusted TLS for each exact Cube origin routed to its rendered ClusterIP service',
        'reviewed BigQuery runtime image with exact packages and strict clean environment',
        'metadata server route must match the selected GKE dataplane; no provider-based readiness probe']
    return result

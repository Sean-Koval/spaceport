"""Render private GKE resources and an inspectable activation bundle, never apply.

Existing version-named Secrets, KSAs, TLS certificates, private registry and
network-level egress policy are operator prerequisites. Native NetworkPolicy
below restricts packet destinations; it does not implement hostname filtering.
"""
import copy
import hashlib
import ipaddress
import json
import re
import ssl
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field,field_validator,model_validator
from gateway.contracts.canonical import Array,Identifier,StrictModel,canonical_digest
from gateway.product_packages import VersionOne,read_asset
from gateway.source_pilot_config import AssetRef
from gateway.source_pilot_builder import encoded
from gateway.pilot_config import _read_file
from gateway.source_pilot_bootstrap import bootstrap_plan_digest

Name=str


class SecretMount(StrictModel):
    name: str = Field(pattern=r'^[a-z][a-z0-9-]{0,50}-v[1-9][0-9]{0,8}$')
    key: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
    target: str = Field(pattern=r'^/run/(?:secrets/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}|auth/credential-hashes\.json|source/secrets/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}|edge-tls/tls\.(?:crt|key))$')


class Workload(StrictModel):
    service_account: str = Field(pattern=r'^[a-z][a-z0-9-]{0,62}$')
    secrets: Array[SecretMount] = Field(max_length=20)

    @model_validator(mode='after')
    def unique(self):
        if len({r.target for r in self.secrets})!=len(self.secrets):raise ValueError('Duplicate secret target')
        return self


class Egress(StrictModel):
    mode: Literal['approved-network-egress']
    review_ref: Identifier
    destination_cidrs: Array[str] = Field(min_length=1,max_length=30)
    dns_cidrs: Array[str] = Field(min_length=1,max_length=4)
    registry_cidrs: Array[str] = Field(min_length=1,max_length=4)
    ingress_cidrs: Array[str] = Field(min_length=1,max_length=10)

    @field_validator('destination_cidrs','dns_cidrs','registry_cidrs','ingress_cidrs')
    @classmethod
    def bounded_networks(cls,values):
        for value in values:
            network=ipaddress.ip_network(value,strict=True)
            if network.prefixlen<(24 if network.version==4 else 64):raise ValueError('Broad network destinations are unsupported')
            if str(network)!=value:raise ValueError('Canonical CIDR required')
        if len(set(values))!=len(values):raise ValueError('Duplicate network destination')
        return values


class DeploymentPlan(VersionOne):
    namespace: str = Field(pattern=r'^[a-z][a-z0-9-]{0,62}$')
    gateway_image: str
    publisher_image: str
    registry_ca_ref: AssetRef
    workloads: dict[Literal['api','worker','retention','bootstrap','edge'],Workload] = Field(min_length=5,max_length=5)
    egress: Egress

    @field_validator('gateway_image','publisher_image')
    @classmethod
    def pinned_image(cls,value):
        if not re.fullmatch(r'[a-z0-9][a-z0-9./_-]{0,240}@sha256:[a-f0-9]{64}',value):raise ValueError('Exact reviewed image digest required')
        return value

    @model_validator(mode='after')
    def identities(self):
        if len({w.service_account for w in self.workloads.values()})!=len(self.workloads):raise ValueError('Independent workload KSAs required')
        return self


def required_secrets(config,assets,role):
    if role=='edge':return {'/run/edge-tls/tls.crt','/run/edge-tls/tls.key'}
    roles={'api':['app'],'worker':['app'],'retention':['app','audit_retention'],'bootstrap':['owner']}[role]
    result={config.database_roles[r].password_file for r in roles}
    if role in {'api','worker'}:
        result|={'/run/secrets/audit_hmac','/run/secrets/cursor_key'}
        context='fixture_test' if role=='api' else 'consumer'
        for source in assets.sources.sources:
            if source.provider=='domo' and source.context==context:
                result|={source.auth.client_id_ref,source.auth.client_secret_ref,
                    config.database_roles['domo_fixture' if role=='api' else 'domo_consumer'].password_file}
            if source.provider=='bigquery' and source.context==context:
                result.add(assets.bundles.resolve(source.model_bundle_digest).secret_ref)
    if config.credential_hash_file and role in {'api','bootstrap'}:result.add(config.credential_hash_file)
    return result


def example_deployment(config,assets):
    base=_example_base(config,assets)
    if any(s.provider=='bigquery' for s in assets.sources.sources):
        from gateway.source_pilot_bigquery_render import example_bigquery_deployment
        return example_bigquery_deployment(config,assets,base)
    return base


def _example_base(config,assets):
    return {'schema_version':1,'namespace':'spaceport-pilot','gateway_image':'example.invalid/spaceport@sha256:'+'0'*64,
        'publisher_image':'example.invalid/publisher@sha256:'+'0'*64,
        'registry_ca_ref':{'path':'registry-ca.pem','sha256':'0'*64},
        'workloads':{role:{'service_account':'spaceport-'+role,'secrets':[{'name':'spaceport-'+role+'-v1',
            'key':Path(path).name,'target':path} for path in sorted(required_secrets(config,assets,role))]}
            for role in ('api','worker','retention','bootstrap','edge')},
        'egress':{'mode':'approved-network-egress','review_ref':'REPLACE-NETWORK-REVIEW',
            'destination_cidrs':['192.0.2.10/32'],'dns_cidrs':['192.0.2.53/32'],
            'registry_cidrs':['192.0.2.54/32'],'ingress_cidrs':['192.0.2.55/32']}}


def _asset_bytes(root,ref):
    ref=AssetRef.model_validate(ref.model_dump(include={'path','sha256'}) if isinstance(ref,AssetRef) else ref)
    data=_read_file(str(Path(root)/ref.path),900000)
    if hashlib.sha256(data).hexdigest()!=ref.sha256:raise ValueError('Retained render bytes differ')
    return data


def _inventory(config,assets,root):
    files={'pilot.json':encoded(config.model_dump(mode='json'))}
    refs=[config.source_catalog_ref,config.product_catalog_ref,config.identity_mapping_ref]
    for ref in tuple(refs):
        value=read_asset(root,ref)
        if 'sources' in value:refs.extend(AssetRef.model_validate(v) for v in value['sources'])
        if 'packages' in value:refs.extend(AssetRef.model_validate(v) for v in value['packages'])
    for retained in assets.packages.packages:
        p=retained.package
        refs.extend(getattr(p,name) for name in ('consumer_binding_ref','fixture_binding_ref','policy_seed_ref',
            'fixture_catalog_ref','bundle_catalog_ref','fixture_equivalence_ref'))
    for ref in refs:
        data=_asset_bytes(root,ref)
        if ref.path in files and files[ref.path]!=data:raise ValueError('Render path aliases different contents')
        files[ref.path]=data
    if len(files)>128 or sum(map(len,files.values()))>8*1024*1024:raise ValueError('Render inventory exceeds bound')
    return files


def _ca(data):
    text=data.decode('ascii')
    pattern=r'-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----'
    if not re.findall(pattern,text) or re.sub(pattern,'',text).strip():raise ValueError('Only public CA certificates are supported')
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);context.load_verify_locations(cadata=text)
    if not context.get_ca_certs():raise ValueError('Registry CA is unavailable')


def _edge_config(host):
    return f'''worker_processes 1;
error_log /dev/stderr crit;
pid /tmp/nginx.pid;
events {{ worker_connections 128; }}
http {{
 include /etc/nginx/mime.types;
 access_log off;
 client_body_temp_path /tmp/client_temp;
 proxy_temp_path /tmp/proxy_temp;
 map $http_host $valid_authority {{ default 0; "{host}" 1; "{host}:443" 1; }}
 server {{
  listen 8443 ssl; server_name {host}; server_tokens off;
  if ($valid_authority = 0) {{ return 444; }}
  ssl_certificate /run/edge-tls/tls.crt; ssl_certificate_key /run/edge-tls/tls.key;
  ssl_protocols TLSv1.2 TLSv1.3;
  client_max_body_size 64k; root /usr/share/nginx/html;
  add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'" always;
  add_header Referrer-Policy no-referrer always;
  add_header X-Content-Type-Options nosniff always;
  proxy_http_version 1.1; proxy_set_header Host {host}; proxy_set_header Connection "";
  proxy_set_header Forwarded ""; proxy_set_header X-Forwarded-For "";
  proxy_set_header X-Forwarded-Host ""; proxy_set_header X-Forwarded-Proto "";
  proxy_buffering off; proxy_read_timeout 180s;
  location = /.well-known/oauth-protected-resource/mcp {{ proxy_pass http://spaceport-api:8000; }}
  location ~ ^/(v1(?:/|$)|mcp(?:/|$)|healthz$|readyz$) {{ proxy_pass http://spaceport-api:8000; }}
  location / {{ try_files $uri $uri/ /index.html; }}
 }}
}}
'''


def render(config,assets,value,*,root):
    if any(s.provider=='bigquery' for s in assets.sources.sources):
        from gateway.source_pilot_bigquery_render import render_bigquery
        return render_bigquery(config,assets,value,root=root)
    return _render_base(config,assets,value,root=root)


def _render_base(config,assets,value,*,root):
    plan=DeploymentPlan.model_validate(value)
    if config.registry.ca_file!='/run/source/config/registry-ca.pem':raise ValueError('Unsupported runtime CA mount')
    for role,workload in plan.workloads.items():
        if {s.target for s in workload.secrets}!=required_secrets(config,assets,role):raise ValueError('Secret inventory does not match process authority')
    ca=_asset_bytes(root,plan.registry_ca_ref);_ca(ca)
    inventory=_inventory(config,assets,root)
    items=[];maps={}
    def configmap(data):
        digest=hashlib.sha256(data).hexdigest();name='spaceport-input-'+digest[:24]
        if len(data)>900000:raise ValueError('ConfigMap exceeds the supported size bound')
        if name not in maps:
            maps[name]=data
            items.append({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':name,'namespace':plan.namespace},
                'immutable':True,'data':{'payload':data.decode('utf-8')}})
        return name
    for data in inventory.values():configmap(data)
    ca_map=configmap(ca);edge_map=configmap(_edge_config(urlsplit(config.public_origin).hostname).encode())
    def labels(role):return {'app.kubernetes.io/part-of':'spaceport-source-pilot','spaceport-role':role}
    security={'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}}
    def pod(role,command):
        workload=plan.workloads[role];mounts=[];volumes=[]
        def mount_config(name,target):
            volume='file-'+str(len(volumes));volumes.append({'name':volume,'configMap':{'name':name,'defaultMode':0o440}})
            mounts.append({'name':volume,'mountPath':target,'subPath':'payload','readOnly':True})
        if role!='edge':
            for path,data in inventory.items():mount_config(configmap(data),'/run/source/packages/'+path)
            mount_config(ca_map,config.registry.ca_file)
        else:mount_config(edge_map,'/etc/nginx/nginx.conf')
        for secret in workload.secrets:
            volume='secret-'+str(len(volumes));volumes.append({'name':volume,'secret':{'secretName':secret.name,'defaultMode':0o440,
                'items':[{'key':secret.key,'path':'payload'}]}})
            mounts.append({'name':volume,'mountPath':secret.target,'subPath':'payload','readOnly':True})
        targets=[m['mountPath'] for m in mounts]
        if len(targets)!=len(set(targets)) or any(a!=b and b.startswith(a+'/') for a in targets for b in targets):raise ValueError('Mount target shadowing')
        volumes.append({'name':'tmp','emptyDir':{'medium':'Memory','sizeLimit':'32Mi'}})
        mounts.append({'name':'tmp','mountPath':'/tmp'})
        container={'name':role,'image':plan.publisher_image if role=='edge' else plan.gateway_image,'imagePullPolicy':'IfNotPresent',
            'command':command,'securityContext':security,'volumeMounts':mounts,
            'resources':{'requests':{'cpu':'100m','memory':'128Mi'},'limits':{'cpu':'1','memory':'512Mi'}}}
        if role!='edge':container['env']=[{'name':'GATEWAY_SOURCE_PILOT_CONFIG_FILE','value':'/run/source/packages/pilot.json'}]
        if role=='api':
            container['ports']=[{'name':'http','containerPort':8000}]
            probe={'httpGet':{'path':'/healthz','port':'http','httpHeaders':[{'name':'Host','value':urlsplit(config.public_origin).netloc}]},'timeoutSeconds':3,'periodSeconds':10}
            container['livenessProbe']=probe;container['readinessProbe']=copy.deepcopy(probe)
            container['readinessProbe']['httpGet']['path']='/readyz'
        if role=='edge':container['ports']=[{'name':'https','containerPort':8443}]
        return {'metadata':{'labels':labels(role)},'spec':{'serviceAccountName':workload.service_account,
            'automountServiceAccountToken':False,'enableServiceLinks':False,'terminationGracePeriodSeconds':config.runtime.shutdown_seconds,
            'securityContext':{'runAsNonRoot':True,'runAsUser':101 if role=='edge' else 65532,'runAsGroup':101 if role=='edge' else 65532,
                'fsGroup':101 if role=='edge' else 65532,'seccompProfile':{'type':'RuntimeDefault'}},'containers':[container],'volumes':volumes}}
    commands={'api':['python','-m','uvicorn','gateway.source_pilot_runtime:app_factory','--factory','--host','0.0.0.0','--port','8000',
        '--no-access-log','--no-proxy-headers','--timeout-graceful-shutdown','125'],
        'worker':['python','-m','gateway.source_pilot_runtime','worker'],'retention':['python','-m','gateway.source_pilot_runtime','retention'],
        'edge':['nginx','-g','daemon off;']}
    for role,command in commands.items():
        items.append({'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':'spaceport-'+role,'namespace':plan.namespace},
            'spec':{'replicas':0,'revisionHistoryLimit':2,'strategy':{'type':'Recreate'},'selector':{'matchLabels':labels(role)},'template':pod(role,command)}})
    for operation in ('migrate','provision'):
        command=['python','-m','gateway.source_pilot_bootstrap',operation]
        if operation=='provision':command+=['--expected-plan-digest',bootstrap_plan_digest(config,assets)]
        template=pod('bootstrap',command);template['spec']['restartPolicy']='Never'
        items.append({'apiVersion':'batch/v1','kind':'CronJob','metadata':{'name':'spaceport-'+operation,'namespace':plan.namespace},
            'spec':{'schedule':'0 0 1 1 *','suspend':True,'concurrencyPolicy':'Forbid','jobTemplate':{'spec':{'backoffLimit':0,
                'activeDeadlineSeconds':300,'template':template}}}})
    for role,port,target in (('api',8000,'http'),('edge',443,'https')):
        items.append({'apiVersion':'v1','kind':'Service','metadata':{'name':'spaceport-'+role,'namespace':plan.namespace},
            'spec':{'type':'ClusterIP','selector':labels(role),'ports':[{'port':port,'targetPort':target,'protocol':'TCP'}]}})
    peers=lambda cidrs:[{'ipBlock':{'cidr':cidr}} for cidr in cidrs]
    items.append({'apiVersion':'networking.k8s.io/v1','kind':'NetworkPolicy','metadata':{'name':'spaceport-private-boundary','namespace':plan.namespace,
        'annotations':{'spaceport.example.com/network-review':plan.egress.review_ref,'spaceport.example.com/fqdn-enforcement':'external-network-prerequisite'}},
        'spec':{'podSelector':{'matchLabels':{'app.kubernetes.io/part-of':'spaceport-source-pilot'},
                'matchExpressions':[{'key':'spaceport-role','operator':'In','values':['api','worker','retention','bootstrap','edge']}]},'policyTypes':['Ingress','Egress'],
            'ingress':[{'from':peers(plan.egress.ingress_cidrs),'ports':[{'protocol':'TCP','port':8443}]},
                {'from':[{'podSelector':{'matchLabels':labels('edge')}}],'ports':[{'protocol':'TCP','port':8000}]}],
            'egress':[{'to':peers(plan.egress.dns_cidrs),'ports':[{'protocol':'UDP','port':53},{'protocol':'TCP','port':53}]},
                {'to':peers(plan.egress.registry_cidrs),'ports':[{'protocol':'TCP','port':config.registry.port}]},
                {'to':peers(plan.egress.destination_cidrs),'ports':[{'protocol':'TCP','port':443}]},
                {'to':[{'podSelector':{'matchLabels':labels('api')}}],'ports':[{'protocol':'TCP','port':8000}]}]}})
    active=copy.deepcopy(items)
    for item in active:
        if item['kind']=='Deployment':item['spec']['replicas']=1
    return {'base.json':{'apiVersion':'v1','kind':'List','items':items},
        'activation.json':{'apiVersion':'v1','kind':'List','items':active},
        'render-status.json':{'schema_version':1,'config_digest':canonical_digest(config),'package_digests':[p.package.digest for p in assets.packages.packages],
            'source_observation':'unverified','deployment_observation':'unverified','rotation':'new version-named Secret and pod rollout required',
            'prerequisites':['existing namespace and isolated KSAs','existing version-named Secret objects and TLS certificates',
                'approved private registry and schema roles','approved packet destinations plus network-level hostname/egress enforcement',
                'approved route to the private edge ClusterIP; no public Ingress is rendered']}}

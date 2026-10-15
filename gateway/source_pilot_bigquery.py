"""Retained static BigQuery assets for the fixed daily account-usage template."""
from dataclasses import dataclass
import hashlib
import json
import re

from pydantic import Field,model_validator
from gateway.contracts.canonical import Digest,StrictModel
from gateway.adapters.bigquery_runtime import BigQueryBinding,BigQuerySource,bigquery_security_contract,check_bigquery_binding
from gateway.source_pilot_samples import weekly_manifest


def cube_bundle_digest(security_content,model_files):
    contents={'security.json':security_content,**{'model/'+name:value for name,value in model_files.items()}}
    digest=hashlib.sha256(b'spaceport-cube-bigquery-bundle-v1\0')
    for name in sorted(contents):
        raw=contents[name].encode('utf-8')
        digest.update(name.encode('ascii')+b'\0'+str(len(raw)).encode('ascii')+b'\0'+raw)
    return digest.hexdigest()


class CubeBundleFiles(StrictModel):
    digest: Digest
    security_content: str = Field(min_length=1,max_length=65536)
    model_files: dict[str,str] = Field(min_length=1,max_length=16)

    @model_validator(mode='after')
    def exact_bytes(self):
        if (len(self.security_content.encode('utf-8'))>65536 or any(
            re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}\.yml',name) is None
            or not 1<=len(value.encode('utf-8'))<=65536
            or re.search(r'COMPILE_CONTEXT|securityContext|security_context|USER_CONTEXT|\{\{|\{%|\$\{|FILTER_PARAMS|FILTER_GROUP',value)
            for name,value in self.model_files.items())
            or cube_bundle_digest(self.security_content,self.model_files)!=self.digest):
            raise ValueError('Exact bounded static Cube bundle bytes required')
        return self


@dataclass(frozen=True)
class BigQueryWeeklyAssets:
    source: BigQuerySource
    manifest: object
    binding: BigQueryBinding
    bundle: CubeBundleFiles


def build_weekly_bigquery_assets(source):
    source=BigQuerySource.model_validate(source.model_dump(mode='python'))
    if len(source.tables)!=1:raise ValueError('The weekly template requires one exact preaggregated table')
    raw=dict(context=source.context,binding_id='weekly-usage-'+source.context.replace('_','-'),revision=1,
        adapter_revision='cube-bigquery-v1',source_id=source.source_id,source_revision=source.revision,
        source_digest=source.digest,model_bundle_digest=source.model_bundle_digest,
        fields={'day':{'member':'Usage.day','kind':'time','cube_type':'time','response_key':'Usage.day.day','granularity':'day'},
            'account':{'member':'Usage.account','kind':'dimension','cube_type':'string','response_key':'Usage.account'},
            'usage_events':{'member':'Usage.usage_events','kind':'measure','cube_type':'number','response_key':'Usage.usage_events'}},
        input_fields={'account':'account'},scope={'tenant_member':'Usage.tenant_id','access_group_member':'Usage.access_group'},
        freshness_mode='unknown',checkpoint_member=None)
    provisional=BigQueryBinding.model_validate(raw)
    security=(json.dumps(bigquery_security_contract(provisional),sort_keys=True,indent=2)+'\n')
    model=f'''# Operator-reviewed producer-preaggregated daily account counts.
cubes:
  - name: Usage
    sql: >
      SELECT row_key, tenant_id, access_group, event_date, account_id, usage_events
      FROM `{source.data_project}.{source.dataset}.{source.tables[0]}`
    refresh_key:
      sql: SELECT 'stable' AS refresh_key
    measures:
      - name: usage_events
        sql: CAST(SUM({{CUBE}}.usage_events) AS STRING)
        type: number
    dimensions:
      - name: row_key
        sql: row_key
        type: string
        primary_key: true
        public: false
      - name: tenant_id
        sql: tenant_id
        type: string
      - name: access_group
        sql: access_group
        type: string
      - name: day
        sql: event_date
        type: time
      - name: account
        sql: account_id
        type: string
'''
    files={'usage.yml':model};digest=cube_bundle_digest(security,files)
    bundle=CubeBundleFiles(digest=digest,security_content=security,model_files=files)
    source=BigQuerySource.model_validate({**source.model_dump(mode='json'),'model_bundle_digest':digest})
    binding=BigQueryBinding.model_validate({**raw,'source_digest':source.digest,'model_bundle_digest':digest})
    manifest=weekly_manifest(adapter_revision='cube-bigquery-v1',binding_id=binding.binding_id,model_bundle_digest=digest)
    check_bigquery_binding(binding,manifest,digest=binding.digest)
    return BigQueryWeeklyAssets(source,manifest,binding,bundle)

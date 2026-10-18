"""Real exact catalog-description release; owner fixtures, no provider source I/O."""
import asyncio
from copy import deepcopy

import pytest

from gateway.adapters.base import StaticBindingResolver
from gateway.adapters.bigquery_runtime import BigQueryBinding
from gateway.api.catalog import CatalogService
from gateway.api.cursors import PageCursorCodec
from gateway.auth.principals import PrincipalStore
from gateway.contracts.clock import SystemClock
from gateway.contracts.errors import GatewayError
from gateway.policy.audit import AuditWriter
from gateway.policy.service import PolicyService
from gateway.source_access import SourceAccessService
from tests.integration.test_source_approval_registry import install
from tests.unit.test_source_approval_contracts import principal

pytestmark = pytest.mark.postgres


def composition(db, valid_manifest, *, approved=True, expires=None):
    from tests.integration.test_source_approval_transactions import consumer_setup
    value=deepcopy(valid_manifest)
    value['freshness']['required']=False
    document,manifest,_,product=consumer_setup(db,value,approve=False,expires=expires)
    document['audiences'][0]['actions']+=['discover','describe']
    receipt=install(db,document) if approved else None
    binding=BigQueryBinding.model_validate(db.scalar("SELECT content FROM gateway.binding_revisions WHERE id='consumer-binding-row'"))
    audit=AuditWriter(b'synthetic-real-catalog-release-key-32')
    access=SourceAccessService(db.registry,audit)
    policy=PolicyService(db.registry,PrincipalStore(db.registry.sessions),audit,source_access=access)
    service=CatalogService(registry=db.registry,policy=policy,bindings=StaticBindingResolver([binding]),
        cursors=PageCursorCodec(b'c'*32,SystemClock()))
    return service,receipt,product


def test_real_catalog_description_releases_only_after_exact_source_allow(registry_database,valid_manifest):
    db=registry_database
    service,receipt,product=composition(db,valid_manifest)
    sessions_sql="SELECT coalesce(jsonb_agg(to_jsonb(s) ORDER BY id),'[]'::jsonb) FROM gateway.source_sessions s"
    jobs_sql="SELECT coalesce(jsonb_agg(to_jsonb(j) ORDER BY session_id,job_id),'[]'::jsonb) FROM gateway.bigquery_source_jobs j"
    groups_sql="SELECT coalesce(jsonb_agg(to_jsonb(g) ORDER BY id),'[]'::jsonb) FROM gateway.source_operation_groups g"
    sends_sql="SELECT coalesce(jsonb_agg(to_jsonb(d) ORDER BY session_id),'[]'::jsonb) FROM gateway.domo_source_sends d"
    retained_sessions=db.scalar(sessions_sql)
    retained_jobs=db.scalar(jobs_sql)
    retained_groups=db.scalar(groups_sql)
    retained_sends=db.scalar(sends_sql)
    assert retained_sessions and retained_jobs
    assert all(row['context']=='fixture_test' for row in retained_sessions)
    view=asyncio.run(service.describe(principal(),product.manifest.product_id,product.manifest.version))
    assert view.digest==product.manifest.content_digest
    assert set(view.outputs)==set(product.manifest.outputs)
    assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='describe' AND decision='allow' AND source_approval_id=%s",(receipt['id'],))==1
    assert db.scalar('SELECT count(*) FROM gateway.executions')==0
    assert db.scalar(sessions_sql)==retained_sessions
    assert db.scalar(jobs_sql)==retained_jobs
    assert db.scalar(groups_sql)==retained_groups
    assert db.scalar(sends_sql)==retained_sends
    assert db.scalar("SELECT count(*) FROM gateway.source_sessions WHERE context='consumer'")==0


@pytest.mark.parametrize('missing',[True,False])
def test_real_catalog_missing_or_wrong_client_approval_cannot_reveal_definition(registry_database,valid_manifest,missing):
    db=registry_database
    service,_,product=composition(db,valid_manifest,approved=not missing)
    actor=principal() if missing else principal().model_copy(update={'service_client_id':'unreviewed-client'})
    with pytest.raises(GatewayError) as caught:
        asyncio.run(service.describe(actor,product.manifest.product_id,product.manifest.version))
    assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
    assert product.manifest.name not in str(caught.value)
    assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='describe' AND decision='allow'")==0
    assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='describe' AND decision='deny'")==1


def test_real_catalog_expiry_after_projection_discards_entire_description(registry_database,valid_manifest):
    from datetime import timezone
    db=registry_database
    expiry=db.scalar("SELECT clock_timestamp()+interval '4 seconds'")
    service,receipt,product=composition(db,valid_manifest,
        expires=expiry.astimezone(timezone.utc).isoformat().replace('+00:00','Z'))
    original=service.policy.source_access.finalize
    reached=[]
    async def expire_before_finalizer(uow,requests):
        reached.append(requests[0].projection.approval_id)
        db.scalar("SELECT pg_sleep(greatest(0,extract(epoch FROM (%s::timestamptz-clock_timestamp()))+0.02))",(expiry,))
        return await original(uow,requests)
    service.policy.source_access.finalize=expire_before_finalizer
    with pytest.raises(GatewayError) as caught:
        asyncio.run(service.describe(principal(),product.manifest.product_id,product.manifest.version))
    assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN' and reached==[receipt['id']]
    assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='describe' AND decision='allow'")==0
    assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='describe' AND decision='deny' AND correlation_id=%s",(caught.value.request_id,))==1

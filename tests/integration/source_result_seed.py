"""Exact owner-installed service result fixtures with real local source settlement.

Only disposable SQL roles and synthetic rows are used. There is no provider I/O,
credential lookup, disabled guard or production approval bootstrap.
"""
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

from gateway.contracts.identity import Principal


def result_principal():
    return Principal(id='result-service',subject='synthetic-result-service',kind='service',service_client_id='result-client')


def result_approval_assets(db, valid_manifest):
    """Explicit synthetic owner setup; never an app/bootstrap approval path."""
    from psycopg.types.json import Jsonb
    from gateway.contracts.canonical import canonical_digest
    from gateway.adapters.bigquery_runtime import BigQueryBinding, BigQuerySource
    from gateway.source_access_contracts import physical_source_identity_digest, source_credential_binding_digest
    from tests.unit.test_source_approval_contracts import package
    from tests.unit.test_bigquery_serving_source import source_value
    from tests.contract.test_cube_adapter import synthetic
    from tests.integration.test_audit import seed
    doc, manifest = package.__wrapped__(valid_manifest)
    seed(db, roles=('consumer','author','publisher'))
    db.execute_owner("UPDATE gateway.principals SET kind='user',service_client_id='test-client' WHERE id='north-user'")
    original = synthetic()[0].model_dump(mode='json')
    for key in ('constraint_members','watermark_member'):
        original.pop(key)
    bindings=[]; sources=[]; lineage=[]
    for kind, context in (('consumer','consumer'),('fixture','fixture_test')):
        model = manifest.model_bundle_digest if kind=='consumer' else '1'*64
        source = BigQuerySource.model_validate(source_value(source_id=kind+'-source',context=context,model_bundle_digest=model,
            auth={'mode':'gke-workload-identity','expected_principal':kind+'@billing-project.iam.gserviceaccount.com'},
            data_project=kind+'-data-project',dataset=kind+'_dataset',cube_endpoint='https://'+kind+'.synthetic.svc.cluster.local'))
        source_pin=dict(source_id=source.source_id,revision=source.revision,provider=source.provider,context=context,
            descriptor_digest=source.digest,physical_identity_digest=physical_source_identity_digest(source),credential_binding_digest=source_credential_binding_digest(source))
        binding=BigQueryBinding.model_validate(original | dict(context=context,binding_id=doc['bindings'][kind]['binding_id'],
            adapter_revision='cube-bigquery-v1',source_id=source.source_id,source_revision=source.revision,source_digest=source.digest,
            model_bundle_digest=model,scope={'tenant_member':'Orders.tenant_id','access_group_member':'Orders.access_group'},
            freshness_mode='same-query-checkpoint',checkpoint_member='Orders.loaded_at'))
        physical={'provider':'bigquery','billing_project':source.billing_project,'location':source.location}
        db.execute_owner('''INSERT INTO gateway.source_pools(capacity_key,provider,physical_identity,max_active,max_dispatches)
            VALUES(%s,'bigquery',%s,2,8000) ON CONFLICT(capacity_key) DO NOTHING''',(source.capacity_pool_id,Jsonb(physical)))
        db.execute_owner('''INSERT INTO gateway.source_profiles(source_id,revision,context,provider,capacity_key,descriptor_digest,descriptor)
            VALUES(%s,1,%s,'bigquery',%s,%s,%s)''',(source.source_id,context,source.capacity_pool_id,source.digest,Jsonb(source.model_dump(mode='json'))))
        db.execute_owner('''INSERT INTO gateway.binding_revisions(id,binding_id,revision,content,content_digest,model_bundle_digest,adapter_revision,actor_id)
            VALUES(%s,%s,1,%s,%s,%s,'cube-bigquery-v1','synthetic-operator')''',
            (kind+'-binding-row',binding.binding_id,Jsonb(binding.model_dump(mode='json')),binding.digest,model))
        doc['bindings'][kind]={'binding_id':binding.binding_id,'revision':1,'content_digest':binding.digest}
        doc['sources'][kind]=source_pin
        for name,field in binding.fields.items():
            lineage.append(dict(id=name,source_context=context,source_member=field.member,purpose='exposed',classification='non-personal'))
            if field.kind!='measure':
                lineage.append(dict(id=name,source_context=context,source_member=field.member,purpose='grouping',classification='non-personal'))
        for name in binding.constraint_members:
            lineage.append(dict(id=name,source_context=context,source_member=name,purpose='hidden_scope',classification='non-personal'))
        for name in manifest.inputs:
            member=binding.fields[binding.input_fields[name]].member if name in binding.input_fields else next(f.member for f in binding.fields.values() if f.kind=='time')
            lineage.append(dict(id=name,source_context=context,source_member=member,purpose='source_input',classification='non-personal'))
        bindings.append(binding);sources.append(source)
    for binding in bindings:
        lineage.append(dict(id=binding.checkpoint_member,source_context=binding.context,
            source_member=binding.checkpoint_member,purpose='checkpoint',classification='non-personal'))
    doc['lineage']=lineage
    actor=result_principal()
    db.execute_owner('INSERT INTO gateway.principals(id,subject,kind,service_client_id) VALUES(%s,%s,%s,%s)',
        (actor.id,actor.subject,actor.kind,actor.service_client_id))
    db.execute_owner("INSERT INTO gateway.principal_grants(principal_id,policy_id,tenant_id,roles,entitlements) VALUES(%s,'revenue-access','north','[\"consumer\",\"policy_admin\"]','{\"groups\":[\"north\"]}')",(actor.id,))
    from tests.unit.test_source_approval_contracts import audience
    doc['audiences'].append(audience(id='retained-result-service',principal_id=actor.id,principal_kind=actor.kind,
        client_ids=[actor.service_client_id],actions=['discover','describe','execute','result','publish']))
    return doc,manifest,bindings,sources



@contextmanager
def mapped_result_provider(db,source):
    """Disposable restricted SQL identity, removed before the helper returns."""
    import secrets
    import psycopg
    from psycopg import sql
    from sqlalchemy import create_engine
    from gateway.source_lifecycle import provision_sources
    from tests.integration.conftest import connection_kwargs
    name='gws_'+secrets.token_hex(8)
    password=secrets.token_urlsafe(32)
    url=db.urls['app'].set(username=name,password=password)
    created=False
    try:
        with psycopg.connect(**connection_kwargs(db.admin_url),autocommit=True) as admin:
            admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}').format(sql.Identifier(name),sql.Literal(password)))
            created=True
            admin.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(db.urls['owner'].database),sql.Identifier(name)))
        settings=db.scalar('SELECT row_to_json(s) FROM gateway.source_runtime_settings s WHERE singleton')
        epoch=settings['deployment_epoch'] if settings['deployment_epoch']!='unconfigured' else 'result-release-test'
        engine=create_engine(db.urls['owner'])
        try:
            with engine.begin() as connection:
                provision_sources(connection,[source],[dict(login_name=name,source_id=source.source_id,
                    revision=source.revision,context='consumer',role_kind='runtime')],deployment_epoch=epoch,
                    max_active_operations=settings['max_active_operations'])
        finally: engine.dispose()
        yield url,epoch
    finally:
        if created:
            with psycopg.connect(**connection_kwargs(db.admin_url.set(database=db.urls['owner'].database)),autocommit=True) as admin:
                admin.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(name)))
            with psycopg.connect(**connection_kwargs(db.admin_url),autocommit=True) as admin:
                admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(name)))


async def settle_result_execution(db,*,execution,product,actor,decision,binding,policy):
    """Genuine app group/session and exact restricted terminal proof; no query."""
    from datetime import timedelta
    import psycopg
    from psycopg.types.json import Jsonb
    from sqlalchemy import text
    from gateway.adapters.base import authorization_scope_digest
    from gateway.adapters.bigquery_runtime import BigQuerySource,compile_bigquery_query
    from gateway.contracts.canonical import canonical_digest
    from gateway.contracts.execution import ExecutionSpec,PreparedQuery
    from gateway.contracts.query import QueryRequest
    from gateway.registry.models import ExecutionState,ExecutionLease
    from gateway.source_lifecycle import SourceLifecycle,SourceSessionSpec
    from tests.integration.conftest import connection_kwargs
    from tests.integration.test_source_lifecycle import intent
    source=BigQuerySource.model_validate(db.scalar('SELECT descriptor FROM gateway.source_profiles WHERE source_id=%s AND revision=%s AND context=\'consumer\'',
        (binding.source_id,binding.source_revision)))
    with mapped_result_provider(db,source) as (url,epoch):
        lifecycle=SourceLifecycle(db.registry,policy,SimpleNamespace(resolve=lambda ref:source),deployment_epoch=epoch)
        spec=ExecutionSpec(principal_id=actor.id,pinned_product=product,canonical_query=execution.canonical_query,
            canonical_hash=execution.canonical_hash,policy_revision=decision.policy_revision,context='consumer',request_id=execution.request_id)
        token=str(uuid4())
        async with db.registry.transaction() as uow:
            await lifecycle._lock_authority(uow,product)
            fresh=await policy.evaluate(actor,'execute',product,uow)
            assert fresh==decision
            state=await uow.session.get(ExecutionState,execution.id,with_for_update=True)
            now=await uow.session.scalar(text('SELECT clock_timestamp()'))
            state.state='running';state.started_at=now
            uow.session.add(ExecutionLease(execution_id=execution.id,worker_id='result-fixture',lease_token=token,
                generation=1,expires_at=now+timedelta(seconds=30),source_reserved_until=execution.deadline_at))
            await uow.session.flush()
            group=await lifecycle.reserve_consumer(uow,(execution,spec,actor,decision,binding),fresh,execution.deadline_at)
            prepared=PreparedQuery(endpoint_id='result-fixture',model_bundle_digest=binding.model_bundle_digest,
                cube_query=compile_bigquery_query(binding,product.manifest,QueryRequest.model_validate(spec.canonical_query),decision))
            plan=SourceSessionSpec(provider='bigquery',source_id=source.source_id,source_revision=source.revision,
                context='consumer',capacity_key=source.capacity_pool_id,definition_digest=source.digest,
                request_digest=spec.canonical_hash,prepared_digest=canonical_digest(prepared.cube_query),
                scope_digest=authorization_scope_digest(decision),decision_digest=canonical_digest(decision),
                binding_digest=binding.digest,model_bundle_digest=binding.model_bundle_digest,catalog_digest=None,
                max_submissions=2,max_bytes_per_submission=1000,max_total_bytes=2000)
            context=await lifecycle._materialize(uow,group=group,owner_id=execution.id,case_id=execution.id,
                spec=spec,principal=actor,actor=actor,binding=binding,prepared=prepared,executed=decision,current=fresh,
                plan=plan,generation=1,token=token,deadline=execution.deadline_at)
            request=await policy.source_access.resolve_owner(uow,{'kind':'execution','id':execution.id},actor,fresh,
                action='execute',request_id=str(uuid4()))
            await policy.source_access.finalize(uow,(request,))
        with psycopg.connect(**connection_kwargs(url)) as runtime:
            wire=context.wire_context()
            job=runtime.execute('SELECT gateway.source_bq_admit(%s,%s,%s)',(Jsonb(wire),'e'*64,Jsonb(intent()))).fetchone()[0]['job']
            runtime.execute('SELECT gateway.source_bq_observe(%s,%s)',(job['job_id'],Jsonb(dict(
                intent_digest=job['intent_digest'],state='done',terminal_error=False))))
            runtime.execute('SELECT gateway.source_seal_attempt(%s,%s)',(Jsonb(wire),'completed'))
        assert await lifecycle.close_group(group.group_id,'completed')=={'closed':True,'settled':True}

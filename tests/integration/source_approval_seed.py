"""Explicit synthetic review plus real app fixture/publication provenance.

No trigger bypass, historical backfill, credential configuration or provider HTTP.
Every retained fixture session reaches an admitted exact terminal metadata proof.
"""
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4
import secrets
from datetime import datetime,timezone

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from sqlalchemy import create_engine

from gateway.adapters.base import authorization_scope_digest
from gateway.contracts.canonical import canonical_bytes,canonical_digest
from gateway.contracts.execution import PinnedProduct
from gateway.contracts.identity import Decision
from gateway.contracts.manifest import Fixture
from gateway.contracts.query import QueryRequest,validate_query
from gateway.publishing.test_runner import ApprovedFixture,BigQueryFixtureCatalog,DomoFixtureCatalog,FixtureCheck,FixtureEvidence,execution_contract_digest
from gateway.source_lifecycle import provision_sources
from tests.integration.conftest import connection_kwargs
from tests.integration.test_source_approval_registry import install
from tests.integration.test_source_fixture_owners import fixture_owner_request
from tests.unit.test_source_approval_contracts import principal


def _insert(conn,table,values):
    conn.execute(sql.SQL('INSERT INTO gateway.{} ({}) VALUES ({})').format(sql.Identifier(table),
        sql.SQL(',').join(map(sql.Identifier,values)),sql.SQL(',').join(sql.Placeholder() for _ in values)),tuple(values.values()))


def _prelock(conn,manifest,draft_id):
    conn.execute("SELECT 1 FROM gateway.capacity_locks WHERE scope_id='deployment' FOR UPDATE")
    conn.execute('SELECT 1 FROM gateway.products WHERE id=%s FOR UPDATE',(manifest.product_id,))
    conn.execute('SELECT 1 FROM gateway.draft_revisions WHERE id=%s FOR UPDATE',(draft_id,))
    conn.execute('SELECT 1 FROM gateway.policies WHERE id=%s FOR UPDATE',(manifest.policy_id,))


def _allow(conn,request,manifest,version_id,correlation_id):
    event=str(uuid4());actor=request['principal']
    _insert(conn,'audit_events',dict(id=event,actor_id=actor['id'],service_client_id=actor['service_client_id'],
        action=request['action'],product_id=manifest.product_id,version_id=version_id,policy_id=manifest.policy_id,
        policy_revision=request['decision']['policy_revision'],decision='allow',correlation_id=correlation_id,
        source_approval_id=request['approval_id'],source_approval_revision=request['approval_revision'],
        source_approval_digest=request['approval_digest'],source_approval_state_revision=1,source_access_reason_code='APPROVED'))
    return event


@contextmanager
def fixture_provider(db,source,*,deployment_epoch):
    """Borrow exact preprovisioned fixture roles, or use disposable test roles."""
    names=('fixture_runtime','fixture_observer') if source.provider=='bigquery' else ('fixture_runtime',)
    created=[]
    try:
        if not all(name in db.urls and name in db.roles for name in names):
            if any(name in db.urls or name in db.roles for name in names):
                raise ValueError('Partial fixture provider mapping')
            with psycopg.connect(**connection_kwargs(db.admin_url),autocommit=True) as admin:
                for kind in names:
                    name='gws_'+secrets.token_hex(8);password=secrets.token_urlsafe(32)
                    admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}').format(sql.Identifier(name),sql.Literal(password)))
                    admin.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(db.urls['owner'].database),sql.Identifier(name)))
                    db.roles[kind]=name;db.urls[kind]=db.urls['app'].set(username=name,password=password);created.append((kind,name))
        engine=create_engine(db.urls['owner'])
        try:
            with engine.begin() as conn:
                provision_sources(conn,[source],[dict(login_name=db.roles[name],source_id=source.source_id,revision=source.revision,
                    context='fixture_test',role_kind='observer' if name=='fixture_observer' else 'runtime') for name in names],
                    deployment_epoch=deployment_epoch)
        finally:engine.dispose()
        yield
    finally:
        if created:
            with psycopg.connect(**connection_kwargs(db.admin_url.set(database=db.urls['owner'].database)),autocommit=True) as admin:
                for _,name in created:admin.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(name)))
            with psycopg.connect(**connection_kwargs(db.admin_url),autocommit=True) as admin:
                for _,name in created:admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(name)))
            for kind,_ in created:db.roles.pop(kind);db.urls.pop(kind)


def _reviewed_catalog(db,document,manifest,bindings,sources,actor):
    """Build complete fixed case review from explicit current synthetic grants."""
    positive_id=getattr(db,'fixture_fixed_principal',None) or 'seed-positive-'+uuid4().hex
    negative_id='seed-negative-'+uuid4().hex
    for name in (positive_id,negative_id):
        db.execute_owner('INSERT INTO gateway.principals(id,subject,kind) VALUES(%s,%s,\'demo\')',(name,name))
    db.execute_owner('''INSERT INTO gateway.principal_grants(principal_id,policy_id,tenant_id,roles,entitlements)
      SELECT %s,policy_id,tenant_id,'["consumer"]',entitlements FROM gateway.principal_grants
      WHERE principal_id=%s AND policy_id=%s AND active''',(positive_id,actor.id,manifest.policy_id))
    cases=[ApprovedFixture(fixture=fixture,principal_id=positive_id,
        kind='input_negative' if fixture.expected_error=='INVALID_INPUT' else 'known_answer') for fixture in manifest.fixtures]
    positive=next(case for case in cases if case.kind=='known_answer')
    cases.append(ApprovedFixture(fixture=Fixture(id='seed-permission-negative',version='1',inputs=dict(positive.fixture.inputs),
        expected_error='NOT_FOUND_OR_FORBIDDEN'),principal_id=negative_id,kind='permission_negative'))
    catalog_type=BigQueryFixtureCatalog if sources[0].provider=='bigquery' else DomoFixtureCatalog
    catalog=catalog_type(product_id=manifest.product_id,consumer_binding=bindings[0],fixture_binding=bindings[1],
        consumer_source=sources[0],fixture_source=sources[1],execution_contract_digest=execution_contract_digest(manifest),cases=cases)
    doc=deepcopy(document);doc['artifacts']['fixture_catalog_digest']=catalog.digest
    author=db.scalar("SELECT gateway.source_current_decision(%s,%s,'author')",(Jsonb(actor.model_dump(mode='json')),Jsonb(manifest.model_dump(mode='json'))))
    fixed=db.scalar('SELECT gateway.source_fixture_principal(%s)',(positive_id,))
    data=db.scalar("SELECT gateway.source_current_decision(%s,%s,'execute')",(Jsonb(fixed),Jsonb(manifest.model_dump(mode='json'))))
    def entry(id,identity,decision,context,actions,case_ids=()):
        return dict(id=id,context=context,principal_id=identity['id'],principal_kind=identity['kind'],client_ids=[identity['service_client_id']],
            actions=list(actions),tenant_id=decision['tenant_id'],row_constraints=decision['row_constraints'],
            visible_fields=decision['visible_fields'],fixture_case_ids=list(case_ids))
    doc['audiences'].append(entry('seed-publisher',actor.model_dump(mode='json'),author,'consumer',('author','publish')))
    doc['audiences'].append(entry('seed-invoker',actor.model_dump(mode='json'),author,'fixture_test',('author',)))
    for case in cases:
        identity=db.scalar('SELECT gateway.source_fixture_principal(%s)',(case.principal_id,))
        doc['audiences'].append(entry('seed-'+case.fixture.id,identity,data,'fixture_test',('execute','result'),(case.fixture.id,)))
    return doc,catalog


def seed_reviewed_publication(db,document,manifest,bindings,sources,*,approve=True,deployment_epoch='synthetic-epoch',
        draft_id='seed-fixture-draft',version_id='version-1',actor=None,existing_draft=None):
    """Return the exact original review and all retained fixture provenance IDs."""
    if existing_draft is not None:
        if (type(existing_draft) is not tuple or len(existing_draft) != 2
                or type(existing_draft[0]) is not str or not existing_draft[0]
                or type(existing_draft[1]) is not int or existing_draft[1] < 1):
            raise ValueError('Exact existing synthetic draft identity required')
        draft_id, revision = existing_draft
        with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
            row = conn.execute("""SELECT d.product_id,d.revision,d.manifest_canonical,d.content_digest,p.latest_draft_id,p.draft_revision
                FROM gateway.draft_revisions d JOIN gateway.products p ON p.id=d.product_id
                WHERE d.id=%s FOR UPDATE OF p,d""", (draft_id,)).fetchone()
            expected = (manifest.product_id, revision,
                canonical_bytes(manifest.model_dump(mode='python',exclude={'content_digest'})).decode(),
                manifest.content_digest, draft_id, revision)
            if row != expected:
                raise ValueError('Existing synthetic draft pins or current head differ')
    actor=actor or principal();document,catalog=_reviewed_catalog(db,document,manifest,bindings,sources,actor)
    if existing_draft is not None:
        # The already-authorized draft retains its separate metadata reviewer.
        # This new review pins only fixture authority for that invoking actor,
        # rather than relying on duplicate consumer tuples to deny a match.
        document['audiences']=[entry|{'actions':[action for action in entry['actions'] if action not in ('author','publish')]}
            if (entry['context']=='consumer' and entry['principal_id']==actor.id
                and entry['principal_kind']==actor.kind and actor.service_client_id in entry['client_ids']) else entry
            for entry in document['audiences']]
        document['audiences']=[entry for entry in document['audiences'] if entry['actions']]
    reviewed=deepcopy(document)
    if not approve:
        reviewed['audiences']=[entry|{'actions':[action for action in entry['actions'] if action not in ('execute','result')]}
            if entry['context']=='consumer' else entry for entry in reviewed['audiences']]
        reviewed['audiences']=[entry for entry in reviewed['audiences'] if entry['actions']]
        assert not any(entry['context']=='consumer' and set(entry['actions'])&{'execute','result'} for entry in reviewed['audiences'])
    approval=install(db,reviewed)
    binding_rows=[db.scalar('SELECT id FROM gateway.binding_revisions WHERE binding_id=%s AND revision=%s',(binding.binding_id,binding.revision)) for binding in bindings]
    if existing_draft is None:
        with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
            conn.execute('INSERT INTO gateway.products(id) VALUES(%s) ON CONFLICT DO NOTHING',(manifest.product_id,))
            revision=conn.execute('SELECT draft_revision FROM gateway.products WHERE id=%s FOR UPDATE',(manifest.product_id,)).fetchone()[0]+1
            _insert(conn,'draft_revisions',dict(id=draft_id,product_id=manifest.product_id,revision=revision,
                manifest=Jsonb(manifest.model_dump(mode='json')),manifest_canonical=canonical_bytes(manifest.model_dump(mode='python',exclude={'content_digest'})).decode(),
                content_digest=manifest.content_digest,actor_id=actor.id))
            conn.execute('UPDATE gateway.products SET latest_draft_id=%s,draft_revision=%s WHERE id=%s',(draft_id,revision,manifest.product_id))
    with fixture_provider(db,sources[1],deployment_epoch=deployment_epoch):
        result=_publish_fixture(db,document,manifest,catalog,approval,actor,binding_rows,draft_id,revision,version_id,deployment_epoch)
    result.document=document;result.approval=approval if approve else None;result.publication_approval=approval
    result.manifest=manifest;result.catalog=catalog;result.bindings=bindings;result.sources=sources
    result.product=PinnedProduct(manifest=manifest,binding_digest=bindings[0].digest,version_id=version_id)
    result.draft_id=draft_id;result.version_id=version_id
    assert db.scalar('SELECT source_operation_id IS NOT NULL FROM gateway.product_versions WHERE id=%s',(version_id,))
    return result


def _publish_fixture(db,document,manifest,catalog,approval,actor,binding_rows,draft_id,revision,version_id,epoch):
    source=catalog.fixture_source;run_id=str(uuid4());group_id=str(uuid4());sessions=[];jobs=[];attempts=[];checks=[];canonicals={}
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        invocation=conn.execute('SELECT gateway.source_accept_fixture_invocation(%s,%s,%s,%s)',
            (draft_id,Jsonb(actor.model_dump(mode='json')),canonical_bytes(catalog.model_dump(mode='python')).decode(),run_id)).fetchone()[0]
    actor_spec=dict(principal_id=actor.id,actor_id=actor.id,policy_id=manifest.policy_id,version_id=None,draft_id=draft_id,catalog_digest=catalog.digest)
    positives=[case for case in catalog.cases if case.kind=='known_answer']
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        _prelock(conn,manifest,draft_id);request=fixture_owner_request(conn,'fixture_invocation',run_id,'author')
        snapshot=conn.execute('SELECT gateway.source_owner_snapshot(%s,%s,%s)',('fixture_invocation',run_id,Jsonb(actor_spec))).fetchone()[0]
        audit=_allow(conn,request,manifest,draft_id,run_id)
        _insert(conn,'source_operation_groups',dict(id=group_id,owner_kind='fixture_test',owner_id=run_id,actor_id=actor.id,
            source_id=source.source_id,source_revision=source.revision,context='fixture_test',capacity_key=source.capacity_pool_id,
            deployment_epoch=epoch,deadline_at=invocation['effective_deadline_at'],roster=Jsonb(snapshot['source_access']['positive_roster']),
            authority_spec=Jsonb(actor_spec),authority_snapshot=Jsonb(snapshot),allow_audit_id=audit,
            max_submissions=(source.max_jobs_per_execution if source.provider=='bigquery' else 1)*len(positives),
            max_total_bytes=source.total_execution_bytes*len(positives) if source.provider=='bigquery' else None))
        conn.execute('SELECT gateway.source_finalize_access(%s)',(Jsonb([request]),))
    for case in catalog.cases:
        fixed=db.scalar('SELECT gateway.source_fixture_principal(%s)',(case.principal_id,))
        decision=db.scalar("SELECT gateway.source_current_decision_or_denial(%s,%s,'execute')",(Jsonb(fixed),Jsonb(manifest.model_dump(mode='json'))))
        attempt_id=None;digest=None
        if case.kind=='permission_negative':
            assert decision is None;error='NOT_FOUND_OR_FORBIDDEN'
        elif case.kind=='input_negative':
            assert decision is not None
            from gateway.contracts.errors import GatewayError
            try:validate_query(manifest,QueryRequest(product_id=manifest.product_id,version=manifest.version,inputs=case.fixture.inputs))
            except GatewayError:pass
            else:raise AssertionError('Synthetic input-negative case was not invalid')
            error='INVALID_INPUT';digest=canonical_digest(decision);canonicals[case.fixture.id]=canonical_bytes(decision).decode()
        else:
            assert decision is not None;error=None;attempt_id=str(uuid4());attempts.append(attempt_id);digest=canonical_digest(decision)
            query=validate_query(manifest,QueryRequest(product_id=manifest.product_id,version=manifest.version,inputs=case.fixture.inputs)).model_dump(mode='json',exclude={'idempotency_key'})
            derived=catalog.execution_manifest(manifest);token=str(uuid4());request_id=str(uuid4());session_id=str(uuid4());sessions.append(session_id)
            with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
                _prelock(conn,manifest,draft_id)
                values=dict(id=attempt_id,product_id=manifest.product_id,draft_id=draft_id,draft_digest=manifest.content_digest,
                    consumer_binding_id=binding_rows[0],consumer_binding_digest=catalog.consumer_binding.digest,
                    fixture_binding_id=binding_rows[1],fixture_binding_digest=catalog.fixture_binding.digest,
                    fixture_manifest=Jsonb(derived.model_dump(mode='json')),fixture_manifest_digest=derived.content_digest,
                    fixture_manifest_canonical=canonical_bytes(derived.model_dump(mode='python',exclude={'content_digest'})).decode(),
                    execution_contract_digest=catalog.execution_contract_digest,policy_id=manifest.policy_id,policy_revision=decision['policy_revision'],
                    catalog_digest=catalog.digest,fixture_id=case.fixture.id,fixture_version=case.fixture.version,actor_id=actor.id,
                    fixed_principal_id=fixed['id'],principal_snapshot=Jsonb(fixed),decision=Jsonb(decision),decision_canonical=canonical_bytes(decision).decode(),
                    canonical_query=Jsonb(query),query_canonical=canonical_bytes(query).decode(),canonical_hash=canonical_digest(query),
                    runner_token=token,request_id=request_id,source_invocation_id=run_id,deadline_at=conn.execute('SELECT clock_timestamp()').fetchone()[0])
                _insert(conn,'fixture_attempts',values);_insert(conn,'fixture_attempt_states',dict(attempt_id=attempt_id,state='running',source_completed=False))
                request=fixture_owner_request(conn,'fixture_attempt',attempt_id,'execute');original=fixture_owner_request(conn,'fixture_invocation',run_id,'author')
                spec=actor_spec|dict(principal_id=fixed['id'])
                snapshot,stamp=conn.execute("WITH s AS MATERIALIZED(SELECT gateway.source_owner_snapshot('fixture_attempt',%s,%s) v) SELECT v,encode(sha256(convert_to(v::text,'UTF8')),'hex') FROM s",(attempt_id,Jsonb(spec))).fetchone()
                audit=_allow(conn,request,manifest,draft_id,request_id)
                prepared=None
                if source.provider=='domo':
                    from gateway.adapters.domo_runtime import compile_domo_query
                    prepared=compile_domo_query(catalog.fixture_binding,
                        PinnedProduct(manifest=derived,binding_digest=catalog.fixture_binding.digest,version_id=draft_id),
                        QueryRequest.model_validate(query),Decision.model_validate(decision),db.fixture_statement_catalog,
                        source=source,request_id=request_id)
                plan=dict(provider=source.provider,source_id=source.source_id,source_revision=source.revision,context='fixture_test',capacity_key=source.capacity_pool_id,
                    definition_digest=source.digest,request_digest=canonical_digest(query),prepared_digest=prepared.digest if prepared else '9'*64,
                    scope_digest=authorization_scope_digest(Decision.model_validate(decision)),decision_digest=digest,binding_digest=catalog.fixture_binding.digest,
                    model_bundle_digest=catalog.fixture_binding.model_bundle_digest,catalog_digest=catalog.digest,max_submissions=source.max_jobs_per_execution if source.provider=='bigquery' else 1,
                    max_bytes_per_submission=source.per_job_maximum_bytes_billed if source.provider=='bigquery' else None,
                    max_total_bytes=source.total_execution_bytes if source.provider=='bigquery' else None)
                context_digest=canonical_digest(dict(group_id=group_id,session_id=session_id,plan=plan,authority_stamp=stamp))
                deadline=invocation['effective_deadline_at']
                _insert(conn,'source_sessions',dict(id=session_id,group_id=group_id,case_id=case.fixture.id,owner_id=attempt_id,
                    source_id=source.source_id,source_revision=source.revision,context='fixture_test',capacity_key=source.capacity_pool_id,
                    plan=Jsonb(plan),provider_nonce=secrets.token_urlsafe(32),authority_spec=Jsonb(spec),authority_snapshot=Jsonb(snapshot),
                    authority_stamp=stamp,context_digest=context_digest,allow_audit_id=audit,generation=1,lease_token=token,deadline_at=deadline))
                if prepared is not None:
                    _insert(conn,'source_preparations',dict(owner_kind='fixture_test',owner_id=attempt_id,session_id=session_id,
                        prepared_query=Jsonb(prepared.model_dump(mode='json')),prepared_canonical=canonical_bytes(prepared).decode(),prepared_digest=prepared.digest))
                conn.execute('SELECT gateway.source_finalize_access(%s)',(Jsonb([original,request]),))
            context=plan|dict(group_id=group_id,session_id=session_id,generation=1,authority_stamp=stamp,context_digest=context_digest,deadline_at=deadline)
            from tests.integration.test_source_lifecycle import call,intent
            if source.provider=='bigquery':
                remaining_ms=int((datetime.fromisoformat(deadline.replace('Z','+00:00'))-db.scalar('SELECT clock_timestamp()')).total_seconds()*1000)
                assert remaining_ms>20, 'The explicit fixture authority expired before its synthetic terminal proof'
                request_intent=intent(timeout=min(source.job_timeout_ms,5000,remaining_ms//2))
                request_intent['maximum_bytes_billed']=str(source.per_job_maximum_bytes_billed)
                job=call(db,'fixture_runtime','SELECT gateway.source_bq_admit(%s,%s,%s)',context,'e'*64,request_intent)['job'];jobs.append(job['job_id'])
                call(db,'fixture_runtime','SELECT gateway.source_bq_observe(%s,%s)',job['job_id'],dict(intent_digest=job['intent_digest'],state='done',terminal_error=False))
            else:
                import hashlib
                from tests.integration.test_source_approval_lifecycle import domo_intent
                nonce=secrets.token_urlsafe(32);intent_value=domo_intent(prepared)
                armed=call(db,'fixture_runtime','SELECT gateway.source_domo_arm_send(%s,%s,%s,%s)',context,prepared.digest,
                    hashlib.sha256(nonce.encode()).hexdigest(),intent_value)
                assert armed['armed'] is True
                call(db,'fixture_runtime','SELECT gateway.source_domo_observe(%s,%s,%s)',session_id,nonce,
                    dict(plan_fingerprint=prepared.digest,response_profile_digest=intent_value['response_profile_digest'],
                        response_sha256='e'*64,status_code=200,media_type='application/json',
                        completed_at=db.scalar('SELECT clock_timestamp()').astimezone(timezone.utc).isoformat(),row_count=len(case.fixture.expected_rows),response_bytes=100))
            call(db,'fixture_runtime','SELECT gateway.source_seal_attempt(%s,%s)',context,'completed')
        observed=(db.scalar('SELECT clock_timestamp()').astimezone(timezone.utc).isoformat() if case.kind=='known_answer' and getattr(catalog.fixture_binding,'freshness_mode',None)=='same-query-checkpoint' else None)
        checks.append(FixtureCheck(fixture_id=case.fixture.id,kind=case.kind,passed=True,expected=case.fixture.expected_rows,
            actual=case.fixture.expected_rows if case.kind=='known_answer' else (),expected_error=case.fixture.expected_error,actual_error=error,
            absolute_tolerance=case.fixture.tolerance,relative_tolerance=case.relative_tolerance,attempt_id=attempt_id,decision_digest=digest,
            reference_watermark=case.fixture.source_watermark,observed_source_load_watermark=observed,
            freshness_status=('fresh' if observed else 'unknown') if case.kind=='known_answer' else None))
    from tests.integration.test_source_lifecycle import call
    call(db,'app','SELECT gateway.source_close_group(%s,%s)',group_id,'completed')
    evidence=FixtureEvidence(catalog_digest=catalog.digest,execution_contract_digest=catalog.execution_contract_digest,
        fixture_binding_digest=catalog.fixture_binding.digest,fixture_model_bundle_digest=catalog.fixture_binding.model_bundle_digest,checks=checks)
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        _prelock(conn,manifest,draft_id)
        ordered=sorted((check for check in checks if check.attempt_id),key=lambda check:check.attempt_id)
        for check in ordered:
            conn.execute('SELECT 1 FROM gateway.fixture_attempt_states WHERE attempt_id=%s FOR UPDATE',(check.attempt_id,))
        author_request=fixture_owner_request(conn,'fixture_invocation',run_id,'author')
        for check in ordered:
            conn.execute("UPDATE gateway.fixture_attempt_states SET state='succeeded',source_completed=true,check_digest=%s WHERE attempt_id=%s",(canonical_digest(check),check.attempt_id))
        _insert(conn,'test_runs',dict(id=run_id,product_id=manifest.product_id,draft_id=draft_id,draft_digest=manifest.content_digest,
            binding_id=binding_rows[0],binding_digest=catalog.consumer_binding.digest,policy_id=manifest.policy_id,policy_revision=author_request['decision']['policy_revision'],
            fixture_version=catalog.digest,evidence=Jsonb(evidence.model_dump(mode='json')),evidence_canonical=canonical_bytes(evidence).decode(),
            source_decision_canonicals=Jsonb(canonicals),status='passed',actor_id=actor.id))
        requests=[fixture_owner_request(conn,'fixture_invocation',run_id,'author')]+[fixture_owner_request(conn,'fixture_attempt',id,'result') for id in attempts]
        for request in requests:_allow(conn,request,manifest,draft_id,str(uuid4()))
        conn.execute('SELECT gateway.source_finalize_access(%s)',(Jsonb(requests),))
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        _prelock(conn,manifest,draft_id)
        request_id=str(uuid4())
        # Test-owner read only; the private selector remains unavailable to the
        # app. The app recorder independently reconstructs and validates it.
        current=db.scalar("SELECT gateway.source_transient_access(%s,%s,'publish')",
            (canonical_bytes(manifest.model_dump(mode='python',exclude={'content_digest'})).decode(),
                Jsonb(actor.model_dump(mode='json'))))
        author=current['decision'];projection=current['projection']
        request=dict(principal=actor.model_dump(mode='json'),action='publish',decision=author,
            approval_id=projection['approval_id'],approval_revision=projection['approval_revision'],approval_digest=projection['approval_digest'])
        audit=_allow(conn,request,manifest,draft_id,request_id)
        head=conn.execute('SELECT current_version_id,publication_revision FROM gateway.products WHERE id=%s',(manifest.product_id,)).fetchone()
        operation=conn.execute('SELECT gateway.source_record_publication_operation(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (Jsonb(actor.model_dump(mode='json')),'publish',manifest.product_id,version_id,draft_id,run_id,head[0],head[1],revision,audit,request_id)).fetchone()[0]
        _insert(conn,'product_versions',dict(id=version_id,product_id=manifest.product_id,version=manifest.version,draft_id=draft_id,
            manifest=Jsonb(manifest.model_dump(mode='json')),manifest_canonical=canonical_bytes(manifest.model_dump(mode='python',exclude={'content_digest'})).decode(),
            content_digest=manifest.content_digest,binding_id=binding_rows[0],binding_digest=catalog.consumer_binding.digest,policy_id=manifest.policy_id,
            policy_revision=author['policy_revision'],test_run_id=run_id,actor_id=actor.id,name=manifest.name,domain=manifest.domain,
            classification=manifest.classification,source_operation_id=operation))
        _insert(conn,'version_states',dict(version_id=version_id,status='published',actor_id=actor.id,reason='Explicit synthetic review',source_operation_id=operation))
        conn.execute('UPDATE gateway.products SET current_version_id=%s,source_operation_id=%s WHERE id=%s',(version_id,operation,manifest.product_id))
        _insert(conn,'publication_events',dict(id=str(uuid4()),product_id=manifest.product_id,version_id=version_id,action='publish',actor_id=actor.id,
            reason='Explicit synthetic review',request_id=request_id,source_operation_id=operation))
    return SimpleNamespace(invocation_id=run_id,group_id=group_id,session_ids=tuple(sessions),job_ids=tuple(jobs),attempt_ids=tuple(attempts),operation_id=str(operation))


def source_database_assets(db,raw,*,per_principal_limit=None):
    """Review the existing test source exactly, with a distinct fixture source."""
    from gateway.adapters.bigquery_runtime import BigQuerySource,BigQueryBinding
    from gateway.source_access_contracts import physical_source_identity_digest,source_credential_binding_digest
    from tests.unit.test_source_approval_contracts import package
    from tests.unit.test_policy import rules
    from tests.contract.test_cube_adapter import synthetic
    raw=deepcopy(raw);raw.update(adapter_revision='cube-bigquery-v1',model_bundle_digest=db.source.model_bundle_digest,content_digest=None)
    raw['freshness']['required']=False
    doc,manifest=package.__wrapped__(raw)
    for domain in doc['input_domains']:
        if domain['type']=='date':domain.update(minimum='2024-01-01',maximum='2025-01-03')
    content={'rules':rules(**({'limits':{'per_principal_concurrency':per_principal_limit}} if per_principal_limit else {})),
        'reason':'Explicit synthetic source lifecycle review'}
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as conn:
        conn.execute('INSERT INTO gateway.policies(id,current_revision) VALUES(%s,1)',(manifest.policy_id,))
        _insert(conn,'policy_revisions',dict(id='policy-1',policy_id=manifest.policy_id,revision=1,content=Jsonb(content),content_digest=canonical_digest(content),actor_id='operator'))
        _insert(conn,'principals',dict(id='north-user',subject='synthetic-north',kind='user',service_client_id='test-client'))
        _insert(conn,'principal_grants',dict(principal_id='north-user',policy_id=manifest.policy_id,tenant_id='north',
            roles=Jsonb(['consumer','author','publisher']),entitlements=Jsonb({'groups':['north']})))
    original=synthetic()[0].model_dump(mode='json');original.pop('constraint_members');original.pop('watermark_member')
    fixture_raw=db.source.model_dump(mode='json');fixture_raw.update(source_id='isolated-lifecycle-fixture',context='fixture_test',
        model_bundle_digest='1'*64 if db.source.model_bundle_digest!='1'*64 else '2'*64,
        data_project='isolated-fixture-project',dataset='isolated_fixture_dataset',cube_endpoint='https://isolated-fixture.synthetic.svc.cluster.local')
    fixture_raw['auth']['expected_principal']='isolated-fixture@billing-project.iam.gserviceaccount.com'
    fixture=BigQuerySource.model_validate(fixture_raw)
    sources=[db.source,fixture];bindings=[]
    for kind,source in zip(('consumer','fixture'),sources):
        binding=BigQueryBinding.model_validate(original|dict(context=source.context,binding_id=doc['bindings'][kind]['binding_id'],
            adapter_revision='cube-bigquery-v1',source_id=source.source_id,source_revision=source.revision,source_digest=source.digest,
            model_bundle_digest=source.model_bundle_digest,scope={'tenant_member':'Orders.tenant_id','access_group_member':'Orders.access_group'},
            freshness_mode='unknown',checkpoint_member=None))
        if kind=='fixture':
            db.execute_owner('INSERT INTO gateway.source_profiles(source_id,revision,context,provider,capacity_key,descriptor_digest,descriptor) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                (source.source_id,source.revision,source.context,source.provider,source.capacity_pool_id,source.digest,Jsonb(source.model_dump(mode='json'))))
        else:
            assert db.scalar('SELECT descriptor_digest FROM gateway.source_profiles WHERE source_id=%s AND revision=%s AND context=%s',
                (source.source_id,source.revision,source.context))==source.digest
        with psycopg.connect(**connection_kwargs(db.urls['owner'])) as conn:
            _insert(conn,'binding_revisions',dict(id=kind+'-binding-row',binding_id=binding.binding_id,revision=binding.revision,
                content=Jsonb(binding.model_dump(mode='json')),content_digest=binding.digest,model_bundle_digest=binding.model_bundle_digest,
                adapter_revision=binding.adapter_revision,actor_id='operator'))
        doc['bindings'][kind]=dict(binding_id=binding.binding_id,revision=binding.revision,content_digest=binding.digest)
        doc['sources'][kind]=dict(source_id=source.source_id,revision=source.revision,context=source.context,provider=source.provider,
            descriptor_digest=source.digest,physical_identity_digest=physical_source_identity_digest(source),credential_binding_digest=source_credential_binding_digest(source))
        bindings.append(binding)
    doc['artifacts']['fixture_model_bundle_digest']=fixture.model_bundle_digest
    doc['lineage']=sum((db.scalar('SELECT gateway.source_approval_binding_inventory(%s,%s)',
        (Jsonb(binding.model_dump(mode='json')),Jsonb(doc['input_domains']))) for binding in bindings),[])
    return doc,manifest,bindings,sources

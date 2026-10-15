"""Genuine PostgreSQL accepted-owner approval checks; synthetic records only."""
import hashlib
import pytest
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.postgres


def test_consumer_companion_is_protected_and_marker_not_app_controlled(registry_database):
    db = registry_database
    assert db.scalar("SELECT to_regclass('gateway.execution_source_authorities') IS NOT NULL")
    for role in ('app', 'source_reader', 'audit_retention'):
        assert not db.scalar('SELECT has_table_privilege(%s,%s,%s)',
            (db.roles[role], 'gateway.execution_source_authorities', 'INSERT,UPDATE,DELETE'))
        assert not db.scalar('SELECT has_column_privilege(%s,%s,%s,%s)',
            (db.roles[role], 'gateway.executions', 'source_access_generation', 'INSERT,UPDATE'))


def test_canonical_text_witness_preserves_rfc8785_floats_and_rejects_duplicates(registry_database):
    from gateway.contracts.canonical import canonical_bytes
    db=registry_database
    value={'n':1e-7,'array':[1.5,1e21,-0.0]}
    raw=canonical_bytes(value).decode()
    digest=hashlib.sha256(raw.encode()).hexdigest()
    assert db.scalar('SELECT gateway.source_json_witness(%s,%s,%s)', (raw,Jsonb(value),digest)) is True
    for bad in ('{"n":1,"n":2}', '{"nested":{"n":1,"n":1}}'):
        assert db.scalar('SELECT gateway.source_json_witness(%s,%s,%s)',
            (bad,Jsonb(__import__('json').loads(bad)),hashlib.sha256(bad.encode()).hexdigest())) is False
    assert db.scalar('SELECT gateway.source_json_witness(%s,%s,%s)', (raw,Jsonb(value),'a'*64)) is False
    assert db.scalar('SELECT gateway.source_json_witness(NULL,NULL,NULL)') is False


def consumer_setup(db, valid_manifest, *, expires=None, approve=True, assets_factory=None, deployment_epoch='synthetic-epoch'):
    """Create explicit review and genuine fixture/publication provenance."""
    from copy import deepcopy
    from tests.integration.source_approval_seed import seed_reviewed_publication
    from tests.integration.test_source_fixture_owners import isolated_approval_assets
    raw=deepcopy(valid_manifest)
    if assets_factory is None:
        # The default synthetic BQ binding has no checkpoint; approve that exact
        # optional-freshness manifest rather than exempting a required check.
        raw['freshness']['required']=False
    doc,manifest,bindings,sources=(assets_factory or isolated_approval_assets)(db,raw)
    if expires:doc['expires_at']=expires
    seeded=seed_reviewed_publication(db,doc,manifest,bindings,sources,approve=approve,deployment_epoch=deployment_epoch)
    db.publication_seed=seeded
    return seeded.document,manifest,seeded.approval,seeded.product


def insert_consumer(conn, manifest, *, exid='execution-1', client='test-client', insert_authority=True, query=None, change_decision=None, probe=False, decision_override=None):
    from gateway.contracts.canonical import canonical_bytes,canonical_digest
    from gateway.contracts.identity import Decision
    from gateway.adapters.base import authorization_scope_digest
    from tests.unit.test_source_approval_contracts import principal
    actor=principal().model_copy(update={'service_client_id':client}).model_dump(mode='json')
    from gateway.policy.service import derive_decision,PolicyRules
    from gateway.auth.principals import PrincipalGrants
    from gateway.contracts.execution import PinnedProduct
    from tests.unit.test_policy import grant,rules
    decision=(derive_decision(PrincipalGrants(principal_id=actor['id'],grants=[grant()]),'execute',
        PinnedProduct(manifest=manifest,binding_digest='a'*64,version_id='version-1'),PolicyRules.model_validate(rules()),1)
        if decision_override is None else Decision.model_validate(decision_override)).model_dump(mode='json')
    if change_decision: decision.update(change_decision)
    query=query or {'product_id':manifest.product_id,'version':manifest.version,'inputs':{'start':'2024-03-10','end':'2024-03-11','segment':None},'filters':[],'sort':[]}
    conn.execute('''INSERT INTO gateway.executions(id,principal_id,product_id,version_id,policy_id,policy_revision,
      canonical_query,query_canonical,canonical_hash,context,request_id,deadline_at,execution_timeout_seconds)
      VALUES(%s,'north-user',%s,'version-1',%s,1,%s,%s,%s,'consumer','request-1',clock_timestamp(),120)''',
      (exid,manifest.product_id,manifest.policy_id,Jsonb(query),canonical_bytes(query).decode(),canonical_digest(query)))
    conn.execute("INSERT INTO gateway.execution_states(execution_id,state) VALUES(%s,'accepted')",(exid,))
    if probe:
        assert conn.execute("SELECT gateway.source_query_reviewed(content,%s,%s) FROM gateway.source_access_approvals",(Jsonb(manifest.model_dump(mode='json')),Jsonb(query))).fetchone()[0] is True, 'query review'
        assert conn.execute("SELECT gateway.source_execution_package(%s,id) FROM gateway.source_access_approvals",(exid,)).fetchone()[0] is True, 'package review'
        assert conn.execute("SELECT gateway.source_complete_audience(content,%s,%s,'execute') FROM gateway.source_access_approvals",(Jsonb(actor),Jsonb(decision))).fetchone()[0]=='north-consumer', 'audience review'
    if insert_authority:
        conn.execute('''INSERT INTO gateway.execution_authorizations(execution_id,principal_snapshot,decision,scope_digest,full_decision_digest)
          VALUES(%s,%s,%s,%s,%s)''',(exid,Jsonb(actor),Jsonb(decision),authorization_scope_digest(Decision.model_validate(decision)),canonical_digest(decision)))
    return actor,decision


def test_execution_acceptance_retains_exact_original_approval_and_client(registry_database,valid_manifest):
    import psycopg
    from tests.integration.conftest import connection_kwargs
    db=registry_database;doc,manifest,receipt,product=consumer_setup(db,valid_manifest)
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as conn:
        actor,decision=insert_consumer(conn,manifest,probe=True)
    retained=db.scalar("SELECT to_jsonb(a) FROM gateway.execution_source_authorities a WHERE execution_id='execution-1'")
    assert retained['approval_id']==receipt['id'] and retained['approval_digest']==receipt['content_digest']
    assert retained['principal_snapshot']==actor
    assert db.scalar("SELECT source_access_generation FROM gateway.executions WHERE id='execution-1'")==1
    assert db.scalar("SELECT a.effective_deadline_at=least(e.deadline_at,a.accepted_approval_expires_at) FROM gateway.execution_source_authorities a JOIN gateway.executions e ON e.id=a.execution_id") is True
    assert db.scalar("SELECT gateway.source_assert_owner_access('execution','execution-1','result')->'decision'")==decision
    for sql in ("UPDATE gateway.execution_source_authorities SET audience_entry_id='changed'", "DELETE FROM gateway.execution_source_authorities"):
        assert db.denied_as_owner(sql)=='permission_denied'
    db.scalar('SELECT gateway.source_approval_revoke(%s,%s,%s)',(receipt['id'],receipt['content_digest'],'synthetic-ended'))
    assert db.denied_as_app("SELECT gateway.source_assert_owner_access('execution','execution-1','result')")=='permission_denied'
    assert db.scalar('SELECT count(*) FROM gateway.execution_source_authorities')==1


@pytest.mark.parametrize('mutation',['missing_auth','missing_approval','wrong_client','wrong_inputs','forged_decision','ambiguous'])
def test_acceptance_denies_atomically(registry_database,valid_manifest,mutation):
    import psycopg,copy
    from tests.integration.conftest import connection_kwargs
    from tests.integration.test_source_approval_registry import install
    db=registry_database;doc,manifest,receipt,_=consumer_setup(db,valid_manifest,approve=mutation!='missing_approval')
    if mutation=='ambiguous':
        other=copy.deepcopy(doc);other['review']['review_id']='other-review';install(db,other)
    options={}
    if mutation=='missing_auth':options['insert_authority']=False
    if mutation=='wrong_client':options['client']='different-client'
    if mutation=='wrong_inputs': options['query']={'product_id':manifest.product_id,'version':manifest.version,'inputs':{'start':'2024-03-10','end':'2024-03-11','segment':'unreviewed'},'filters':[],'sort':[]}
    if mutation=='forged_decision':options['change_decision']={'visible_fields':['day']}
    with pytest.raises(psycopg.Error):
        with psycopg.connect(**connection_kwargs(db.urls['owner'])) as conn:
            insert_consumer(conn,manifest,**options)
    assert db.scalar('SELECT count(*) FROM gateway.executions')==0
    assert db.scalar('SELECT count(*) FROM gateway.execution_source_authorities')==0


def test_replacement_approval_never_rebinds_accepted_owner(registry_database,valid_manifest):
    import copy,psycopg
    from tests.integration.conftest import connection_kwargs
    from tests.integration.test_source_approval_registry import install
    db=registry_database;doc,manifest,original,_=consumer_setup(db,valid_manifest)
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as conn: insert_consumer(conn,manifest)
    db.scalar('SELECT gateway.source_approval_revoke(%s,%s,%s)',(original['id'],original['content_digest'],'synthetic-ended'))
    replacement=copy.deepcopy(doc);replacement.update(revision=2,supersedes_id=original['id'])
    successor=install(db,replacement)
    assert successor['id']!=original['id']
    assert db.denied_as_app("SELECT gateway.source_assert_owner_access('execution','execution-1','result')")=='permission_denied'
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as conn: insert_consumer(conn,manifest,exid='execution-2')
    assert db.scalar("SELECT approval_id::text FROM gateway.execution_source_authorities WHERE execution_id='execution-1'")==original['id']
    assert db.scalar("SELECT approval_id::text FROM gateway.execution_source_authorities WHERE execution_id='execution-2'")==successor['id']


def test_expiry_is_rechecked_after_approval_lock_wait(registry_database,valid_manifest):
    import psycopg
    from concurrent.futures import ThreadPoolExecutor
    from tests.integration.conftest import connection_kwargs
    db=registry_database
    expiry=db.scalar("SELECT clock_timestamp()+interval '2 seconds'")
    doc,manifest,receipt,_=consumer_setup(db,valid_manifest,expires=expiry.astimezone(__import__('datetime').timezone.utc).isoformat().replace('+00:00','Z'))
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as conn: insert_consumer(conn,manifest)
    before=db.scalar('SELECT content FROM gateway.source_access_approvals')
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as blocker, ThreadPoolExecutor() as pool:
        blocker.execute('SELECT 1 FROM gateway.source_access_approval_states WHERE approval_id=%s FOR UPDATE',(receipt['id'],))
        check=pool.submit(db.denied_as_app,"SELECT gateway.source_assert_owner_access('execution','execution-1','result')")
        blocker.execute('SELECT pg_sleep(greatest(0,extract(epoch FROM (%s::timestamptz-clock_timestamp()))+0.02))',(expiry,))
        blocker.commit()
        assert check.result(timeout=3)=='permission_denied'
    assert db.scalar('SELECT content FROM gateway.source_access_approvals')==before
    assert db.scalar('SELECT count(*) FROM gateway.execution_source_authorities')==1


def test_deferred_acceptance_expiry_aborts_owner_and_companion(registry_database,valid_manifest):
    import psycopg
    from tests.integration.conftest import connection_kwargs
    db=registry_database
    expiry=db.scalar("SELECT clock_timestamp()+interval '1.5 seconds'")
    _,manifest,_,_=consumer_setup(db,valid_manifest,expires=expiry.astimezone(__import__('datetime').timezone.utc).isoformat().replace('+00:00','Z'))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with psycopg.connect(**connection_kwargs(db.urls['owner'])) as conn:
            insert_consumer(conn,manifest)
            conn.execute('SELECT pg_sleep(greatest(0,extract(epoch FROM (%s::timestamptz-clock_timestamp()))+0.02))',(expiry,))
    assert db.scalar('SELECT count(*) FROM gateway.executions')==0
    assert db.scalar('SELECT count(*) FROM gateway.execution_source_authorities')==0


def test_original_execution_duration_does_not_expire_result_approval(registry_database,valid_manifest):
    import psycopg
    from tests.integration.conftest import connection_kwargs
    db=registry_database
    valid_manifest['limits']={'execution_timeout_seconds':1,'sync_wait_seconds':0}
    _,manifest,_,_=consumer_setup(db,valid_manifest)
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as conn: insert_consumer(conn,manifest)
    db.scalar("SELECT pg_sleep(greatest(0,extract(epoch FROM (deadline_at-clock_timestamp()))+0.02)) FROM gateway.executions")
    assert db.denied_as_app("SELECT gateway.source_assert_owner_access('execution','execution-1','execute')")=='permission_denied'
    assert db.scalar("SELECT gateway.source_assert_owner_access('execution','execution-1','result')->'accepted' IS NOT NULL") is True


@pytest.mark.parametrize('mutation',['unknown_sort','bad_direction','duplicate_sort','too_many_sort','backwards_dates','equal_dates','long_dates','too_many_filter_values','duplicate_filter_values','missing_default','unknown_input'])
def test_protected_acceptance_enforces_full_query_shape(registry_database,valid_manifest,mutation):
    import psycopg
    from tests.integration.conftest import connection_kwargs
    if mutation=='long_dates':valid_manifest['limits']={'max_date_range_days':3}
    if mutation=='too_many_filter_values':valid_manifest['filters']['segment']['max_values']=1
    if mutation=='too_many_sort':valid_manifest['sort']['max_fields']=1
    db=registry_database;_,manifest,_,_=consumer_setup(db,valid_manifest)
    query={'product_id':manifest.product_id,'version':manifest.version,'inputs':{'start':'2024-03-10','end':'2024-03-11','segment':None},'filters':[],'sort':[]}
    if mutation=='unknown_sort':query['sort']=[{'field':'private_field','direction':'asc'}]
    if mutation=='bad_direction':query['sort']=[{'field':'day','direction':'sideways'}]
    if mutation=='duplicate_sort':query['sort']=[{'field':'day','direction':'asc'}]*2
    if mutation=='too_many_sort':query['sort']=[{'field':v,'direction':'asc'} for v in ['day','segment']]
    if mutation=='backwards_dates':query['inputs'].update(start='2024-03-11',end='2024-03-10')
    if mutation=='equal_dates':query['inputs'].update(start='2024-03-10',end='2024-03-10')
    if mutation=='long_dates':query['inputs'].update(start='2024-03-10',end='2024-03-14')
    if mutation=='duplicate_filter_values':query['filters']=[{'field':'segment','operator':'in','value':['A','A']}]
    if mutation=='too_many_filter_values':query['filters']=[{'field':'segment','operator':'in','value':['A','B']}]
    if mutation=='missing_default':del query['inputs']['segment']
    if mutation=='unknown_input':query['inputs']['unexpected']='A'
    with pytest.raises(psycopg.Error):
        with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:insert_consumer(conn,manifest,query=query)
    assert db.scalar('SELECT count(*) FROM gateway.executions')==0


@pytest.mark.parametrize('expanded',[False,True])
def test_valid_app_role_acceptance_reaches_companion_boundary(registry_database,valid_manifest,expanded):
    import psycopg
    from tests.integration.conftest import connection_kwargs
    db=registry_database;_,manifest,receipt,_=consumer_setup(db,valid_manifest)
    query={'product_id':manifest.product_id,'version':manifest.version,'inputs':{'start':'2024-03-10','end':'2024-03-11','segment':None},'filters':[],'sort':[]}
    if expanded:
        query['filters']=[{'field':'segment','operator':'in','value':['A','B']}]
        query['sort']=[{'field':'day','direction':'desc'},{'field':'segment','direction':'asc'}]
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:insert_consumer(conn,manifest,query=query)
    assert db.scalar('SELECT count(*) FROM gateway.executions')==1
    assert db.scalar('SELECT approval_id::text FROM gateway.execution_source_authorities')==receipt['id']


def test_loading_retained_pins_does_not_grant_or_require_a_result_action(registry_database,valid_manifest):
    import asyncio,psycopg
    from gateway.source_access import load_execution_access
    from tests.integration.conftest import connection_kwargs
    from tests.integration.test_source_approval_registry import install
    db=registry_database;doc,manifest,_,_=consumer_setup(db,valid_manifest,approve=False)
    doc['audiences'][0]['actions']=['execute'];installed=install(db,doc)
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:insert_consumer(conn,manifest)
    async def load():
        async with db.registry.transaction() as uow:return await load_execution_access(uow,'execution-1')
    assert asyncio.run(load()).approval_id==installed['id']
    assert db.denied_as_app("SELECT gateway.source_assert_owner_access('execution','execution-1','result')")=='permission_denied'
    db.scalar('SELECT gateway.source_approval_revoke(%s,%s,%s)',(installed['id'],installed['content_digest'],'synthetic-ended'))
    assert asyncio.run(load()).approval_id==installed['id']
    assert db.denied_as_app("SELECT gateway.source_assert_owner_access('execution','execution-1','execute')")=='permission_denied'


@pytest.mark.parametrize('malformation',['duplicate_roles','invalid_unused_entitlement'])
def test_protected_derivation_rejects_malformed_current_grant(registry_database,valid_manifest,malformation):
    import psycopg
    from tests.integration.conftest import connection_kwargs
    db=registry_database;_,manifest,_,_=consumer_setup(db,valid_manifest)
    if malformation=='duplicate_roles':
        db.execute_owner("UPDATE gateway.principal_grants SET roles='[\"consumer\",\"consumer\"]'::jsonb WHERE principal_id='north-user'")
    else:
        db.execute_owner("UPDATE gateway.principal_grants SET entitlements=entitlements||'{\"unused\":[]}'::jsonb WHERE principal_id='north-user'")
    with pytest.raises(psycopg.Error):
        with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:insert_consumer(conn,manifest)
    assert db.scalar('SELECT count(*) FROM gateway.executions')==0


@pytest.mark.parametrize('malformation',['duplicate_fields','unknown_limit','incoherent_limit','constraint_extra','entitlement_separator'])
def test_protected_current_decision_rejects_invalid_policy_shape(registry_database,valid_manifest,malformation):
    import psycopg
    from gateway.contracts.canonical import canonical_digest
    from gateway.policy.service import PolicyContent
    from tests.integration.conftest import connection_kwargs
    from tests.unit.test_source_approval_contracts import principal
    db=registry_database;_,manifest,_,_=consumer_setup(db,valid_manifest)
    content=db.scalar('SELECT content FROM gateway.policy_revisions WHERE revision=1')
    if malformation=='duplicate_fields':content['rules']['visible_fields']=['day','day']
    if malformation=='unknown_limit':content['rules']['limits']={'unexpected':1}
    if malformation=='incoherent_limit':content['rules']['limits']={'max_rows':1,'page_size':200}
    if malformation=='constraint_extra':content['rules']['tenant_constraint']['unexpected']='value'
    if malformation=='entitlement_separator':content['rules']['entitlement_constraint']['source']='grantXentitlementsYaccess_groups'
    with pytest.raises(ValueError):PolicyContent.model_validate(content)
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as conn:
        conn.execute("INSERT INTO gateway.policy_revisions(id,policy_id,revision,content,content_digest,actor_id) VALUES('policy-2',%s,2,%s,%s,'operator')",
            (manifest.policy_id,Jsonb(content),canonical_digest(content)))
        conn.execute('UPDATE gateway.policies SET current_revision=2 WHERE id=%s',(manifest.policy_id,))
    assert db.denied_as_owner("SELECT gateway.source_current_decision(%s,%s,'execute')",
        (Jsonb(principal().model_dump(mode='json')),Jsonb(manifest.model_dump(mode='json'))))=='permission_denied'


@pytest.mark.parametrize('case',['positive','valid_unused_entitlement','no_grant','no_role','no_entitlement','empty_entitlement','duplicate_entitlement','bad_unused_entitlement','no_grant_bad_policy','no_role_bad_product_limits','missing_policy','inactive_identity'])
def test_structural_decision_proves_only_genuine_policy_denial(registry_database,valid_manifest,case):
    import psycopg
    from gateway.contracts.canonical import canonical_digest
    from tests.integration.conftest import connection_kwargs
    from tests.unit.test_source_approval_contracts import principal
    db=registry_database;_,manifest,_,_=consumer_setup(db,valid_manifest)
    value=manifest.model_dump(mode='json');actor=principal().model_dump(mode='json')
    if case in ('no_grant','no_grant_bad_policy'):
        db.execute_owner("UPDATE gateway.principal_grants SET active=false WHERE principal_id='north-user'")
    if case in ('no_role','no_role_bad_product_limits'):
        db.execute_owner("UPDATE gateway.principal_grants SET roles='[\"author\"]' WHERE principal_id='north-user'")
    if case=='valid_unused_entitlement':db.execute_owner("UPDATE gateway.principal_grants SET entitlements=entitlements||%s WHERE principal_id='north-user'",(Jsonb({'unused':[1.25,'arbitrary valid scalar ☺']}),))
    if case=='no_entitlement':db.execute_owner("UPDATE gateway.principal_grants SET entitlements='{}' WHERE principal_id='north-user'")
    if case=='empty_entitlement':db.execute_owner("UPDATE gateway.principal_grants SET entitlements='{\"groups\":[]}' WHERE principal_id='north-user'")
    if case=='duplicate_entitlement':db.execute_owner("UPDATE gateway.principal_grants SET roles='[]',entitlements='{\"groups\":[\"north\",\"north\"]}' WHERE principal_id='north-user'")
    if case=='bad_unused_entitlement':db.execute_owner("UPDATE gateway.principal_grants SET roles='[]',entitlements='{\"groups\":[\"north\"],\"unused\":[]}' WHERE principal_id='north-user'")
    if case=='no_role_bad_product_limits':value['limits']['unexpected']=1
    if case=='missing_policy':value['policy_id']='missing-policy'
    if case=='inactive_identity':db.execute_owner("UPDATE gateway.principals SET active=false WHERE id='north-user'")
    if case=='no_grant_bad_policy':
        content=db.scalar('SELECT content FROM gateway.policy_revisions WHERE revision=1')
        content['rules']['limits']={'page_size':200,'max_rows':1}
        with psycopg.connect(**connection_kwargs(db.urls['owner'])) as conn:
            conn.execute("INSERT INTO gateway.policy_revisions(id,policy_id,revision,content,content_digest,actor_id) VALUES('policy-2',%s,2,%s,%s,'operator')",(manifest.policy_id,Jsonb(content),canonical_digest(content)))
            conn.execute('UPDATE gateway.policies SET current_revision=2 WHERE id=%s',(manifest.policy_id,))
    arguments=(Jsonb(actor),Jsonb(value))
    sql="SELECT gateway.source_current_decision_or_denial(%s,%s,'execute')"
    if case in ('positive','valid_unused_entitlement'):
        result=db.scalar(sql,arguments)
        assert set(result)=={'principal_id','policy_revision','tenant_id','row_constraints','visible_fields','limits'}
        assert result==db.scalar("SELECT gateway.source_current_decision(%s,%s,'execute')",arguments)
    elif case in ('no_grant','no_role','no_entitlement'):
        assert db.scalar(sql,arguments) is None
        assert db.denied_as_owner("SELECT gateway.source_current_decision(%s,%s,'execute')",arguments)=='permission_denied'
    else:
        assert db.denied_as_owner(sql,arguments)=='permission_denied'
    assert db.scalar('SELECT count(*) FROM gateway.executions')==0
    assert db.scalar("SELECT count(*) FROM gateway.source_operation_groups WHERE context='consumer'")==0
    assert db.scalar('SELECT closed AND settled_at IS NOT NULL FROM gateway.source_operation_groups WHERE id=%s',(db.publication_seed.group_id,))
    assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE decision='deny'")==0
    assert db.denied_as_app(sql,arguments)=='permission_denied'


def test_audit_pruner_has_only_exact_retention_role_and_bounded_clock(registry_database):
    db=registry_database
    signature='gateway.prune_unreferenced_audits(timestamptz,integer)'
    for role in ('app','source_reader'):
        assert not db.scalar('SELECT has_function_privilege(%s,%s,\'EXECUTE\')',(db.roles[role],signature))
    assert db.scalar('SELECT has_function_privilege(%s,%s,\'EXECUTE\')',(db.roles['audit_retention'],signature))
    assert db.denied_as_owner("SELECT gateway.prune_unreferenced_audits(clock_timestamp(),1)")=='permission_denied'
    for parameters in ((None,1),('infinity',1),('-infinity',1),('2020-01-01',None),('2020-01-01',0),('2020-01-01',1001)):
        assert db.denied_as_retention('SELECT gateway.prune_unreferenced_audits(%s,%s)',parameters)=='permission_denied'
    for name in ('ix_source_groups_allow_audit','ix_source_sessions_allow_audit'):
        assert db.scalar('SELECT to_regclass(%s) IS NOT NULL',('gateway.'+name,))
    for table in ('source_operation_groups','source_sessions','source_access_approvals','source_publication_operations'):
        assert db.denied_as_retention('SELECT * FROM gateway.'+table)=='permission_denied'


def test_audit_pruner_progresses_in_bounded_batches_and_clamps_future_cutoff(registry_database):
    db=registry_database
    db.execute_owner("""INSERT INTO gateway.audit_events(id,created_at,actor_id,action,decision,correlation_id)
      SELECT 'old-'||n,clock_timestamp()-interval '31 days','synthetic','execute','deny','old-'||n FROM generate_series(1,1002) n""")
    db.execute_owner("""INSERT INTO gateway.audit_events(id,created_at,actor_id,action,decision,correlation_id)
      VALUES('young',clock_timestamp()-interval '29 days','synthetic','execute','deny','young')""")
    prune=lambda:db._execute('audit_retention',"SELECT gateway.prune_unreferenced_audits(clock_timestamp()+interval '365 days',1000)",scalar=True)
    assert prune()==1000 and prune()==2 and prune()==0
    assert db.scalar('SELECT array_agg(id) FROM gateway.audit_events')==['young']


def test_genuine_seed_retains_unsorted_multi_case_terminal_provenance(registry_database,valid_manifest):
    from copy import deepcopy
    db=registry_database
    first=deepcopy(valid_manifest['fixtures'][0]);first['id']='z-last'
    second=deepcopy(first);second['id']='a-first'
    valid_manifest['fixtures']=[first,second]
    _,_,_,_=consumer_setup(db,valid_manifest)
    seed=db.publication_seed
    assert [case.fixture.id for case in seed.catalog.cases[:2]]==['z-last','a-first']
    assert db.scalar('SELECT roster FROM gateway.source_operation_groups WHERE id=%s',(seed.group_id,))==['a-first','z-last']
    assert len(seed.attempt_ids)==len(seed.session_ids)==len(seed.job_ids)==2
    assert db.scalar('SELECT count(*) FROM gateway.source_sessions WHERE group_id=%s AND sealed',(seed.group_id,))==2
    assert db.scalar('SELECT count(*) FROM gateway.bigquery_source_jobs WHERE job_id=ANY(%s) AND state=\'done\'',(list(seed.job_ids),))==2
    assert db.scalar('SELECT closed AND settled_at IS NOT NULL FROM gateway.source_operation_groups WHERE id=%s',(seed.group_id,))
    assert db.scalar('SELECT count(*) FROM gateway.executions')==0


def test_audit_pruner_progresses_past_retained_groups_and_session_references(registry_database,valid_manifest):
    """Age only synthetic audits; every owner/group/session guard stays active."""
    import psycopg
    from uuid import uuid4
    from tests.integration.conftest import connection_kwargs
    from tests.integration.source_approval_seed import _insert
    db=registry_database;_,manifest,approval,_=consumer_setup(db,valid_manifest)
    seed=db.publication_seed;source=seed.sources[0];binding=seed.bindings[0]
    fixture_audits=db.scalar('SELECT array_agg(id ORDER BY id) FROM gateway.audit_events')
    publication=db.scalar('SELECT to_jsonb(p) FROM gateway.source_publication_operations p WHERE id=%s',(seed.operation_id,))
    spec=dict(principal_id='north-user',actor_id='north-user',policy_id=manifest.policy_id,
        version_id='version-1',draft_id=None,catalog_digest=None)
    # The retained fixture already occupies one of the 1,000 group slots.
    # Separate session audits bring 999 consumer groups to 1,001 protected
    # audit references without changing any source or concurrency cap.
    retained=[];old_sessions=[]
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as app, psycopg.connect(**connection_kwargs(db.urls['owner'])) as owner:
        for index in range(999):
            eid='retention-owner-'+str(index);gid=str(uuid4());audit='retained-audit-'+str(index);token=str(uuid4())
            with app.transaction():insert_consumer(app,manifest,exid=eid)
            with owner.transaction():
                owner.execute("SELECT 1 FROM gateway.capacity_locks WHERE scope_id='deployment' FOR UPDATE")
                owner.execute('SELECT 1 FROM gateway.products WHERE id=%s FOR UPDATE',(manifest.product_id,))
                owner.execute('SELECT 1 FROM gateway.policies WHERE id=%s FOR UPDATE',(manifest.policy_id,))
                owner.execute("SELECT 1 FROM gateway.version_states WHERE version_id='version-1' FOR UPDATE")
                owner.execute('SELECT 1 FROM gateway.execution_states WHERE execution_id=%s FOR UPDATE',(eid,))
                if index<2:
                    owner.execute("UPDATE gateway.execution_states SET state='running',started_at=clock_timestamp() WHERE execution_id=%s",(eid,))
                    owner.execute("INSERT INTO gateway.execution_leases(execution_id,worker_id,lease_token,generation,expires_at,source_reserved_until) SELECT id,'retention',%s,1,clock_timestamp()+interval '30 seconds',deadline_at FROM gateway.executions WHERE id=%s",(token,eid))
                owner.execute('SELECT 1 FROM gateway.source_pools WHERE capacity_key=%s FOR UPDATE',(source.capacity_pool_id,))
                snapshot=owner.execute("SELECT gateway.source_owner_snapshot('execution',%s,%s)",(eid,Jsonb(spec))).fetchone()[0]
                deadline=snapshot['source_access']['effective_deadline_at']
                _insert(owner,'audit_events',dict(id=audit,created_at=owner.execute("SELECT clock_timestamp()-interval '40 days'").fetchone()[0],
                    actor_id='north-user',service_client_id='test-client',action='execute',product_id=manifest.product_id,version_id='version-1',
                    policy_id=manifest.policy_id,policy_revision=1,decision='allow',correlation_id='request-1',source_approval_id=approval['id'],
                    source_approval_revision=approval['revision'],source_approval_digest=approval['content_digest'],
                    source_approval_state_revision=1,source_access_reason_code='APPROVED'))
                _insert(owner,'source_operation_groups',dict(id=gid,owner_kind='consumer',owner_id=eid,actor_id='north-user',
                    source_id=source.source_id,source_revision=source.revision,context='consumer',capacity_key=source.capacity_pool_id,
                    deployment_epoch='synthetic-epoch',deadline_at=deadline,roster=Jsonb([eid]),authority_spec=Jsonb(spec),
                    authority_snapshot=Jsonb(snapshot),allow_audit_id=audit,max_submissions=source.max_jobs_per_execution,max_total_bytes=source.total_execution_bytes))
                if index<2:
                    old_session=str(uuid4());old_sessions.append(old_session)
                    session_audit='retained-session-audit-'+str(index)
                    owner.execute("""INSERT INTO gateway.audit_events(id,created_at,actor_id,service_client_id,action,product_id,version_id,policy_id,policy_revision,decision,correlation_id,source_approval_id,source_approval_revision,source_approval_digest,source_approval_state_revision,source_access_reason_code)
                        SELECT %s,created_at,actor_id,service_client_id,action,product_id,version_id,policy_id,policy_revision,decision,correlation_id,source_approval_id,source_approval_revision,source_approval_digest,source_approval_state_revision,source_access_reason_code
                        FROM gateway.audit_events WHERE id=%s""",(session_audit,audit))
                    retained.append(session_audit)
                    canonical_hash=owner.execute('SELECT canonical_hash FROM gateway.executions WHERE id=%s',(eid,)).fetchone()[0]
                    scope,decision=owner.execute('SELECT scope_digest,full_decision_digest FROM gateway.execution_authorizations WHERE execution_id=%s',(eid,)).fetchone()
                    plan=dict(provider='bigquery',source_id=source.source_id,source_revision=source.revision,context='consumer',
                        capacity_key=source.capacity_pool_id,definition_digest=source.digest,request_digest=canonical_hash,prepared_digest='9'*64,
                        scope_digest=scope,decision_digest=decision,binding_digest=binding.digest,model_bundle_digest=binding.model_bundle_digest,
                        catalog_digest=None,max_submissions=source.max_jobs_per_execution,max_bytes_per_submission=source.per_job_maximum_bytes_billed,
                        max_total_bytes=source.total_execution_bytes)
                    stamp=owner.execute("SELECT encode(sha256(convert_to(%s::jsonb::text,'UTF8')),'hex')",(Jsonb(snapshot),)).fetchone()[0]
                    _insert(owner,'source_sessions',dict(id=old_session,group_id=gid,case_id=eid,owner_id=eid,source_id=source.source_id,
                        source_revision=source.revision,context='consumer',capacity_key=source.capacity_pool_id,plan=Jsonb(plan),provider_nonce=__import__('secrets').token_urlsafe(32),
                        authority_spec=Jsonb(spec),authority_snapshot=Jsonb(snapshot),authority_stamp=stamp,context_digest='e'*64,
                        allow_audit_id=session_audit,generation=1,lease_token=token,deadline_at=deadline))
            with app.transaction():
                assert app.execute("SELECT gateway.source_close_group(%s,'completed')",(gid,)).fetchone()[0]=={'closed':True,'settled':True}
            retained.append(audit)
    assert db.scalar("SELECT count(*) FROM gateway.source_operation_groups WHERE context='consumer' AND closed AND settled_at IS NOT NULL")==999
    assert len(retained)==1001
    assert db.scalar('SELECT count(*) FROM gateway.source_sessions WHERE id=ANY(%s) AND sealed',(old_sessions,))==2
    assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==0
    db.execute_owner("""INSERT INTO gateway.audit_events(id,created_at,actor_id,action,decision,correlation_id)
        SELECT 'eligible-'||n,clock_timestamp()-interval '31 days','synthetic','execute','deny','eligible-'||n FROM generate_series(1,1002) n""")
    # Revocation must not make retained provenance deletable or block maintenance.
    db.scalar('SELECT gateway.source_approval_revoke(%s,%s,%s)',(approval['id'],approval['content_digest'],'retention-review-ended'))
    assert db.denied_as_retention('DELETE FROM gateway.audit_events WHERE id=%s',(retained[0],))=='23503'
    query="""SELECT a.id FROM gateway.audit_events a WHERE a.created_at<=clock_timestamp()-interval '30 days'
        AND NOT EXISTS(SELECT 1 FROM gateway.source_operation_groups g WHERE g.allow_audit_id=a.id)
        AND NOT EXISTS(SELECT 1 FROM gateway.source_sessions s WHERE s.allow_audit_id=a.id)
        ORDER BY a.created_at,a.id LIMIT 1000"""
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as owner:
        owner.execute('ANALYZE gateway.audit_events; ANALYZE gateway.source_operation_groups; ANALYZE gateway.source_sessions')
        # This explicit planner control checks the two FK anti-probe indexes are
        # usable; the real pruner below runs with its normal planner settings.
        owner.execute('SET LOCAL enable_seqscan=off; SET LOCAL enable_hashjoin=off; SET LOCAL enable_mergejoin=off')
        plan=owner.execute('EXPLAIN (ANALYZE,FORMAT JSON) '+query).fetchone()[0][0]['Plan']
        assert plan['Node Type']=='Limit' and plan['Actual Rows']==1000
        serialized=str(plan)
        assert 'ix_source_groups_allow_audit' in serialized and 'ix_source_sessions_allow_audit' in serialized
    prune=lambda:db._execute('audit_retention','SELECT gateway.prune_unreferenced_audits(clock_timestamp(),1000)',scalar=True)
    assert [prune(),prune(),prune()]==[1000,2,0]
    assert db.scalar('SELECT count(*) FROM gateway.audit_events WHERE id=ANY(%s)',(retained,))==1001
    assert db.scalar('SELECT count(*) FROM gateway.source_publication_operations')==1
    assert db.scalar('SELECT to_jsonb(p) FROM gateway.source_publication_operations p WHERE id=%s',(seed.operation_id,))==publication
    assert db.scalar('SELECT count(*) FROM gateway.audit_events WHERE id=ANY(%s)',(fixture_audits,))==len(fixture_audits)
    assert db.scalar('SELECT count(*) FROM gateway.source_operation_groups')==1000
    assert db.scalar('SELECT count(*) FROM gateway.source_sessions')==len(seed.session_ids)+2

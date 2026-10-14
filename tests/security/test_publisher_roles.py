"""Real current-policy checks on author, reviewer, and publisher operations."""
import asyncio
import pytest
from gateway.contracts.errors import GatewayError
from tests.integration.conftest import registry_database
from tests.integration.test_audit import principal
from tests.integration.test_publishing import setup

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('roles',[('consumer',),('publisher',)])
def test_non_author_cannot_save_draft(registry_database,roles):
    async def scenario():
        service,manifest=await setup(registry_database,roles=roles)
        with pytest.raises(GatewayError) as caught: await service.save_draft(principal(),manifest,0)
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert registry_database.scalar('SELECT count(*) FROM gateway.draft_revisions') == 0
    asyncio.run(scenario())


def test_author_without_publisher_cannot_publish_even_before_evidence_lookup(registry_database):
    async def scenario():
        service,manifest=await setup(registry_database,roles=('author',))
        draft=await service.save_draft(principal(),manifest,0)
        with pytest.raises(GatewayError) as caught:
            await service.publish(principal(),draft.id,draft.content_digest,1,'Reviewed')
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert 'binding' not in caught.value.safe_message.lower()
    asyncio.run(scenario())


def test_publisher_without_author_cannot_run_fixtures(registry_database):
    async def scenario():
        service,manifest=await setup(registry_database,roles=('author',))
        draft=await service.save_draft(principal(),manifest,0)
        registry_database.execute_owner("UPDATE gateway.principal_grants SET roles='[\"publisher\"]'::jsonb")
        with pytest.raises(GatewayError) as caught: await service.test(principal(),draft.id)
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
    asyncio.run(scenario())


def test_author_cannot_attach_another_policy_without_admin_on_both_scopes(registry_database):
    async def scenario():
        import psycopg
        from psycopg.types.json import Jsonb
        from tests.integration.conftest import connection_kwargs
        from tests.unit.test_policy import rules
        from gateway.contracts.canonical import canonical_digest
        from gateway.contracts.manifest import validate_manifest
        service,manifest=await setup(registry_database,roles=('author',))
        draft=await service.save_draft(principal(),manifest,0)
        content={'rules':rules(),'reason':'A separate reviewed policy'}
        with psycopg.connect(**connection_kwargs(registry_database.urls['owner'])) as connection:
            connection.execute("INSERT INTO gateway.policies(id,current_revision) VALUES ('broader-access',1)")
            connection.execute("INSERT INTO gateway.policy_revisions(id,policy_id,revision,content,content_digest,actor_id) VALUES ('broader-policy','broader-access',1,%s,%s,'operator')",(Jsonb(content),canonical_digest(content)))
            connection.execute("INSERT INTO gateway.principal_grants(principal_id,policy_id,tenant_id,roles,entitlements) VALUES ('north-user','broader-access','north','[\"author\"]','{}')")
        raw=manifest.model_dump(mode='json',exclude={'content_digest'});raw['policy_id']='broader-access'
        with pytest.raises(GatewayError) as caught:
            await service.save_draft(principal(),validate_manifest(raw),1)
        assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
        assert registry_database.scalar('SELECT draft_revision FROM gateway.products')==1
        assert registry_database.scalar('SELECT count(*) FROM gateway.draft_revisions')==1
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='policy_admin' AND decision='deny'")==1
    asyncio.run(scenario())


@pytest.mark.parametrize('change',['author_revoked','fixed_identity_missing','negative_identity_missing'])
def test_fixture_authority_cannot_turn_missing_identities_into_permission_success(registry_database,change):
    async def scenario():
        from tests.integration.test_publishing import fixture_setup
        service,manifest,_,_=await fixture_setup(registry_database)
        draft=await service.save_draft(principal(),manifest,0)
        if change=='author_revoked':
            registry_database.execute_owner("UPDATE gateway.principal_grants SET active=false WHERE principal_id='north-user'")
            with pytest.raises(GatewayError) as caught: await service.test(principal(),draft.id)
            assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
            assert registry_database.scalar('SELECT count(*) FROM gateway.fixture_attempts')==0
        else:
            identity='fixture-denied' if change=='negative_identity_missing' else 'fixture-north'
            registry_database.execute_owner('UPDATE gateway.principals SET active=false WHERE id=%s',(identity,))
            run=await service.test(principal(),draft.id)
            assert run.status=='failed'
            failed=[c for c in run.evidence['checks'] if not c['passed']]
            assert any(c['actual_error']=='BINDING_DRIFT' for c in failed)
            with pytest.raises(GatewayError) as caught:
                await service.publish(principal(),draft.id,draft.content_digest,1,'Missing fixture identity')
            assert caught.value.code=='CONFLICT'
    asyncio.run(scenario())


def test_permission_filtered_review_uses_each_current_policy_field_projection(registry_database):
    async def scenario():
        from tests.unit.test_policy import rules
        from tests.integration.test_publishing import fixture_setup
        service,manifest,_,_=await fixture_setup(registry_database,roles=('author','publisher','policy_admin'))
        draft=await service.save_draft(principal(),manifest,0)
        await service.policy.change_policy(principal(),manifest.policy_id,rules(visible_fields=['day','segment']),
            'Revenue measure is hidden from this review')
        review=await service.diff(principal(),draft.id)
        assert not any(c.path.startswith('/outputs/revenue') or c.path.startswith('/units/revenue') for c in review.changes)
        fixtures=next(c.after for c in review.changes if c.path=='/fixtures')
        assert all('revenue' not in row for fixture in fixtures for row in fixture['expected_rows'])
    asyncio.run(scenario())


def test_authors_cannot_put_principals_credentials_or_policy_overrides_in_fixtures(registry_database):
    async def scenario():
        from gateway.contracts.manifest import validate_manifest
        service,manifest=await setup(registry_database,roles=('author',))
        for field in ('principal_id','credentials','policy_override','source_url'):
            raw=manifest.model_dump(mode='json',exclude={'content_digest'})
            raw['fixtures'][0][field]='unapproved'
            with pytest.raises(GatewayError) as caught: validate_manifest(raw)
            assert caught.value.code=='INVALID_INPUT'
        assert registry_database.scalar('SELECT count(*) FROM gateway.fixture_attempts')==0
    asyncio.run(scenario())


def test_test_result_values_are_currently_field_filtered_but_stored_proof_is_complete(registry_database):
    async def scenario():
        from tests.unit.test_policy import rules
        from tests.integration.test_publishing import fixture_setup
        service,manifest,_,_=await fixture_setup(registry_database,roles=('author','publisher','policy_admin'))
        await service.policy.change_policy(principal(),manifest.policy_id,rules(visible_fields=['day','segment']),
            'Hide the revenue measure in authorized review evidence')
        draft=await service.save_draft(principal(),manifest,0)
        run=await service.test(principal(),draft.id)
        assert run.status=='failed'
        assert all('revenue' not in row for check in run.evidence['checks'] for name in ('expected','actual') for row in check[name])
        saved=registry_database.scalar('SELECT evidence FROM gateway.test_runs WHERE id=%s',(run.id,))
        assert any('revenue' in row for check in saved['checks'] for row in check['expected'])
        assert registry_database.denied_as_app("UPDATE gateway.test_runs SET evidence='{}' WHERE id=%s",(run.id,))=='permission_denied'
    asyncio.run(scenario())


def test_current_test_read_after_visibility_tightening_keeps_pins_and_hides_values(registry_database):
    async def scenario():
        from tests.unit.test_policy import rules
        from tests.integration.test_publishing import fixture_setup
        service,manifest,_,_=await fixture_setup(registry_database,roles=('author','publisher','policy_admin'))
        draft=await service.save_draft(principal(),manifest,0)
        run=await service.test(principal(),draft.id)
        assert run.status=='passed' and run.policy_revision==1
        await service.policy.change_policy(principal(),manifest.policy_id,rules(visible_fields=['day','segment']),
            'Tighten evidence review visibility after the run')
        view=await service.get_test(principal(),draft.id,run.id)
        assert view is not run and view.id==run.id and view.draft_digest==run.draft_digest
        assert view.policy_revision==1 and view.fixture_version==run.fixture_version and view.status=='passed'
        assert all('revenue' not in row for check in view.evidence['checks'] for name in ('expected','actual') for row in check[name])
        assert all(c['absolute_tolerance'] is None and c['relative_tolerance'] is None for c in view.evidence['checks'])
        stored=registry_database.scalar('SELECT evidence FROM gateway.test_runs WHERE id=%s',(run.id,))
        assert any('revenue' in row for check in stored['checks'] for row in check['actual'])
        assert all(c['absolute_tolerance'] is not None for c in stored['checks'])
        assert 'policy' in (await service.diff(principal(),draft.id)).stale_evidence
        with pytest.raises(GatewayError) as caught:
            await service.publish(principal(),draft.id,draft.content_digest,1,'Stale field visibility review')
        assert caught.value.code=='CONFLICT'
    asyncio.run(scenario())


def test_publisher_can_review_without_author_or_consumer_and_read_ids_stay_scoped(registry_database):
    async def scenario():
        from tests.integration.test_publishing import fixture_setup
        from gateway.contracts.manifest import validate_manifest
        service,manifest,_,_=await fixture_setup(registry_database)
        draft=await service.save_draft(principal(),manifest,0)
        run=await service.test(principal(),draft.id)
        raw=manifest.model_dump(mode='json',exclude={'content_digest'});raw['version']='1.1.0'
        another=await service.save_draft(principal(),validate_manifest(raw),1)
        registry_database.execute_owner("UPDATE gateway.principal_grants SET roles='[\"publisher\"]'::jsonb WHERE principal_id='north-user'")
        assert (await service.get_test(principal(),draft.id,run.id)).id==run.id
        for draft_id,run_id in ((another.id,run.id),(draft.id,'unknown-run')):
            with pytest.raises(GatewayError) as caught: await service.get_test(principal(),draft_id,run_id)
            assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
        with pytest.raises(GatewayError) as caught: await service.test(principal(),another.id)
        assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
        registry_database.execute_owner("UPDATE gateway.principal_grants SET active=false WHERE principal_id='north-user'")
        with pytest.raises(GatewayError) as caught: await service.get_test(principal(),draft.id,run.id)
        assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
        assert registry_database.scalar("SELECT roles FROM gateway.principal_grants WHERE principal_id='north-user'")==['publisher']
    asyncio.run(scenario())


def test_current_draft_read_exposes_editability_but_partial_visibility_cannot_replace(registry_database):
    async def scenario():
        from tests.unit.test_policy import rules
        from gateway.contracts.manifest import validate_manifest
        service,manifest=await setup(registry_database,roles=('author','publisher','policy_admin'))
        draft=await service.save_draft(principal(),manifest,0)
        full=await service.get_draft(principal(),draft.id)
        assert full.editable_full_definition and set(full.definition['outputs'])==set(manifest.outputs)
        await service.policy.change_policy(principal(),manifest.policy_id,rules(visible_fields=['day','segment']),
            'Restrict definition field visibility')
        partial=await service.get_draft(principal(),draft.id)
        assert not partial.editable_full_definition and 'revenue' not in partial.definition['outputs']
        raw=manifest.model_dump(mode='json',exclude={'content_digest'});raw['description']='A new explanation'
        changed=validate_manifest(raw)
        for operation in (lambda:service.update_draft(principal(),draft.id,changed,1),
                          lambda:service.save_draft(principal(),changed,1)):
            with pytest.raises(GatewayError) as caught: await operation()
            assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
        assert registry_database.scalar('SELECT count(*) FROM gateway.draft_revisions')==1
        registry_database.execute_owner("UPDATE gateway.principal_grants SET roles='[\"publisher\"]'::jsonb WHERE principal_id='north-user'")
        assert not (await service.get_draft(principal(),draft.id)).editable_full_definition
        registry_database.execute_owner("UPDATE gateway.principal_grants SET active=false WHERE principal_id='north-user'")
        with pytest.raises(GatewayError) as caught: await service.get_draft(principal(),draft.id)
        assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
    asyncio.run(scenario())


@pytest.mark.parametrize('scope',['author','publisher','mixed','unrelated','revoked','ambiguous'])
def test_review_current_tenant_scope_controls_all_reads_and_test_return(registry_database,scope):
    async def scenario():
        from tests.integration.test_publishing import fixture_setup
        from tests.unit.test_policy import rules
        service,manifest,_,_=await fixture_setup(registry_database,roles=('author','publisher','policy_admin'))
        draft=await service.save_draft(principal(),manifest,0)
        original=await service.test(principal(),draft.id)
        await service.policy.change_policy(principal(),manifest.policy_id,
            rules(tenant_ids=['north','south'] if scope=='ambiguous' else ['north'],visible_fields=['day','segment']),
            'Narrow current review scope')
        roles='["publisher"]' if scope=='publisher' else '["author"]'
        registry_database.execute_owner('UPDATE gateway.principal_grants SET roles=%s::jsonb WHERE principal_id=%s',
            (roles,principal().id))
        if scope in {'mixed','ambiguous','unrelated'}:
            registry_database.execute_owner("INSERT INTO gateway.principal_grants(principal_id,policy_id,tenant_id,roles,entitlements) VALUES ('north-user','revenue-access','south','[\"publisher\"]','{}')")
        if scope in {'revoked','unrelated'}:
            registry_database.execute_owner("UPDATE gateway.principal_grants SET active=false WHERE principal_id='north-user' AND tenant_id='north'")
        denied=scope in {'unrelated','revoked','ambiguous'}
        for operation,args in ((service.get_draft,(principal(),draft.id)),
                               (service.diff,(principal(),draft.id)),
                               (service.get_test,(principal(),draft.id,original.id))):
            if denied:
                with pytest.raises(GatewayError) as caught: await operation(*args)
                assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
            else:
                view=await operation(*args)
                assert view is not None
                if operation==service.get_draft:
                    assert 'revenue' not in view.definition['outputs']
        if scope=='mixed':
            returned=await service.test(principal(),draft.id)
            assert returned.id and returned.status=='failed'
            assert all('revenue' not in row for check in returned.evidence['checks'] for name in ('expected','actual') for row in check[name])
            assert (await service.diff(principal(),draft.id)).latest_test_run_id==returned.id
        if denied:
            assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE actor_id='north-user' AND decision='deny'")>=3
    asyncio.run(scenario())


def test_completed_fixture_with_revoked_final_authority_remains_quarantined(registry_database):
    async def scenario():
        from tests.integration.test_publishing import fixture_setup
        service,manifest,_,_=await fixture_setup(registry_database)
        original=service.fixture_runner.adapter_factory
        class RevokedAfterCompletion(original):
            async def poll(self,*args,**kwargs):
                completed=await super().poll(*args,**kwargs)
                if self.decision.principal_id=='fixture-north':
                    registry_database.execute_owner("UPDATE gateway.principal_grants SET active=false WHERE principal_id='fixture-north'")
                return completed
        service.fixture_runner.adapter_factory=RevokedAfterCompletion
        draft=await service.save_draft(principal(),manifest,0)
        run=await service.test(principal(),draft.id)
        assert run.status=='failed'
        assert registry_database.scalar("SELECT count(*) FROM gateway.fixture_source_reservations r JOIN gateway.fixture_attempts a ON a.id=r.attempt_id JOIN gateway.fixture_attempt_states s ON s.attempt_id=a.id WHERE a.fixed_principal_id='fixture-north' AND r.released_at IS NULL AND NOT s.source_completed AND s.state='failed'")==1
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE actor_id='fixture-north' AND action='result' AND decision='deny'")>=1
        assert any(c['actual_error']=='NOT_FOUND_OR_FORBIDDEN' for c in run.evidence['checks'])
    asyncio.run(scenario())


@pytest.mark.parametrize('new_scope_authorized',[True,False])
def test_conflict_reload_locator_never_discloses_an_unauthorized_current_policy(registry_database,new_scope_authorized):
    async def scenario():
        import psycopg
        from psycopg.types.json import Jsonb
        from tests.integration.conftest import connection_kwargs
        from tests.unit.test_policy import rules
        from gateway.contracts.canonical import canonical_digest
        from gateway.contracts.manifest import validate_manifest
        service,manifest=await setup(registry_database,roles=('author','publisher','policy_admin'))
        original=await service.save_draft(principal(),manifest,0)
        content={'rules':rules(),'reason':'Separate new draft scope'}
        with psycopg.connect(**connection_kwargs(registry_database.urls['owner'])) as connection:
            connection.execute("INSERT INTO gateway.policies(id,current_revision) VALUES ('new-access',1)")
            connection.execute("INSERT INTO gateway.policy_revisions(id,policy_id,revision,content,content_digest,actor_id) VALUES ('new-policy','new-access',1,%s,%s,'operator')",(Jsonb(content),canonical_digest(content)))
            connection.execute("INSERT INTO gateway.principal_grants(principal_id,policy_id,tenant_id,roles,entitlements) VALUES ('north-user','new-access','north','[\"author\",\"policy_admin\"]','{}')")
        raw=manifest.model_dump(mode='json',exclude={'content_digest'});raw.update(policy_id='new-access',version='1.1.0')
        latest=await service.update_draft(principal(),original.id,validate_manifest(raw),1)
        if not new_scope_authorized:
            registry_database.execute_owner("UPDATE gateway.principal_grants SET active=false WHERE principal_id='north-user' AND policy_id='new-access'")
        view=await service.get_draft(principal(),original.id)
        assert not view.is_current_revision and view.id==original.id and view.revision==1
        assert view.current_draft_id==(latest.id if new_scope_authorized else None)
        if new_scope_authorized:
            current=await service.get_draft(principal(),view.current_draft_id)
            assert current.is_current_revision and current.revision==2 and current.can_test and not current.can_publish
        else:
            assert latest.id not in str(view.model_dump())
            assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE actor_id='north-user' AND decision='deny' AND policy_id='new-access'")>=1
    asyncio.run(scenario())

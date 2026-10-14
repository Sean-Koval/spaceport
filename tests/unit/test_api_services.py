"""Pure projections/cursors and service sequencing; PostgreSQL evidence is separate."""
import asyncio
import importlib
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from contextlib import asynccontextmanager

import pytest
from gateway.contracts.manifest import validate_manifest
from gateway.contracts.execution import PinnedProduct
from gateway.contracts.identity import Decision
from gateway.contracts.errors import GatewayError
from gateway.adapters.base import CubeBinding
from tests.unit.test_policy import rules

pytestmark=pytest.mark.unit


def module(name):
    try:return importlib.import_module('gateway.api.'+name)
    except ModuleNotFoundError as error:pytest.fail(f'Shared API service is missing: {error.name}')


def binding(manifest):
    return CubeBinding(binding_id=manifest.binding_id,revision=1,adapter_revision='cube-rest-v1',
        model_bundle_digest=manifest.model_bundle_digest,
        fields={'day':{'member':'Orders.ordered_at','kind':'time','cube_type':'time','response_key':'Orders.ordered_at.day','granularity':'day'},
                'segment':{'member':'Orders.segment','kind':'dimension','cube_type':'string','response_key':'Orders.segment'},
                'revenue':{'member':'Orders.revenue','kind':'measure','cube_type':'number','response_key':'Orders.revenue'}},
        input_fields={'segment':'segment'},constraint_members=['Orders.tenant_id','Orders.access_group'],watermark_member='Watermark.loaded_at')


def decision(principal,fields):
    return Decision(principal_id=principal.id,policy_revision=1,tenant_id='north',row_constraints=[
        {'member':'Orders.tenant_id','operator':'eq','values':['north']},
        {'member':'Orders.access_group','operator':'in','values':['north']}],visible_fields=fields,limits={})


@pytest.mark.parametrize('fields',[['day','revenue'],['segment'],['revenue'],['day','segment','revenue']])
def test_projection_omits_hidden_dependencies_prose_examples_links(valid_manifest,north_principal,fields):
    manifest=validate_manifest(valid_manifest); b=binding(manifest)
    target=PinnedProduct(manifest=manifest,binding_digest=b.digest,version_id='version-1')
    result=module('catalog').project_product(target,decision(north_principal,fields),b,'published')
    body=result.model_dump(mode='json')
    assert set(body['outputs'])==set(fields)
    assert set(body['filters']) <= set(fields) and set(body['units']) <= set(fields)
    assert set(body['sort']['fields']) <= set(fields)
    assert body['links']==[] and body['provenance']['links']==[]
    if 'segment' not in fields:assert 'segment' not in body['inputs']
    if 'day' not in fields:assert 'start' not in body['inputs'] and body['date_boundaries'] is None
    if len(fields)<3:
        assert body['description'] is None and body['examples']==[] and body['aggregation'] is None
    else:
        assert body['description']==manifest.description and len(body['examples'])==1
    assert body['digest']==manifest.content_digest
    assert not {'fixtures','redaction','policy_id','constraint_members'} & body.keys()
    assert 'Orders.' not in result.model_dump_json() and 'example.com' not in result.model_dump_json()


def test_cursor_is_encrypted_identity_scope_purpose_query_and_expiry_bound(clock):
    codec=module('cursors').PageCursorCodec(b'k'*32,clock)
    token=codec.issue(purpose='catalog',identity='i'*64,scope='s'*64,query='q'*64,last='restricted-revenue')
    assert 'restricted' not in token and 'catalog' not in token
    assert codec.read(token,purpose='catalog',identity='i'*64,scope='s'*64,query='q'*64).last=='restricted-revenue'
    for change in [{'purpose':'audit'},{'identity':'j'*64},{'scope':'r'*64},{'query':'x'*64}]:
        with pytest.raises(GatewayError) as caught:
            codec.read(token,**({'purpose':'catalog','identity':'i'*64,'scope':'s'*64,'query':'q'*64}|change))
        assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
    for bad in [token[:-2]+'xx','',token+'=', 'x'*4097]:
        with pytest.raises(GatewayError):codec.read(bad,purpose='catalog',identity='i'*64,scope='s'*64,query='q'*64)
    clock.instant += timedelta(seconds=301)
    with pytest.raises(GatewayError):codec.read(token,purpose='catalog',identity='i'*64,scope='s'*64,query='q'*64)


def test_readiness_not_ready_without_operator_probe(clock):
    svc=module('readiness').ReadinessService(registry=None,policy=None,adapter=None,bindings=None,bundles=None,
        clock=clock,probe_principal_id=None,probe_product_id=None)
    assert asyncio.run(svc.check()).ready is False


def test_scope_authorization_is_non_executable_and_audited(north_principal):
    from gateway.policy.service import PolicyService, PolicyContent
    from gateway.auth.principals import PrincipalGrants,PolicyGrant
    from gateway.policy.audit import AuditWriter
    store=SimpleNamespace(get_current=AsyncMock(return_value=PrincipalGrants(principal_id=north_principal.id,grants=[
        PolicyGrant(policy_id='revenue-access',tenant_id='north',capabilities=['author'],entitlements={})])))
    audit=AuditWriter(b'k'*32);audit.append=AsyncMock()
    svc=PolicyService(None,store,audit)
    svc._identity=AsyncMock();svc._current=AsyncMock(return_value=(SimpleNamespace(revision=3),PolicyContent(rules=rules(),reason='approved')))
    uow=SimpleNamespace(session=SimpleNamespace(rollback=AsyncMock()))
    method=getattr(svc,'authorize_scope',None)
    assert method is not None,'Non-product scope seam is missing'
    value=asyncio.run(method(north_principal,'revenue-access','author',uow))
    assert value.policy_revision==3 and value.action=='author' and not isinstance(value,Decision)
    assert not hasattr(value,'row_constraints')
    audit.append.assert_awaited_once()
    assert audit.append.await_args.args[1].policy_id=='revenue-access'
    with pytest.raises(GatewayError) as caught:asyncio.run(method(north_principal,'revenue-access','execute',uow))
    assert caught.value.code=='INVALID_INPUT'


class Rows:
    def __init__(self,rows):self.rows=rows
    def all(self):return self.rows
    def scalars(self):return self


class CatalogRegistry:
    """Sequencing fake only; it does not claim SQL/locking behavior."""
    def __init__(self,targets):
        self.targets={item.manifest.product_id:item for item in targets}
        self.status={item.version_id:'published' for item in targets}
        self.transactions=[]
        self.session=SimpleNamespace(execute=AsyncMock(side_effect=self.execute),scalar=AsyncMock(side_effect=self.scalar),
            get=AsyncMock(return_value=SimpleNamespace(current_revision=1)))
    async def execute(self,stmt):return Rows([(key,value.manifest.version,value.version_id) for key,value in sorted(self.targets.items())])
    async def scalar(self,stmt):return self.status[stmt.compile().params['version_id_1']]
    @asynccontextmanager
    async def transaction(self):
        tx=SimpleNamespace(session=self.session,committed=False)
        self.transactions.append(tx)
        yield tx
        tx.committed=True
    async def resolve(self,product_id,version=None):
        if product_id not in self.targets:
            from gateway.registry.repository import not_found
            raise not_found()
        return self.targets[product_id]


def catalog_setup(valid_manifest,north_principal,clock):
    from gateway.auth.principals import PrincipalGrants,PolicyGrant
    targets=[]
    for product_id in ['a-hidden','b-visible','c-visible']:
        raw={**valid_manifest,'product_id':product_id,'name':product_id,'description':'safe prose','content_digest':None}
        manifest=validate_manifest(raw);b=binding(manifest)
        targets.append(PinnedProduct(manifest=manifest,binding_digest=b.digest,version_id=product_id+'-v1'))
    registry=CatalogRegistry(targets)
    grants=PrincipalGrants(principal_id=north_principal.id,grants=[PolicyGrant(policy_id='revenue-access',tenant_id='north',capabilities=['discover','describe'],entitlements={'groups':['north']})])
    policy=SimpleNamespace(_identity=AsyncMock(),principals=SimpleNamespace(get_current=AsyncMock(return_value=grants)),
        _current=AsyncMock(return_value=(SimpleNamespace(revision=1,content_digest='p'*64),None)))
    async def authorize(principal,action,target,uow):
        if target.manifest.product_id=='a-hidden':
            from gateway.registry.repository import not_found
            raise not_found()
        return decision(principal,['day','revenue'])
    policy.authorize=AsyncMock(side_effect=authorize)
    service=module('catalog').CatalogService(registry=registry,policy=policy,
        bindings=SimpleNamespace(resolve=AsyncMock(return_value=b)),cursors=module('cursors').PageCursorCodec(b'k'*32,clock))
    return service,registry,policy


def test_catalog_filters_before_page_and_search_and_commits_selected(north_principal,valid_manifest,clock):
    svc,registry,policy=catalog_setup(valid_manifest,north_principal,clock)
    page=asyncio.run(svc.search(north_principal,limit=1))
    assert [item.product_id for item in page.items]==['b-visible'] and page.next_cursor
    next_page=asyncio.run(svc.search(north_principal,limit=1,cursor=page.next_cursor))
    assert [item.product_id for item in next_page.items]==['c-visible'] and next_page.next_cursor is None
    assert registry.transactions[-1].committed
    # Hidden freeform prose is removed before it can influence text matching.
    assert asyncio.run(svc.search(north_principal,query='safe prose')).items==()
    assert asyncio.run(svc.search(north_principal,query='a-hidden')).items==()


def test_catalog_cursor_invalidated_by_current_policy_and_final_recheck(north_principal,valid_manifest,clock):
    svc,registry,policy=catalog_setup(valid_manifest,north_principal,clock)
    page=asyncio.run(svc.search(north_principal,limit=1))
    policy._current.return_value=(SimpleNamespace(revision=2,content_digest='r'*64),None)
    with pytest.raises(GatewayError):asyncio.run(svc.search(north_principal,limit=1,cursor=page.next_cursor))
    calls=0
    original=policy.authorize.side_effect
    async def revoke(principal,action,target,uow):
        nonlocal calls
        calls+=1
        if calls==5:policy._current.return_value=(SimpleNamespace(revision=3,content_digest='t'*64),None)
        return await original(principal,action,target,uow)
    policy.authorize.side_effect=revoke
    with pytest.raises(GatewayError):asyncio.run(svc.search(north_principal,limit=1))


def test_catalog_final_denial_does_not_release_earlier_page(north_principal,valid_manifest,clock):
    svc,registry,policy=catalog_setup(valid_manifest,north_principal,clock)
    original=policy.authorize.side_effect
    counts={}
    async def deny_final(principal,action,target,uow):
        key=(target.version_id,action);counts[key]=counts.get(key,0)+1
        if target.manifest.product_id=='b-visible' and counts[key]==2:
            from gateway.registry.repository import not_found
            raise not_found()
        return await original(principal,action,target,uow)
    policy.authorize.side_effect=deny_final
    with pytest.raises(GatewayError):asyncio.run(svc.search(north_principal,limit=1))
    assert registry.transactions[-1].committed is False


def test_discovery_work_limit_is_explicit(north_principal,valid_manifest,clock):
    svc,registry,policy=catalog_setup(valid_manifest,north_principal,clock)
    registry.session.execute.side_effect=None;registry.session.execute.return_value=Rows([('a','1.0.0','v')]*1001)
    with pytest.raises(GatewayError) as caught:asyncio.run(svc.search(north_principal))
    assert caught.value.code=='RESOURCE_LIMIT'


def test_binding_and_policy_choices_implement_shared_service():
    assert callable(module('choices').ChoiceService.bindings)
    assert callable(module('choices').ChoiceService.policies)


def test_audit_read_implements_scoped_service():
    assert callable(module('audit').AuditReadService.list)


def choice_setup(valid_manifest,north_principal,clock):
    from gateway.auth.principals import PrincipalGrants,PolicyGrant
    manifest=validate_manifest(valid_manifest);b=binding(manifest)
    registry=CatalogRegistry([])
    registry.session.scalar.side_effect=None
    registry.session.scalar.return_value=SimpleNamespace(content_digest=b.digest,adapter_revision=b.adapter_revision,model_bundle_digest=b.model_bundle_digest)
    grants=PrincipalGrants(principal_id=north_principal.id,grants=[
        PolicyGrant(policy_id=pid,tenant_id='north',capabilities=['author'],entitlements={}) for pid in ['revenue-access','hidden-policy']])
    policy=SimpleNamespace(_identity=AsyncMock(),principals=SimpleNamespace(get_current=AsyncMock(return_value=grants)),
        _current=AsyncMock(return_value=(SimpleNamespace(revision=1,content_digest='p'*64),None)))
    async def scoped(actor,policy_id,action,uow):
        if policy_id=='hidden-policy' or action=='policy_admin':
            from gateway.registry.repository import not_found
            raise not_found()
        return SimpleNamespace(policy_revision=1)
    policy.authorize_scope=AsyncMock(side_effect=scoped)
    svc=module('choices').ChoiceService(registry=registry,policy=policy,
        bindings=SimpleNamespace(resolve=AsyncMock(return_value=b)),
        bundles=SimpleNamespace(resolve=lambda digest:SimpleNamespace(context='consumer')),
        cursors=module('cursors').PageCursorCodec(b'k'*32,clock),templates=(manifest,))
    svc._witnesses=AsyncMock(return_value=[manifest])
    return svc,policy


def test_choices_filter_scopes_and_never_enumerate_operator_binding_map(valid_manifest,north_principal,clock):
    svc,policy=choice_setup(valid_manifest,north_principal,clock)
    choices=asyncio.run(svc.policies(north_principal))
    assert [item.policy_id for item in choices.items]==['revenue-access']
    choices=asyncio.run(svc.bindings(north_principal))
    assert [item.binding_id for item in choices.items]==['revenue-orders']
    assert 'Orders.' not in choices.model_dump_json() and 'secret' not in choices.model_dump_json()


def test_changed_policy_choice_requires_both_old_and_new_admin(valid_manifest,north_principal,clock):
    svc,policy=choice_setup(valid_manifest,north_principal,clock)
    raw={**valid_manifest,'policy_id':'old-policy','content_digest':None}
    svc._context=AsyncMock(return_value=validate_manifest(raw))
    choices=asyncio.run(svc.policies(north_principal,draft_id='draft-1'))
    assert choices.items==()
    assert any(call.args[2]=='policy_admin' for call in policy.authorize_scope.await_args_list)


def test_audit_without_attributable_authorized_scope_is_denied(valid_manifest,north_principal,clock):
    svc,policy=choice_setup(valid_manifest,north_principal,clock)
    from gateway.registry.repository import not_found
    policy.authorize_scope.side_effect=not_found()
    reader=module('audit').AuditReadService(registry=svc.registry,policy=policy,cursors=svc.cursors,clock=clock)
    with pytest.raises(GatewayError) as caught:asyncio.run(reader.list(north_principal))
    assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'


def test_product_description_includes_effective_limits(valid_manifest,north_principal):
    manifest=validate_manifest(valid_manifest);b=binding(manifest)
    auth=decision(north_principal,['day','segment','revenue'])
    result=module('catalog').project_product(PinnedProduct(manifest=manifest,binding_digest=b.digest,version_id='version-1'),auth,b,'published')
    assert result.limits==auth.limits


def test_catalog_deferred_audit_failure_maps_availability(north_principal,valid_manifest,clock):
    svc,registry,policy=catalog_setup(valid_manifest,north_principal,clock)
    @asynccontextmanager
    async def failing():
        yield SimpleNamespace(session=registry.session)
        raise GatewayError('CONFLICT','db','sensitive deferred trigger text')
    registry.transaction=failing
    with pytest.raises(GatewayError) as caught:asyncio.run(svc.describe(north_principal,'b-visible'))
    assert caught.value.code=='UPSTREAM_UNAVAILABLE'


def test_example_values_conflicting_with_current_row_scope_are_omitted(valid_manifest,north_principal):
    from gateway.contracts.identity import RowConstraint
    valid_manifest['examples']=[{'description':'restricted segment','inputs':{'start':'2024-03-09','end':'2024-03-12','segment':'B'}}]
    manifest=validate_manifest(valid_manifest);b=binding(manifest)
    auth=decision(north_principal,['day','segment','revenue'])
    auth=auth.model_copy(update={'row_constraints':(*auth.row_constraints,RowConstraint(member='Orders.segment',operator='in',values=['A']))})
    result=module('catalog').project_product(PinnedProduct(manifest=manifest,binding_digest=b.digest,version_id='version-1'),auth,b,'published')
    assert result.examples==()


def ready_setup(valid_manifest,north_principal,clock):
    manifest=validate_manifest(valid_manifest);b=binding(manifest)
    target=PinnedProduct(manifest=manifest,binding_digest=b.digest,version_id='version-1')
    registry=CatalogRegistry([target])
    expected=module('readiness').ReadinessService.expected_heads()
    registry.session.execute.side_effect=None
    registry.session.execute.return_value=Rows(expected)
    policy=SimpleNamespace(principals=SimpleNamespace(resolve_principal=AsyncMock(return_value=north_principal)),
        authorize=AsyncMock(return_value=decision(north_principal,['day','segment','revenue'])))
    adapter=SimpleNamespace(validate_binding=AsyncMock())
    resolver=SimpleNamespace(resolve=AsyncMock(return_value=b))
    bundles=SimpleNamespace(resolve=lambda digest:SimpleNamespace(context='consumer'))
    svc=module('readiness').ReadinessService(registry=registry,policy=policy,adapter=adapter,bindings=resolver,
        bundles=bundles,clock=clock,probe_principal_id=north_principal.id,probe_product_id=manifest.product_id)
    return svc


def test_readiness_actual_check_sequence_is_fresh_and_metadata_only(valid_manifest,north_principal,clock):
    svc=ready_setup(valid_manifest,north_principal,clock)
    assert asyncio.run(svc.check()).ready is True
    assert asyncio.run(svc.check()).ready is True
    assert svc.policy.authorize.await_count==2 and svc.adapter.validate_binding.await_count==2
    args,kwargs=svc.adapter.validate_binding.await_args
    assert args[0].product_id=='revenue' and kwargs['deadline']==clock.now()+timedelta(seconds=3)
    assert kwargs['decision'].principal_id==north_principal.id and kwargs['request_id']


@pytest.mark.parametrize('failure',['head','multiple_heads','registry','identity','resolve','policy','binding','bundle','metadata','timeout'])
def test_readiness_fails_closed_for_each_dependency(valid_manifest,north_principal,clock,failure):
    svc=ready_setup(valid_manifest,north_principal,clock)
    error=GatewayError('UPSTREAM_UNAVAILABLE','internal','private source detail')
    if failure=='head':svc.registry.session.execute.return_value=Rows(['old'])
    elif failure=='multiple_heads':svc.expected_heads=lambda:['one','two']
    elif failure=='registry':svc.registry.session.execute.side_effect=error
    elif failure=='identity':svc.policy.principals.resolve_principal.side_effect=error
    elif failure=='resolve':svc.registry.resolve=AsyncMock(side_effect=error)
    elif failure=='policy':svc.policy.authorize.side_effect=error
    elif failure=='binding':svc.bindings.resolve.side_effect=error
    elif failure=='bundle':svc.bundles.resolve=lambda digest:SimpleNamespace(context='fixture_test')
    elif failure=='metadata':svc.adapter.validate_binding.side_effect=error
    elif failure=='timeout':
        svc.timeout_seconds=0.01
        async def slow(*args,**kwargs):await asyncio.sleep(1)
        svc.adapter.validate_binding.side_effect=slow
    assert asyncio.run(svc.check()).model_dump(mode='json')=={'ready':False}


def test_authorize_scope_audit_outage_and_cross_scope_fail_closed(north_principal):
    from gateway.policy.service import PolicyService, PolicyContent
    from gateway.auth.principals import PrincipalGrants,PolicyGrant
    from gateway.policy.audit import AuditWriter,audit_unavailable
    grants=PrincipalGrants(principal_id=north_principal.id,grants=[PolicyGrant(policy_id='revenue-access',tenant_id='north',capabilities=['author'],entitlements={})])
    writer=AuditWriter(b'k'*32);writer.append=AsyncMock(side_effect=audit_unavailable())
    registry=CatalogRegistry([])
    svc=PolicyService(registry,SimpleNamespace(get_current=AsyncMock(return_value=grants)),writer)
    svc._identity=AsyncMock();svc._current=AsyncMock(return_value=(SimpleNamespace(revision=1),PolicyContent(rules=rules(),reason='approved')))
    uow=SimpleNamespace(session=SimpleNamespace(rollback=AsyncMock()))
    with pytest.raises(GatewayError) as caught:asyncio.run(svc.authorize_scope(north_principal,'revenue-access','author',uow))
    assert caught.value.code=='UPSTREAM_UNAVAILABLE'
    writer.append.side_effect=None
    with pytest.raises(GatewayError) as caught:asyncio.run(svc.authorize_scope(north_principal,'another-policy','author',uow))
    assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'


def test_audit_page_bounds_precede_its_own_events_and_returns_only_contract(valid_manifest,north_principal,clock):
    svc,policy=choice_setup(valid_manifest,north_principal,clock)
    registry=svc.registry
    registry.session.scalar.return_value=clock.now()
    async def authorized(*args,**kwargs):return SimpleNamespace(policy_revision=1)
    policy.authorize_scope.side_effect=authorized
    entry=dict(id='audit-1',created_at=clock.now(),actor_id=north_principal.id,service_client_id=None,action='describe',
        product_id='revenue',version_id='version-1',policy_id='revenue-access',policy_revision=1,decision='allow',
        parameter_names=['start'],parameter_digest='a'*64,duration_ms=1,row_count=3,adapter='cube-rest-v1',correlation_id='request-1')
    registry.session.execute.side_effect=None
    registry.session.execute.return_value=Rows([SimpleNamespace(**entry,raw_body='never serialized'),
        SimpleNamespace(**{**entry,'id':'audit-2'},raw_body='never serialized')])
    reader=module('audit').AuditReadService(registry=registry,policy=policy,cursors=svc.cursors,clock=clock)
    result=asyncio.run(reader.list(north_principal,limit=1))
    assert result.items[0].id=='audit-1' and result.next_cursor and 'raw_body' not in result.model_dump_json()
    statement=registry.session.execute.await_args.args[0]
    assert statement.compile().params['created_at_1']==clock.now()
    assert registry.transactions[-1].committed is True


@pytest.mark.parametrize('value',[[],{},'', 'x'*129])
def test_choices_reject_invalid_draft_identifier_before_io(valid_manifest,north_principal,clock,value):
    svc,policy=choice_setup(valid_manifest,north_principal,clock)
    with pytest.raises(GatewayError) as caught:asyncio.run(svc.policies(north_principal,draft_id=value))
    assert caught.value.code=='INVALID_INPUT'
    policy._identity.assert_not_awaited()


def test_catalog_sql_order_matches_opaque_ascii_continuation(north_principal,valid_manifest,clock):
    svc,registry,policy=catalog_setup(valid_manifest,north_principal,clock)
    asyncio.run(svc.search(north_principal))
    statement=str(registry.session.execute.await_args_list[0].args[0])
    assert 'COLLATE "C"' in statement


@pytest.mark.parametrize('default,explicit,expected',[('B',False,False),('B',True,False),('A',False,True),('A',True,True)])
def test_examples_authorize_normalized_default_values(valid_manifest,north_principal,default,explicit,expected):
    from gateway.contracts.identity import RowConstraint
    valid_manifest['inputs']['segment']['default']={'value':default}
    if explicit:valid_manifest['examples'][0]['inputs']['segment']=default
    manifest=validate_manifest(valid_manifest);before=manifest.model_dump(mode='json');b=binding(manifest)
    auth=decision(north_principal,['day','segment','revenue'])
    auth=auth.model_copy(update={'row_constraints':(*auth.row_constraints,RowConstraint(member='Orders.segment',operator='in',values=['A']))})
    target=PinnedProduct(manifest=manifest,binding_digest=b.digest,version_id='version-1')
    result=module('catalog').project_product(target,auth,b,'published')
    assert bool(result.examples) is expected
    if expected:assert result.examples==manifest.examples
    assert result.digest==manifest.content_digest and manifest.model_dump(mode='json')==before


def test_examples_obey_tighter_current_date_limit_and_preserve_safe_example(valid_manifest,north_principal):
    from gateway.contracts.manifest import Limits
    valid_manifest['examples'].append({'description':'One permitted local day','inputs':{'start':'2024-03-10','end':'2024-03-11'}})
    manifest=validate_manifest(valid_manifest);before=manifest.model_dump(mode='json');b=binding(manifest)
    auth=decision(north_principal,['day','segment','revenue']).model_copy(update={'limits':Limits(max_date_range_days=1)})
    result=module('catalog').project_product(PinnedProduct(manifest=manifest,binding_digest=b.digest,version_id='version-1'),auth,b,'published')
    assert result.limits.max_date_range_days==1
    assert result.examples==(manifest.examples[1],)
    assert result.digest==manifest.content_digest and manifest.model_dump(mode='json')==before

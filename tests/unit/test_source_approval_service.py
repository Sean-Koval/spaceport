"""Closed internal operation variants and transaction-bound receipts."""
import importlib
from uuid import uuid4
import pytest

pytestmark=pytest.mark.unit


def api():
    try: return importlib.import_module('gateway.source_access')
    except ModuleNotFoundError: pytest.fail('Typed source access service is missing')


@pytest.mark.parametrize('kind,action', [('execution','author'),('fixture_invocation','execute'),('fixture_attempt','author')])
def test_owner_action_matrix_is_closed(kind,action):
    module=api()
    owner={'kind':kind,'id':'owner-1'}
    with pytest.raises(ValueError): module.validate_owner_operation(owner,action)


def test_known_owner_actions_and_no_transient_result_or_execute():
    module=api()
    for kind,action in [('execution','execute'),('execution','result'),('fixture_invocation','author'),('fixture_attempt','execute'),('fixture_attempt','result')]:
        assert module.validate_owner_operation({'kind':kind,'id':'owner-1'},action).kind==kind
    for action in ('result','execute','policy_admin','operate','audit_view'):
        with pytest.raises(ValueError): module.validate_transient_action(action)
    for action in ('discover','describe','author','publish'):
        assert module.validate_transient_action(action)==action


def test_receipt_cannot_cross_uow_or_survive_transaction():
    from types import SimpleNamespace
    module=api();transaction=object()
    class Session:
        current=transaction
        def get_transaction(self): return self.current
    first=SimpleNamespace(session=Session());other=SimpleNamespace(session=Session())
    receipt=module.AccessReceipt._issue(first,approval_id=str(uuid4()),approval_revision=1,approval_digest='a'*64,
        audience_entry_id='audience-1',projection_digest='b'*64,request_id=str(uuid4()))
    receipt.assert_uow(first)
    with pytest.raises(ValueError): receipt.assert_uow(other)
    first.session.current=object()
    with pytest.raises(ValueError): receipt.assert_uow(first)


def test_precheck_rejects_nested_model_hooks_before_serializing():
    from pydantic import model_serializer
    from gateway.contracts.execution import PinnedProduct
    from gateway.contracts.errors import GatewayError
    module=api();calls=[]
    class HostileProduct(PinnedProduct):
        @model_serializer
        def unsafe(self):
            calls.append('serializer')
            return {}
    values=dict.fromkeys(module.AccessRequest.model_fields)
    values['product']=HostileProduct.model_construct()
    request=module.AccessRequest.model_construct(**values)
    with pytest.raises((GatewayError,ValueError)):
        module.SourceAccessService(None,None).precheck(request)
    assert calls==[]


def test_prelock_rejects_invalid_package_before_any_database_call():
    import asyncio
    from types import SimpleNamespace
    module=api();calls=[]
    class Session:
        async def scalar(self,*args,**kwargs):
            calls.append('database')
            return object()
    async def scenario():
        with pytest.raises(Exception):
            await module.SourceAccessService(None,None).prelock(SimpleNamespace(session=Session()),(object(),))
    asyncio.run(scenario())
    assert calls==[]


from tests.unit.test_source_approval_contracts import package


def test_request_freshening_preserves_and_rejects_unknown_nested_product_fields(package):
    from gateway.contracts.execution import PinnedProduct
    from tests.unit.test_source_approval_contracts import parsed,principal,decision
    module=api();approval,pins=parsed(package)
    values=dict.fromkeys(module.AccessRequest.model_fields)
    values.update(request_kind='admission',product=PinnedProduct(manifest=package[1],binding_digest=pins.bindings.consumer.content_digest,
        version_id='synthetic-version'),principal=principal(),decision=decision(),action='execute',context='consumer',
        request_id=str(uuid4()),pins=pins,projection=approval.runtime_projection(str(uuid4())),audience_entry_id='north-consumer')
    request=module.AccessRequest.model_validate(values)
    assert module._fresh_request(request)==request
    object.__setattr__(request,'product',request.product.model_copy(update={'unexpected':'must-not-disappear'}))
    with pytest.raises(ValueError):module._fresh_request(request)


def test_fixture_result_requires_original_positive_case_actor_and_result_audience(package):
    import copy
    from gateway.contracts.canonical import canonical_digest,canonical_bytes
    from gateway.contracts.execution import PinnedProduct
    from gateway.contracts.identity import Principal
    from gateway.contracts.errors import GatewayError
    from tests.unit.test_source_approval_contracts import audience,principal,decision
    module=api();doc=copy.deepcopy(package[0])
    doc['audiences']=[audience(id='original-author',context='fixture_test',actions=['author']),
        audience(id='fixed-data',context='fixture_test',principal_id='fixed-north',principal_kind='demo',client_ids=[None],
            fixture_case_ids=['positive-case'],actions=['execute','result'])]
    from gateway.source_access_contracts import validate_source_approval
    approval=validate_source_approval(canonical_bytes(doc))
    pins=approval.package_pins(package[1],approval_digest=approval.content_digest)
    projected=approval.runtime_projection(str(uuid4()))
    fixed=Principal(id='fixed-north',subject='synthetic-fixture',service_client_id=None,kind='demo')
    admitted=decision(principal_id='fixed-north')
    accepted=module.AcceptedApprovalRef(approval_id=projected.approval_id,approval_revision=1,approval_digest=approval.content_digest,
        audience_entry_id='fixed-data',principal=fixed,package_pins_digest=pins.package_pins_digest,
        accepted_approval_expires_at='2030-01-01T00:00:00Z',effective_deadline_at='2030-01-01T00:00:00Z')
    values=dict.fromkeys(module.AccessRequest.model_fields)
    values.update(request_kind='owner',owner={'kind':'fixture_attempt','id':'positive-attempt'},
        product=PinnedProduct(manifest=package[1],binding_digest=pins.bindings.fixture.content_digest,version_id='draft-1'),
        principal=fixed,decision=admitted,action='result',context='fixture_test',request_id=str(uuid4()),pins=pins,
        projection=projected,audience_entry_id='fixed-data',accepted=accepted,admitted_decision=admitted,
        admitted_decision_digest=canonical_digest(admitted),fixture_case_id='positive-case',
        invoking_actor=principal(),invoking_actor_decision=decision(),invoking_actor_entry_id='original-author')
    request=module.AccessRequest.model_validate(values)
    assert module.SourceAccessService(None,None).precheck(request)=='fixed-data'
    for change in ({'admitted_decision':None,'admitted_decision_digest':None},{'fixture_case_id':None},{'invoking_actor':None},
        {'accepted':accepted.model_copy(update={'approval_id':str(uuid4())})}):
        with pytest.raises(ValueError):module.AccessRequest.model_validate(values|change)
    for change in ({'fixture_case_id':'negative-case'},
        {'invoking_actor':principal().model_copy(update={'service_client_id':'different-client'})}):
        with pytest.raises(GatewayError):module.SourceAccessService(None,None).precheck(request.model_copy(update=change))
    doc['audiences'][1]['actions']=['execute']
    changed=validate_source_approval(canonical_bytes(doc))
    with pytest.raises(GatewayError):module.SourceAccessService(None,None).precheck(request.model_copy(update={
        'projection':changed.runtime_projection(projected.approval_id)}))


def test_prelock_unions_explicit_policy_heads_before_versions(package,monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from gateway.contracts.execution import PinnedProduct
    module=api();calls=[]
    async def capacity(uow):calls.append('C')
    monkeypatch.setattr('gateway.execution.queue.lock_execution_capacity',capacity)
    class Session:
        async def scalar(self,statement):
            calls.append('V' if 'version_states' in str(statement) else 'P')
    class Uow:
        session=Session()
        async def lock_policy(self,key):calls.append('Q:'+key)
        async def lock_draft(self,key):calls.append('D:'+key)
    product=PinnedProduct(manifest=package[1],binding_digest='a'*64,version_id='v1')
    asyncio.run(module.SourceAccessService(None,None).prelock(Uow(),(product,),
        policy_ids=('zzz-policy','aaa-policy'),version_ids=('v1',)))
    assert calls==['C','P']+['Q:'+key for key in sorted({'aaa-policy','zzz-policy',product.manifest.policy_id})]+['V']


@pytest.mark.parametrize('kind',['product','owner','raw_owner'])
def test_private_request_and_owner_keys_are_inert_before_hash_or_equality(package,kind):
    from gateway.contracts.execution import PinnedProduct
    module=api();calls=[]
    class Key(str):
        def __hash__(self):calls.append('hash');return str.__hash__(self)
        def __eq__(self,other):calls.append('eq');return str.__eq__(self,other)
    if kind=='product':
        value=PinnedProduct(manifest=package[1],binding_digest='a'*64,version_id='v1')
        data=dict(value.__dict__);original=data.pop('binding_digest');data[Key('binding_digest')]=original
        object.__setattr__(value,'__dict__',data)
        check=lambda:module._service_plain(value)
    else:
        data={Key('kind'):'execution','id':'owner-1'}
        if kind=='owner':
            value=module.ExecutionOwner(kind='execution',id='owner-1')
            object.__setattr__(value,'__dict__',data)
        else:value=data
        check=lambda:module.validate_owner_operation(value,'execute')
    calls.clear()
    with pytest.raises(ValueError):check()
    assert calls==[]


def test_transient_match_rejects_coherently_conflicting_protected_binding_without_abort(package):
    import asyncio,copy
    from types import SimpleNamespace
    from gateway.contracts.canonical import canonical_bytes
    from gateway.contracts.execution import PinnedProduct
    from gateway.contracts.errors import GatewayError
    from gateway.source_access_contracts import validate_source_approval
    from tests.unit.test_source_approval_contracts import parsed,principal,decision
    module=api();approval,pins=parsed(package);calls=[]
    value=copy.deepcopy(package[0]);value['bindings']['consumer']['content_digest']='f'*64
    corrupt=validate_source_approval(canonical_bytes(value)).runtime_projection(str(uuid4()))
    valid=approval.runtime_projection(str(uuid4()))
    product=PinnedProduct(manifest=package[1],binding_digest=pins.bindings.consumer.content_digest,version_id='unsaved')
    class Session:
        async def scalar(self,*args):
            return SimpleNamespace(content_digest=product.binding_digest,adapter_revision=product.manifest.adapter_revision,
                model_bundle_digest=product.manifest.model_bundle_digest)
        async def scalars(self,*args):return [corrupt.model_dump(mode='json'),valid.model_dump(mode='json')]
        async def rollback(self):calls.append('rollback')
    async def scenario():
        with pytest.raises(GatewayError):
            await module.SourceAccessService(None,None).match_transient(SimpleNamespace(session=Session()),product,
                principal(),decision(),action='describe',request_id=str(uuid4()))
    asyncio.run(scenario())
    assert calls==[]


def test_fixture_snapshot_service_methods_delegate_only_to_inert_loaders(monkeypatch):
    import asyncio
    module=api();calls=[];uow=object();service=module.SourceAccessService(None,None)
    names=('load_fixture_invocation_access','load_fixture_attempt_access','load_fixture_case_review','load_fixture_test_access')
    for name in names:
        async def load(*args,_name=name,**kwargs):calls.append((_name,args,kwargs));return _name
        monkeypatch.setattr(module,name,load)
    async def run():
        assert await service.load_fixture_invocation_access(uow,'inv')==names[0]
        assert await service.load_fixture_attempt_access(uow,'attempt')==names[1]
        assert await service.load_fixture_case_review(uow,invocation_id='inv',fixture_id='case',request_id='request')==names[2]
        assert await service.load_fixture_test_access(uow,'test')==names[3]
    asyncio.run(run())
    assert [item[0] for item in calls]==list(names)
    assert all(item[1][0] is uow for item in calls)

"""Reviewed v2 contracts, with immutable v1 compatibility."""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from gateway.contracts.canonical import canonical_digest
from gateway.contracts.identity import Decision
from gateway.contracts.manifest import Limits

pytestmark = [pytest.mark.unit, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[2]


def definition(cube='Bookings'):
    return dict(context='consumer', binding_id='bookings', revision=1,
        adapter_revision='cube-rest-v2', model_bundle_digest='b'*64,
        fields={'day':dict(member=f'{cube}.day',kind='time',cube_type='time',response_key=f'{cube}.day.day',granularity='day'),
            'segment':dict(member=f'{cube}.category',kind='dimension',cube_type='string',response_key=f'{cube}.category'),
            'revenue':dict(member=f'{cube}.total',kind='measure',cube_type='number',response_key=f'{cube}.total')},
        input_fields={'segment':'segment'},
        scope=dict(tenant_member=f'{cube}.tenant',access_group_member=f'{cube}.group'),
        checkpoint_member=f'{cube}.loaded_at')


def decision(cube='Bookings'):
    return Decision(principal_id='north-user',policy_revision=1,tenant_id='north',
        row_constraints=[dict(member=f'{cube}.tenant',operator='eq',values=['north']),
            dict(member=f'{cube}.group',operator='in',values=['north-A'])],
        visible_fields=['day','segment','revenue'],limits=Limits())


def test_v1_remains_exact_and_v2_names_are_not_hardcoded():
    from gateway.adapters.base import CubeBinding
    from gateway.adapters.reviewed import ReviewedCubeBinding, parse_binding, validate_binding_decision
    raw=json.loads((ROOT/'fixtures/bindings/revenue.json').read_text())
    old=CubeBinding.model_validate(raw)
    assert parse_binding(raw).digest==old.digest==canonical_digest(raw)
    for cube in ['Bookings','Inventory']:
        b=ReviewedCubeBinding.model_validate(definition(cube))
        assert parse_binding(b).digest==b.digest
        assert validate_binding_decision(b,decision(cube),'request')==decision(cube)
    with pytest.raises(ValidationError): CubeBinding.model_validate(definition())


@pytest.mark.parametrize('mutation',[
    lambda x:x.update(sql='SELECT secret'),
    lambda x:x['scope'].update(access_group_member=x['scope']['tenant_member']),
    lambda x:x['scope'].update(tenant_member='Other.tenant'),
    lambda x:x['fields']['revenue'].update(member='Other.total',response_key='Other.total'),
    lambda x:x['fields']['revenue'].update(cube_type='string'),
    lambda x:x['fields']['day'].update(granularity=None,response_key='Bookings.day'),
    lambda x:x['input_fields'].update(segment='revenue'),
    lambda x:x.update(checkpoint_member='Other.loaded_at'),
    lambda x:x.update(checkpoint_member='Bookings.day'),
])
def test_v2_rejects_unreviewable_contract(mutation):
    from gateway.adapters.reviewed import ReviewedCubeBinding
    value=definition(); mutation(value)
    with pytest.raises((ValueError,ValidationError)): ReviewedCubeBinding.model_validate(value)


@pytest.mark.parametrize('mutation',[
    lambda x:x.update(tenant_id='south'),
    lambda x:x.update(row_constraints=x['row_constraints'][:1]),
    lambda x:x['row_constraints'][1].update(member='Bookings.category'),
    lambda x:x['row_constraints'][1].update(values=['north-A','north-A']),
    lambda x:x['row_constraints'].append(dict(member='Bookings.category',operator='eq',values=['A'])),
])
def test_scope_must_match_independent_reviewed_roles(mutation):
    from gateway.adapters.reviewed import ReviewedCubeBinding, validate_binding_decision
    from gateway.contracts.errors import GatewayError
    value=decision().model_dump(mode='json');mutation(value)
    with pytest.raises((GatewayError,ValueError)):
        validate_binding_decision(ReviewedCubeBinding.model_validate(definition()),Decision.model_validate(value),'request')

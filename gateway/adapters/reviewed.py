"""Versioned operator-reviewed semantics; never accepted from public clients.

V1 stays frozen. V2 has one daily aggregate cube and an inline committed-load
checkpoint selected in the same source statement as its rows. Atomic publication
and a unique tenant checkpoint join are operator prerequisites, not metadata facts.
"""
import re
from datetime import timedelta
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from gateway.contracts.canonical import Identifier, Digest, StrictModel, canonical_digest
from gateway.contracts.errors import ErrorCode, GatewayError
from gateway.contracts.identity import Decision
from gateway.contracts.manifest import iso_date
from gateway.contracts.query import validate_query
from .base import CubeBinding, CubeMember, MemberBinding, validate_data_decision, validate_query_visibility


class ScopeMembers(StrictModel):
    tenant_member: CubeMember
    access_group_member: CubeMember


class ReviewedCubeBinding(StrictModel):
    context: Literal['consumer','fixture_test'] = 'consumer'
    binding_id: Identifier
    revision: int = Field(ge=1)
    adapter_revision: Literal['cube-rest-v2']
    model_bundle_digest: Digest
    fields: Annotated[dict[Identifier,MemberBinding],Field(min_length=2,max_length=19)]
    input_fields: Annotated[dict[Identifier,Identifier],Field(max_length=19)] = Field(default_factory=dict)
    scope: ScopeMembers
    checkpoint_member: CubeMember

    @model_validator(mode='after')
    def reviewed_semantics(self):
        items=list(self.fields.values())
        cubes={item.member.split('.')[0] for item in items}
        if len(cubes)!=1 or len({i.member for i in items})!=len(items):
            raise ValueError('One reviewed cube and distinct members are required')
        cube=next(iter(cubes))
        expected={'measure':'number','dimension':'string','time':'time'}
        if any(item.cube_type!=expected[item.kind] for item in items):
            raise ValueError('Unsupported reviewed scalar type')
        if sum(i.kind=='time' for i in items)!=1 or not any(i.kind=='measure' for i in items):
            raise ValueError('One daily dimension and at least one measure are required')
        hidden=(self.scope.tenant_member,self.scope.access_group_member,self.checkpoint_member)
        if (len(set(hidden))!=3 or any(m.split('.')[0]!=cube for m in hidden)
            or set(hidden)&{i.member for i in items}):
            raise ValueError('Independent same-cube hidden scope and checkpoint members are required')
        if any(f not in self.fields or self.fields[f].kind!='dimension' for f in self.input_fields.values()):
            raise ValueError('Inputs require approved string dimensions')
        return self

    @property
    def constraint_members(self):
        return (self.scope.tenant_member,self.scope.access_group_member)

    @property
    def digest(self): return canonical_digest(self)


Binding = Annotated[CubeBinding | ReviewedCubeBinding,Field(discriminator='adapter_revision')]
_BINDING = TypeAdapter(Binding)


def parse_binding(value) -> Binding:
    if isinstance(value,(CubeBinding,ReviewedCubeBinding)):
        value=value.model_dump(mode='python')
    return _BINDING.validate_python(value)


def validate_binding_decision(binding, decision, request_id):
    if isinstance(binding,CubeBinding):
        return validate_data_decision(decision,request_id,binding.constraint_members)
    try:
        binding=parse_binding(binding)
        decision=Decision.model_validate(decision.model_dump(mode='python'))
        constraints={item.member:item for item in decision.row_constraints}
        if len(decision.row_constraints)!=2 or set(constraints)!=set(binding.constraint_members): raise ValueError
        for item in constraints.values():
            if (any(type(v) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',v) for v in item.values)
                or len(set(item.values))!=len(item.values)): raise ValueError
        tenant=constraints[binding.scope.tenant_member]
        if tenant.operator!='eq' or tenant.values!=(decision.tenant_id,): raise ValueError
        if constraints[binding.scope.access_group_member].operator not in {'eq','in'}: raise ValueError
        return decision
    except (ValueError,TypeError,AttributeError,KeyError):
        raise GatewayError(ErrorCode.NOT_FOUND_OR_FORBIDDEN,request_id,'The current scope cannot execute this product.') from None


def security_contract(binding):
    binding=parse_binding(binding)
    if not isinstance(binding,ReviewedCubeBinding): raise ValueError('A reviewed v2 binding is required')
    members={i.member:{'kind':i.kind,'type':i.cube_type} for i in binding.fields.values()}
    members.update({m:{'kind':'dimension','type':'string'} for m in binding.constraint_members})
    members[binding.checkpoint_member]={'kind':'dimension','type':'string'}
    return dict(schema_version=1,adapter_revision='cube-rest-v2',data_cube=binding.checkpoint_member.split('.')[0],
        members=members,tenant_member=binding.scope.tenant_member,access_group_member=binding.scope.access_group_member,
        time_member=next(i.member for i in binding.fields.values() if i.kind=='time'),checkpoint_member=binding.checkpoint_member)


def date_filter(member,operator,value):
    def bound(day,op): return {'member':member,'operator':op,'values':[day.isoformat()+'T00:00:00']}
    day=iso_date(value); tomorrow=day+timedelta(days=1)
    if operator=='eq': return {'and':[bound(day,'afterOrOnDate'),bound(tomorrow,'beforeDate')]}
    if operator=='ne': return {'or':[bound(day,'beforeDate'),bound(tomorrow,'afterOrOnDate')]}
    return bound(tomorrow if operator in {'gt','lte'} else day,{'gte':'afterOrOnDate','gt':'afterOrOnDate','lt':'beforeDate','lte':'beforeDate'}[operator])


def compile_query(binding, manifest, request, decision, *, max_rows=1000, request_id='query-compilation'):
    """Pure deterministic query compiled only from trusted pinned authority."""
    binding=parse_binding(binding)
    if not isinstance(binding,ReviewedCubeBinding): raise ValueError('A reviewed v2 binding is required')
    decision=validate_binding_decision(binding,decision,request_id)
    request=validate_query(manifest,request)
    if (not {n for n,i in binding.fields.items() if i.kind!='measure'}<=set(decision.visible_fields)
        or set(decision.visible_fields)-set(manifest.outputs)):
        raise GatewayError(ErrorCode.NOT_FOUND_OR_FORBIDDEN,request_id,'The current field permissions are incompatible.')
    validate_query_visibility(binding,request,decision,request_id)
    bounds=manifest.date_boundaries
    if (iso_date(request.inputs[bounds.end_input])-iso_date(request.inputs[bounds.start_input])).days>decision.limits.max_date_range_days:
        raise GatewayError(ErrorCode.INVALID_INPUT,request_id,'The requested date range exceeds the effective limit.')
    time_member=next(i.member for i in binding.fields.values() if i.kind=='time')
    filters=[{'member':time_member,'operator':'afterOrOnDate','values':[request.inputs[bounds.start_input]+'T00:00:00']},
        {'member':time_member,'operator':'beforeDate','values':[request.inputs[bounds.end_input]+'T00:00:00']}]
    for name,field in binding.input_fields.items():
        if name in request.inputs and request.inputs[name] is not None:
            filters.append({'member':binding.fields[field].member,'operator':'equals','values':[request.inputs[name]]})
    operators={'eq':'equals','ne':'notEquals','in':'equals','not_in':'notEquals','lt':'lt','lte':'lte','gt':'gt','gte':'gte','is_null':'notSet'}
    for f in request.filters:
        member=binding.fields[f.field]
        if member.kind=='time':
            filters.append({'or' if f.operator=='in' else 'and':[date_filter(member.member,'eq' if f.operator=='in' else 'ne',v) for v in f.value]} if f.operator in {'in','not_in'} else date_filter(member.member,f.operator,f.value))
        else:
            item={'member':member.member,'operator':operators[f.operator]}
            if f.operator!='is_null':
                values=list(f.value) if isinstance(f.value,tuple) else [f.value]
                if any(type(v) is not str or len(v)>128 for v in values): raise ValueError('Unsupported bounded filter value')
                item['values']=values
            filters.append(item)
    filters.extend({'member':c.member,'operator':'equals','values':list(c.values)} for c in decision.row_constraints)
    return {'measures':[i.member for n,i in binding.fields.items() if i.kind=='measure' and n in decision.visible_fields],
        'dimensions':[i.member for i in binding.fields.values() if i.kind=='dimension']+[binding.checkpoint_member],
        'timeDimensions':[{'dimension':time_member,'granularity':'day'}], 'filters':[{'and':filters}],
        'order':[[binding.fields[i.field].member,i.direction] for i in request.sort],
        'timezone':manifest.timezone,'limit':min(manifest.limits.max_rows,decision.limits.max_rows,max_rows)+1}

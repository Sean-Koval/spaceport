from copy import deepcopy
from types import SimpleNamespace

import pytest

from gateway.adapters.reviewed import ReviewedCubeBinding, security_contract
from gateway.adapters.base import StaticBindingResolver
from gateway.adapters.bundles import BundleCatalog
from gateway.contracts.canonical import canonical_digest
from gateway.contracts.execution import ExecutionSpec, PinnedProduct
from gateway.contracts.manifest import Manifest
from gateway.contracts.query import QueryRequest, validate_query
from test_bindings import definition, decision


@pytest.fixture
def reviewed_case(valid_manifest):
    binding=ReviewedCubeBinding.model_validate(definition())
    raw=deepcopy(valid_manifest)
    raw.update(binding_id=binding.binding_id,binding_revision=binding.revision,
        adapter_revision=binding.adapter_revision,model_bundle_digest=binding.model_bundle_digest)
    manifest=Manifest.model_validate(raw)
    product=PinnedProduct(manifest=manifest,binding_digest=binding.digest,version_id='bookings-1')
    allowed=decision().model_copy(update={'limits':manifest.limits})
    request=validate_query(manifest,QueryRequest(product_id='revenue',version='1.0.0',inputs={'start':'2024-03-09','end':'2024-03-12'}))
    canonical=request.model_dump(mode='json',exclude={'idempotency_key'})
    spec=ExecutionSpec(principal_id=allowed.principal_id,pinned_product=product,canonical_query=canonical,
        canonical_hash=canonical_digest(canonical),policy_revision=1,context='consumer',request_id='reviewed-request')
    catalog=BundleCatalog({'consumer':dict(endpoint_id='reviewed-consumer',url='https://cube.example.com',
        bundle_digest=binding.model_bundle_digest,secret_ref='/run/secrets/cube_signing_key',context='consumer',adapter_revision='cube-rest-v2')})
    entries=security_contract(binding)['members']
    meta={'cubes':[{'name':'Bookings',
        'measures':[{'name':n,'type':v['type']} for n,v in entries.items() if v['kind']=='measure'],
        'dimensions':[{'name':n,'type':v['type']} for n,v in entries.items() if v['kind']!='measure']}]}
    row={'Bookings.day.day':'2024-03-09T00:00:00.000','Bookings.category':'A','Bookings.total':'9007199254740993.01','Bookings.loaded_at':'2024-03-12T00:00:00.000Z'}
    return SimpleNamespace(binding=binding,manifest=manifest,product=product,decision=allowed,request=request,spec=spec,catalog=catalog,
        bindings=StaticBindingResolver([binding]),meta=meta,row=row)

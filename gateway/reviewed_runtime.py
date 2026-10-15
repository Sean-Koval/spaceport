"""Explicit composition for the retained, synthetic reviewed-Postgres example.

No live-source selector, secret generation, networking, provisioning or publication
occurs in the asset loader. Composition uses caller-supplied existing registry,
authentication and mounted-key resolution. Original demo/pilot entrypoints remain
unchanged; this is the reviewed example's isolated integration seam.
"""
import json
from pathlib import Path

from gateway.adapters.base import StaticBindingResolver
from gateway.adapters.bundles import BundleCatalog
from gateway.adapters.cube import CubeAdapter
from gateway.adapters.reviewed import ReviewedCubeBinding, security_contract
from gateway.api.audit import AuditReadService
from gateway.api.catalog import CatalogService
from gateway.api.choices import ChoiceService
from gateway.api.cursors import PageCursorCodec
from gateway.api.drafts import DraftDiscoveryService
from gateway.api.readiness import ReadinessService
from gateway.api.services import Services
from gateway.auth.principals import PrincipalStore
from gateway.contracts.canonical import canonical_bytes
from gateway.contracts.clock import SystemClock
from gateway.contracts.manifest import validate_manifest
from gateway.execution.results import ResultService
from gateway.execution.service import ExecutionService
from gateway.policy.audit import AuditWriter
from gateway.policy.service import PolicyContent, PolicyService
from gateway.publishing.service import PublishingService
from gateway.publishing.test_runner import FixtureCatalog, FixtureRunner
from gateway.runtime import Runtime
from gateway.runtime_config import Assets, ROOT, load_assets as legacy_assets
from scripts.reviewed_cube_bundle_digest import bundle_digest


def load_assets(root: Path=ROOT) -> Assets:
    """Read and cross-check this finite operator-reviewed asset set, offline."""
    path=root/'fixtures/reviewed-postgres'; cube=root/'cube/reviewed-postgres'
    def read(p):
        if p.is_symlink() or not p.is_file() or p.stat().st_size>1_048_576: raise ValueError('Invalid reviewed asset')
        def unique(pairs):
            result={}
            for k,v in pairs:
                if k in result: raise ValueError('Duplicate reviewed asset key')
                result[k]=v
            return result
        return json.loads(p.read_bytes(),object_pairs_hook=unique)
    manifest=validate_manifest(read(path/'product.json'))
    binding=ReviewedCubeBinding.model_validate(read(path/'binding.json'))
    fixtures=FixtureCatalog.model_validate(read(path/'tests.json'))
    fixtures.approve(manifest,binding)
    bundles=BundleCatalog(read(cube/'bundles.json'))
    contract=read(cube/'security.json')
    for b,directory in [(binding,'model'),(fixtures.fixture_binding,'test-model')]:
        endpoint=bundles.resolve(b.model_bundle_digest)
        if (endpoint.context!=b.context or endpoint.adapter_revision!='cube-rest-v2'
            or b.model_bundle_digest!=bundle_digest(cube/directory,root=cube)
            or canonical_bytes(contract)!=canonical_bytes(security_contract(b))):
            raise ValueError('Reviewed bundle identity is incompatible')
    policy=read(path/'policy.json')
    if set(policy)!={'policy_id','revision','content'} or policy['policy_id']!=manifest.policy_id or policy['revision']!=1:
        raise ValueError('Reviewed policy identity is incompatible')
    content=PolicyContent.model_validate(policy['content'])
    if (content.rules.tenant_constraint.member!=binding.scope.tenant_member
        or content.rules.entitlement_constraint.member!=binding.scope.access_group_member):
        raise ValueError('Reviewed policy scope is incompatible')
    old=legacy_assets(root)
    return Assets(manifest,(binding,fixtures.fixture_binding),bundles,fixtures,policy['policy_id'],1,policy['content'],old.accounts)


def build_runtime(*, registry, authenticator, secret_resolver, audit_key: bytes, cursor_key: bytes,
                  assets: Assets | None=None, clock=None, transport=None) -> Runtime:
    """Compose actual existing services; caller owns explicit setup/provisioning.

    Endpoint overrides are only internal reviewed deployment configuration. They
    must retain the bundle/adapter/context identities verified by this factory.
    The factory never supplies demo identity to an organization deployment.
    """
    assets=assets if assets is not None else load_assets()
    verified=load_assets()
    if (assets.manifest!=verified.manifest or assets.bindings!=verified.bindings
        or assets.fixture_catalog!=verified.fixture_catalog or assets.policy_id!=verified.policy_id
        or assets.policy_revision!=verified.policy_revision or assets.policy_content!=verified.policy_content
        or assets.accounts!=verified.accounts):
        raise ValueError('Only the retained reviewed example is composed here')
    for binding in assets.bindings:
        endpoint=assets.bundles.resolve(binding.model_bundle_digest)
        expected=verified.bundles.resolve(binding.model_bundle_digest)
        if endpoint.model_dump(exclude={'url'})!=expected.model_dump(exclude={'url'}):
            raise ValueError('Reviewed endpoint identity changed')
    clock=clock or SystemClock()
    principals=PrincipalStore(registry.sessions)
    policy=PolicyService(registry,principals,AuditWriter(audit_key))
    bindings=StaticBindingResolver(list(assets.bindings))
    def adapter_factory():
        return CubeAdapter(catalog=assets.bundles,bindings=bindings,secret_resolver=secret_resolver,clock=clock,transport=transport)
    adapter=adapter_factory()
    cursors=PageCursorCodec(cursor_key,clock)
    publishing=PublishingService(registry=registry,policy=policy,bindings=bindings,bundles=assets.bundles,
        fixture_runner=FixtureRunner(registry=registry,policy=policy,bindings=bindings,bundles=assets.bundles,
            catalog_provider=lambda:assets.fixture_catalog,adapter_factory=adapter_factory,clock=clock))
    services=Services(authenticator=authenticator,registry=registry,policy=policy,bindings=bindings,bundles=assets.bundles,
        execution=ExecutionService(registry=registry,policy=policy,bindings=bindings,clock=clock),
        results=ResultService(registry,policy,bindings,clock),publishing=publishing,clock=clock,
        catalog=CatalogService(registry=registry,policy=policy,bindings=bindings,cursors=cursors),
        drafts=DraftDiscoveryService(registry=registry,policy=policy,publishing=publishing,cursors=cursors),
        choices=ChoiceService(registry=registry,policy=policy,bindings=bindings,bundles=assets.bundles,cursors=cursors,templates=(assets.manifest,)),
        audit_reader=AuditReadService(registry=registry,policy=policy,cursors=cursors,clock=clock),
        readiness=ReadinessService(registry=registry,policy=policy,adapter=adapter,bindings=bindings,bundles=assets.bundles,
            clock=clock,probe_principal_id='demo-north',probe_product_id=assets.manifest.product_id))
    return Runtime(services,adapter,adapter_factory)

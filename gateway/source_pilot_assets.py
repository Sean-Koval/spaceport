"""Offline, fixed-dispatch semantic composition of retained reviewed packages."""
from dataclasses import dataclass
from types import MappingProxyType
from pydantic import Field,model_validator

from gateway.contracts.canonical import Array,Identifier,canonical_digest,json_value
from gateway.contracts.errors import GatewayError
from gateway.product_packages import ProductCatalog,SourceCatalog,VersionOne,read_asset,PackageError
from gateway.source_pilot_bootstrap import IdentityMapping,PolicySeed,validate_identity_mapping
from gateway.source_pilot_bigquery import CubeBundleFiles


class BundleFile(VersionOne):
    endpoints: dict[Identifier,dict] = Field(min_length=2,max_length=50)
    statement_catalogs: Array[Array[dict]] = Field(default=(),max_length=50)
    cube_bundles: Array[CubeBundleFiles] = Field(default=(),max_length=50)

    @model_validator(mode='after')
    def distinct_bundles(self):
        if len({b.digest for b in self.cube_bundles})!=len(self.cube_bundles):raise ValueError('Duplicate Cube bundle')
        return self


def validate_bigquery_isolation(consumer,fixture):
    physical=lambda s:{(s.data_project,s.dataset,table) for table in s.tables}
    if (consumer.provider!='bigquery' or fixture.provider!='bigquery'
        or consumer.context!='consumer' or fixture.context!='fixture_test'
        or consumer.source_id==fixture.source_id or consumer.auth.expected_principal==fixture.auth.expected_principal
        or consumer.cube_endpoint==fixture.cube_endpoint or consumer.model_bundle_digest==fixture.model_bundle_digest
        or physical(consumer)&physical(fixture)):
        raise ValueError('Independent configured BigQuery fixture identity required')


def validate_source_roles(config,sources):
    descriptors={(s.source_id,s.revision):s for s in sources};partitions={};coverage=set()
    for row in config.source_role_mappings:
        source=descriptors.get((row.source_id,row.revision))
        if source is None or source.context!=row.context or source.provider=='domo' and row.role_kind=='observer':
            raise ValueError('Source role mapping differs from the configured source')
        partition=(source.provider,None if row.role_kind=='observer' else row.context,row.role_kind)
        if row.login_name in partitions and partitions[row.login_name]!=partition:
            raise ValueError('Source login crosses a provider/context/capability partition')
        partitions[row.login_name]=partition
        coverage.add((row.source_id,row.revision,row.context,row.role_kind))
    for source in sources:
        kinds=('runtime','observer') if source.provider=='bigquery' else ('runtime',)
        if any((source.source_id,source.revision,source.context,kind) not in coverage for kind in kinds):
            raise ValueError('Every source requires its exact existing runtime/observer logins')
    for role,context in (('domo_consumer','consumer'),('domo_fixture','fixture_test')):
        needs=any(s.provider=='domo' and s.context==context for s in sources)
        login=config.database_roles.get(role)
        if needs!=(login is not None):raise ValueError('Domo registry credentials must match configured contexts')
        if login and partitions.get(login.username)!=('domo',context,'runtime'):
            raise ValueError('Domo registry credential is outside its exact source partition')


class FixtureCatalogs:
    def __init__(self,entries):
        self._entries=MappingProxyType(dict(entries))

    def resolve(self,manifest,binding):
        try:
            catalog=self._entries[(manifest.product_id,manifest.version,binding.digest)]
            catalog.approve(manifest,binding)
            return catalog
        except (KeyError,ValueError,GatewayError):
            raise PackageError() from None


@dataclass(frozen=True)
class SourceAssets:
    sources: SourceCatalog
    packages: ProductCatalog
    identities: IdentityMapping
    bindings: tuple
    bundles: object
    fixtures: FixtureCatalogs
    statements: object
    cube_bundles: object



def _offline_domo_cases(retained,fixture_catalog,identities,statements):
    from gateway.adapters.domo_runtime import compile_domo_query
    from gateway.auth.principals import PrincipalGrants,PolicyGrant,capabilities_for_roles
    from gateway.contracts.execution import PinnedProduct
    from gateway.contracts.query import QueryRequest
    from gateway.policy.service import derive_decision
    manifest=retained.package.manifest
    seed=PolicySeed.model_validate(retained.assets['policy_seed_ref'])
    def decision(principal,grant,target):
        return derive_decision(PrincipalGrants(principal_id=principal,grants=[PolicyGrant(policy_id=manifest.policy_id,
            tenant_id=grant.tenant_id,capabilities=capabilities_for_roles(grant.roles),entitlements=grant.entitlements)]),
            'execute',target,seed.content.rules,seed.revision)
    fixture_manifest=fixture_catalog.execution_manifest(manifest)
    target=PinnedProduct(manifest=fixture_manifest,binding_digest=fixture_catalog.fixture_binding.digest,version_id='offline-fixture')
    fixture_principals={p.id:p for p in seed.fixture_principals}
    for case in fixture_catalog.cases:
        if case.kind!='known_answer':continue
        grant=fixture_principals[case.principal_id]
        compile_domo_query(fixture_catalog.fixture_binding,target,
            QueryRequest(product_id=manifest.product_id,version=manifest.version,inputs=case.fixture.inputs),
            decision(case.principal_id,grant,target),statements[fixture_catalog.fixture_binding.model_bundle_digest],
            source=retained.fixture_source)
    target=PinnedProduct(manifest=manifest,binding_digest=fixture_catalog.consumer_binding.digest,version_id='offline-consumer')
    supported=False
    for principal in identities.principals:
        for grant in principal.grants:
            if grant.policy_id!=manifest.policy_id or 'consumer' not in grant.roles:continue
            try:
                compile_domo_query(fixture_catalog.consumer_binding,target,retained.package.consumer_check.request,
                    decision(principal.id,grant,target),statements[fixture_catalog.consumer_binding.model_bundle_digest],
                    source=retained.consumer_source)
                supported=True
            except GatewayError:pass
    if not supported:raise ValueError('No configured consumer decision supports the exact source-check request')

def _check_recipe(package,binding):
    rows=package.consumer_check.expected_rows;manifest=package.manifest;request=package.consumer_check.request
    if binding.adapter_revision=='domo-named-v2':
        grain=binding.grain_keys;time_field=binding.time_field
    else:
        grain=tuple(name for name,item in binding.fields.items() if item.kind!='measure')
        time_field=next(name for name,item in binding.fields.items() if item.kind=='time')
    bounds=manifest.date_boundaries
    if (len(rows)>manifest.limits.max_rows or len({tuple(r[k] for k in grain) for r in rows})!=len(rows)
        or any(not request.inputs[bounds.start_input]<=r[time_field]<request.inputs[bounds.end_input] for r in rows)):
        raise ValueError('Check recipe exceeds its exact output grain/window')


def load_source_assets(config,root):
    """No sockets, token/secret reads, metadata probes or operator DB actions."""
    try:
        from gateway.adapters.reviewed import parse_binding
        from gateway.adapters.base import check_binding
        from gateway.adapters.bundles import BundleCatalog
        from gateway.publishing.test_runner import parse_fixture_catalog
        sources=SourceCatalog.load(root,config.source_catalog_ref)
        packages=ProductCatalog.load(root,config.product_catalog_ref,sources)
        identities=IdentityMapping.model_validate(read_asset(root,config.identity_mapping_ref))
        validate_source_roles(config,sources.sources)
        validate_identity_mapping(identities,config,packages.packages)
        bindings={};endpoints={};fixtures={};statements={};cube_bundles={};used_sources=set()
        for retained in packages.packages:
            package=retained.package;manifest=package.manifest;raw=retained.assets
            consumer=parse_binding(json_value(raw['consumer_binding_ref']))
            fixture=parse_binding(json_value(raw['fixture_binding_ref']))
            if consumer.adapter_revision not in {'domo-named-v2','cube-bigquery-v1'} or fixture.adapter_revision!=consumer.adapter_revision:
                raise ValueError('Only reviewed serving adapters may be composed')
            check_binding(consumer,manifest,digest=package.consumer_binding_ref.content_digest)
            _check_recipe(package,consumer)
            for binding,source in ((consumer,retained.consumer_source),(fixture,retained.fixture_source)):
                if (binding.source_id,binding.source_revision,binding.source_digest,binding.context)!=(source.source_id,source.revision,source.digest,source.context):
                    raise ValueError('Binding and source identity differ')
                used_sources.add((source.source_id,source.revision))
                key=(binding.binding_id,binding.revision)
                if key in bindings and bindings[key].digest!=binding.digest:raise ValueError('Binding alias')
                bindings[key]=binding
            if retained.consumer_source.provider=='bigquery':
                validate_bigquery_isolation(retained.consumer_source,retained.fixture_source)
            else:
                from gateway.adapters.domo_runtime import validate_domo_source_isolation
                validate_domo_source_isolation(retained.consumer_source,retained.fixture_source)
            if canonical_digest(raw['fixture_equivalence_ref'])!=canonical_digest(raw['fixture_catalog_ref']):
                raise ValueError('This package version requires the exact fixture catalog as its equivalence mapping')
            catalog=parse_fixture_catalog(json_value(raw['fixture_catalog_ref']))
            catalog.approve(manifest,consumer)
            if catalog.consumer_binding.digest!=consumer.digest or catalog.fixture_binding.digest!=fixture.digest:
                raise ValueError('Fixture binding identity differs')
            if (catalog.consumer_source.digest!=retained.consumer_source.digest
                or catalog.fixture_source.digest!=retained.fixture_source.digest):raise ValueError('Fixture source identity differs')
            fixture_manifest=catalog.execution_manifest(manifest)
            check_binding(fixture,fixture_manifest,digest=package.fixture_binding_ref.content_digest)
            seed=PolicySeed.model_validate(raw['policy_seed_ref'])
            rules=seed.content.rules
            if ((rules.tenant_constraint.member,rules.entitlement_constraint.member)!=consumer.constraint_members
                or set(rules.visible_fields)!=set(manifest.outputs)):
                raise ValueError('Policy seed differs from the retained execution shape')
            principals={p.id for p in seed.fixture_principals}
            if {case.principal_id for case in catalog.cases}!=principals:
                raise ValueError('Fixture principal/case inventory differs')
            fixtures[(manifest.product_id,manifest.version,consumer.digest)]=catalog
            bundle_file=BundleFile.model_validate(raw['bundle_catalog_ref'])
            for name,record in bundle_file.endpoints.items():
                if name in endpoints and canonical_digest(endpoints[name])!=canonical_digest(record):raise ValueError('Endpoint alias')
                endpoints[name]=record
            for bundle in bundle_file.cube_bundles:
                cube_bundles[bundle.digest]=bundle
            if bundle_file.statement_catalogs:
                from gateway.adapters.domo_runtime import DomoStatement,DomoStatementCatalog
                for values in bundle_file.statement_catalogs:
                    catalog_row=DomoStatementCatalog([DomoStatement.model_validate(v) for v in values])
                    statements[catalog_row.digest]=catalog_row
        if used_sources!={(s.source_id,s.revision) for s in sources.sources}:raise ValueError('Unreferenced source descriptor')
        bundles=BundleCatalog(json_value(endpoints))
        for binding in bindings.values():
            endpoint=bundles.resolve(binding.model_bundle_digest)
            if (endpoint.adapter_revision,endpoint.context)!=(binding.adapter_revision,binding.context):raise ValueError('Bundle context drift')
            if binding.adapter_revision=='domo-named-v2':
                if (endpoint.source_id,endpoint.source_revision,endpoint.source_digest)!=(binding.source_id,binding.source_revision,binding.source_digest):raise ValueError('Domo bundle source drift')
                catalog=statements[binding.model_bundle_digest]
                for variant in binding.variants:catalog.resolve(variant.statement_id,variant.statement_revision,variant.statement_digest)
            else:
                source=sources.resolve(dict(provider='bigquery',source_id=binding.source_id,revision=binding.source_revision,
                    descriptor_digest=binding.source_digest,context=binding.context))
                if endpoint.url!=source.cube_endpoint or binding.model_bundle_digest!=source.model_bundle_digest:raise ValueError('BigQuery endpoint drift')
                from gateway.adapters.bigquery_runtime import bigquery_security_contract
                from gateway.pilot_config import _parse_json
                bundle=cube_bundles[binding.model_bundle_digest]
                if _parse_json(bundle.security_content.encode())!=bigquery_security_contract(binding):raise ValueError('BigQuery security contract drift')
        if set(cube_bundles)!={b.model_bundle_digest for b in bindings.values() if b.adapter_revision=='cube-bigquery-v1'}:
            raise ValueError('Cube bundle inventory differs')
        catalogs=FixtureCatalogs(fixtures)
        for retained in packages.packages:
            if retained.consumer_source.provider=='domo':
                m=retained.package.manifest
                binding=bindings[(m.binding_id,m.binding_revision)]
                _offline_domo_cases(retained,catalogs.resolve(m,binding),identities,statements)
        return SourceAssets(sources,packages,identities,tuple(bindings.values()),bundles,catalogs,MappingProxyType(statements),MappingProxyType(cube_bundles))
    except (ImportError,ValueError,TypeError,KeyError,AttributeError,GatewayError):
        raise PackageError('SOURCE_COMPOSITION_INVALID') from None

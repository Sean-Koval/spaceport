"""Explicit owner review of fictional weekly pilot assets in disposable tests.

No runtime import uses this helper. It never approves arbitrary products or
variants; callers must supply the fixed weekly sample and its exact case roster.
"""
from gateway.source_access_contracts import SourceApprovalDocument
from gateway.source_access_operator import install_approval
from gateway.contracts.canonical import json_value, canonical_bytes
from gateway.publishing.test_runner import parse_fixture_catalog
from tests.unit.test_source_approval_tooling import synthetic_approval


def weekly_approval(retained):
    catalog = parse_fixture_catalog(json_value(retained.assets['fixture_catalog_ref']))
    from gateway.source_pilot_samples import CASE_PRINCIPALS, weekly_manifest
    manifest = retained.package.manifest
    expected = weekly_manifest(adapter_revision=manifest.adapter_revision, binding_id=manifest.binding_id,
        model_bundle_digest=manifest.model_bundle_digest)
    from gateway.publishing.test_runner import ApprovedFixture
    expected_cases = tuple(ApprovedFixture(fixture=fixture, principal_id=CASE_PRINCIPALS[fixture.id],
        kind='permission_negative' if fixture.expected_error == 'NOT_FOUND_OR_FORBIDDEN' else
            ('input_negative' if fixture.expected_error else 'known_answer'), relative_tolerance='0')
        for fixture in expected.fixtures)
    if (canonical_bytes(catalog.cases) != canonical_bytes(expected_cases)
            or {case.fixture.id: case.principal_id for case in catalog.cases} != CASE_PRINCIPALS
            or canonical_bytes(manifest) != canonical_bytes(expected)):
        raise ValueError('Exact fictional weekly fixture roster required')
    document = synthetic_approval(retained).model_dump(mode='json')
    def audience(identity, actions, *, context='consumer', groups=('core','special'), tenant='north', cases=()):
        return dict(id=identity+'-'+context, context=context, principal_id=identity,
            principal_kind='demo' if cases else 'user', client_ids=[None] if cases else ['reviewed-client'],
            actions=list(actions), tenant_id=tenant,
            row_constraints=[dict(member='Usage.tenant_id', operator='eq', values=[tenant]),
                dict(member='Usage.access_group', operator='in', values=list(groups))],
            visible_fields=['day','account','usage_events'], fixture_case_ids=list(cases))
    audiences = [audience('usage-author', ['author']), audience('usage-publisher', ['publish']),
        audience('usage-consumer-all', ['discover','describe','execute','result']),
        audience('usage-consumer-core', ['discover','describe','execute','result'], groups=('core',)),
        audience('usage-author', ['author'], context='fixture_test')]
    for suffix in ('all','core','denied','south'):
        identity = 'fixture-usage-'+suffix
        audiences.append(audience(identity, ['execute','result'], context='fixture_test',
            groups=('core','special') if suffix=='all' else ('core',),
            tenant='south' if suffix=='south' else 'north',
            cases=tuple(case.fixture.id for case in catalog.cases if case.principal_id==identity)))
    document['audiences'] = audiences
    return SourceApprovalDocument.model_validate(document)


async def install_weekly_approval(owner, assets):
    if len(assets.packages.packages) != 1:
        raise ValueError('One fixed fictional package required')
    retained = assets.packages.packages[0]
    document = weekly_approval(retained)
    return await install_approval(owner, retained, document.canonical_content)


def approval_config(config, assets, receipt):
    """Only the installed immutable reference enters the disposable runtime file."""
    from gateway.source_pilot_config import SourcePilotConfig
    package = assets.packages.packages[0].package
    return SourcePilotConfig.model_validate(config.model_dump(mode='json') | {'source_approval_refs': [dict(
        package_id=package.package_id, revision=package.revision, approval_id=receipt['approval_id'],
        approval_revision=receipt['revision'], approval_digest=receipt['content_digest'])]})

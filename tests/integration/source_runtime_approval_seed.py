"""Fixed fictional Usage approval assets for the standalone Node probe.

This helper is test-owned and never imports credentials or calls a cloud API.
It accepts no arbitrary source or product; every physical descriptor and retained
model contract must match the checked-in synthetic probe before owner approval.
"""
from gateway.contracts.canonical import canonical_bytes, manifest_digest
from gateway.contracts.identity import Principal
from gateway.source_access_contracts import SourceApprovalDocument, physical_source_identity_digest, source_credential_binding_digest

CLIENT = 'source-runtime-test-client'
AUTHOR = 'source-runtime-author'
FIXTURE_BUNDLE_DIGEST = '6449da6d7471e19b48da9b800b99d935252de3f16a12d2ab789c9f46fb14abe3'


def runtime_actor(name):
    from tests.integration.source_runtime_fixture import CASES
    if name not in CASES:
        raise ValueError('Fixed source runtime case required')
    identity = 'source-' + name.replace('_', '-')
    return Principal(id=identity, subject=identity, kind='service', service_client_id=CLIENT)


def runtime_author():
    return Principal(id=AUTHOR, subject=AUTHOR, kind='service', service_client_id=CLIENT)


def fixture_source_profile(source):
    from tests.integration.source_runtime_fixture import source_profile
    if canonical_bytes(source) != canonical_bytes(source_profile()):
        raise ValueError('Exact fictional consumer source required')
    import hashlib
    from tests.integration.source_runtime_fixture import ROOT, _read
    root = ROOT / 'tests/runtime/bigquery-serving-fixture-bundle'
    digest = hashlib.sha256(b'spaceport-cube-bigquery-bundle-v1\0')
    for name in sorted(('security.json', 'model/usage.yml'), key=lambda value: value.encode()):
        data = _read(root / name)
        digest.update(name.encode() + b'\0' + str(len(data)).encode() + b'\0' + data)
    if digest.hexdigest() != FIXTURE_BUNDLE_DIGEST:
        raise ValueError('Static fictional fixture bundle drift')
    return type(source).model_validate(source.model_dump(mode='json') | {
        'source_id': 'synthetic-usage-fixture', 'context': 'fixture_test',
        'data_project': 'fixture-only-project', 'dataset': 'fixture_inputs',
        'cube_endpoint': 'https://fixture-cube.example.test', 'model_bundle_digest': FIXTURE_BUNDLE_DIGEST,
        'auth': {'mode': 'gke-workload-identity', 'expected_principal': 'fixture@fixture-project.iam.gserviceaccount.com'}})


def runtime_approval_assets(source, fixture_source):
    from tests.integration.source_runtime_fixture import CASES, _binding, _manifest, source_profile
    from tests.unit.test_source_approval_contracts import package, domain
    if (canonical_bytes(source) != canonical_bytes(source_profile())
            or canonical_bytes(fixture_source) != canonical_bytes(fixture_source_profile(source))):
        raise ValueError('Exact fictional source pair required')
    consumer = _binding(source)
    fixture = type(consumer).model_validate(consumer.model_dump(mode='json') | {
        'context': 'fixture_test', 'binding_id': 'usage-fixture-binding',
        'source_id': fixture_source.source_id, 'source_revision': fixture_source.revision,
        'source_digest': fixture_source.digest, 'model_bundle_digest': fixture_source.model_bundle_digest})
    manifest = _manifest(consumer)
    document, _ = package.__wrapped__(manifest.model_dump(mode='json'))
    document['product'] = dict(product_id=manifest.product_id, version=manifest.version,
        manifest_digest=manifest_digest(manifest), policy_id=manifest.policy_id)
    document['artifacts'].update(model_bundle_digest=consumer.model_bundle_digest,
        fixture_model_bundle_digest=fixture.model_bundle_digest)
    lineage = []
    for name, binding, profile in (('consumer', consumer, source), ('fixture', fixture, fixture_source)):
        document['bindings'][name] = dict(binding_id=binding.binding_id, revision=binding.revision, content_digest=binding.digest)
        document['sources'][name] = dict(source_id=profile.source_id, revision=profile.revision,
            provider=profile.provider, context=profile.context, descriptor_digest=profile.digest,
            physical_identity_digest=physical_source_identity_digest(profile),
            credential_binding_digest=source_credential_binding_digest(profile))
        for field, mapped in binding.fields.items():
            for purpose in ('exposed',) if mapped.kind == 'measure' else ('exposed', 'grouping'):
                lineage.append(dict(id=field, source_context=binding.context, source_member=mapped.member,
                    purpose=purpose, classification='non-personal'))
        for member in binding.constraint_members:
            lineage.append(dict(id=member, source_context=binding.context, source_member=member,
                purpose='hidden_scope', classification='non-personal'))
        for field in manifest.inputs:
            member = binding.fields[binding.input_fields[field]].member if field in binding.input_fields else 'Usage.day'
            lineage.append(dict(id=field, source_context=binding.context, source_member=member,
                purpose='source_input', classification='non-personal'))
    document['lineage'] = lineage
    document['input_domains'] = [domain('start', 'date', minimum='2024-01-01', maximum='2025-01-02'),
        domain('end', 'date', minimum='2024-01-01', maximum='2025-01-02'),
        domain('account', 'string', values=('A', 'B'), nullable=True)]
    document['filter_domains'] = [domain('account', 'string', values=('A', 'B'), nullable=True),
        domain('day', 'date', minimum='2024-01-01', maximum='2025-01-02')]
    document['audiences'] = [dict(id=actor.id, context='consumer', principal_id=actor.id,
        principal_kind=actor.kind, client_ids=[actor.service_client_id], actions=['execute', 'result'],
        tenant_id='north', row_constraints=[dict(member='Usage.tenant', operator='eq', values=['north']),
            dict(member='Usage.group', operator='in', values=['north'])],
        visible_fields=['day', 'account', 'usage_events'], fixture_case_ids=[]) for actor in map(runtime_actor, CASES)]
    document['review'].update(review_id='synthetic-runtime-review', purpose='synthetic-usage',
        evidence_ref='checked-in-source-runtime-probe')
    # The publication helper adds the exact author/fixed-case roster and catalog
    # digest before installing the final owner review. This intermediate value
    # is validated only; it is never installed or treated as authority.
    validated = SourceApprovalDocument.model_validate(document)
    validated.package_pins(manifest)
    return document, manifest, (consumer, fixture), (source, fixture_source)

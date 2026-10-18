"""Operator review is explicit; trusted synthetic scope never comes from JSON."""
import asyncio
from dataclasses import replace
import importlib
from types import SimpleNamespace

import pytest

from gateway.contracts.execution import PinnedProduct
from gateway.registry.repository import Registry
from gateway import runtime_config

pytestmark = pytest.mark.unit


def selector():
    assert hasattr(runtime_config, 'source_approval_required'), 'Required-mode selector is missing'
    return runtime_config.source_approval_required


def registry():
    return Registry('postgresql+psycopg://gateway_app:synthetic@registry-db:5432/gateway_registry')


def pinned(assets, context='consumer'):
    manifest = assets.manifest if context == 'consumer' else assets.fixture_catalog.execution_manifest(assets.manifest)
    binding = next(b for b in assets.bindings if b.context == context)
    return PinnedProduct(manifest=manifest, binding_digest=binding.digest, version_id='synthetic-test')


@pytest.mark.parametrize('profile', ['legacy', 'reviewed'])
def test_every_adapter_requires_approval_without_trusted_composition(profile):
    assets = runtime_config.load_assets() if profile == 'legacy' else importlib.import_module('gateway.reviewed_runtime').load_assets()
    assert selector()(pinned(assets)) is True
    assert selector()(pinned(assets), synthetic_capability={'synthetic': True}) is True


@pytest.mark.parametrize('profile', ['legacy', 'reviewed'])
def test_capability_is_bound_to_exact_registry_context_and_pins(profile):
    assets = runtime_config.load_assets() if profile == 'legacy' else importlib.import_module('gateway.reviewed_runtime').load_assets()
    db, other = registry(), registry()
    capability = runtime_config._synthetic_capability(assets, db, profile=profile)
    try:
        for context in ('consumer', 'fixture_test'):
            target = pinned(assets, context)
            assert selector()(target, registry=db, context=context, synthetic_capability=capability) is False
            assert selector()(target, registry=other, context=context, synthetic_capability=capability) is True
            changed = target.model_copy(update={'binding_digest': 'f' * 64})
            assert selector()(changed, registry=db, context=context, synthetic_capability=capability) is True
        assert selector()(pinned(assets), registry=db, context='fixture_test', synthetic_capability=capability) is True
        runtime_config._revoke_synthetic_capability(capability)
        assert selector()(pinned(assets), registry=db, synthetic_capability=capability) is True
    finally:
        asyncio.run(db.close()); asyncio.run(other.close())


def test_factory_refuses_changed_assets_untrusted_registry_and_endpoint_override():
    from gateway.adapters.bundles import BundleCatalog
    assets = importlib.import_module('gateway.reviewed_runtime').load_assets()
    db = registry()
    records = {b.context: assets.bundles.resolve(b.model_bundle_digest).model_dump() for b in assets.bindings}
    records['consumer']['url'] = 'http://127.0.0.1:4000'
    try:
        with pytest.raises(ValueError):
            runtime_config._synthetic_capability(replace(assets, bundles=BundleCatalog(records)), db, profile='reviewed')
        with pytest.raises(ValueError):
            runtime_config._synthetic_capability(assets, SimpleNamespace(sessions=None), profile='reviewed')
        with pytest.raises(ValueError):
            runtime_config._synthetic_capability(replace(assets, manifest=assets.manifest.model_copy(update={'name': 'Changed'})), db, profile='reviewed')
    finally:
        asyncio.run(db.close())


@pytest.mark.parametrize('field', ['binding_digest', 'context', 'unknown', 'nested_unknown'])
def test_selector_revalidates_untrusted_values_without_invoking_hooks(field):
    assets = runtime_config.load_assets(); db = registry(); calls = []
    capability = runtime_config._synthetic_capability(assets, db, profile='legacy')
    target = pinned(assets); context = 'consumer'
    class Forged:
        def __eq__(self, other):
            calls.append('eq'); return True
        def __hash__(self):
            calls.append('hash'); return hash('consumer')
    if field == 'binding_digest': object.__setattr__(target, field, Forged())
    if field == 'context': context = Forged()
    if field == 'unknown': target.__dict__['undeclared'] = 'ignored'
    if field == 'nested_unknown': target.manifest.__dict__['undeclared'] = 'ignored'
    try:
        assert selector()(target, registry=db, context=context, synthetic_capability=capability) is True
        assert calls == []
    finally:
        asyncio.run(db.close())


def test_factory_rejects_model_subclasses_before_serialization_or_digest_hooks():
    from gateway.adapters.base import CubeBinding
    assets = runtime_config.load_assets(); original = assets.bindings[0]; db = registry(); calls = []
    class AlteredBinding(CubeBinding):
        def model_dump(self, *args, **kwargs):
            calls.append('model_dump'); return original.model_dump(*args, **kwargs)
        @property
        def digest(self):
            calls.append('digest'); return 'f' * 64
    altered = AlteredBinding.model_construct(**original.__dict__)
    try:
        with pytest.raises(ValueError):
            runtime_config._synthetic_capability(replace(assets, bindings=(altered, assets.bindings[1])), db, profile='legacy')
        assert calls == []
    finally:
        asyncio.run(db.close())


def test_selector_turns_invalid_manifest_contract_into_required_mode():
    assets = runtime_config.load_assets(); db = registry()
    capability = runtime_config._synthetic_capability(assets, db, profile='legacy')
    manifest = assets.manifest.model_copy(deep=True)
    manifest.examples[0].__dict__['inputs'] = {'unexpected': 'value'}
    manifest.__dict__['content_digest'] = None
    target = PinnedProduct.model_construct(manifest=manifest, binding_digest=assets.bindings[0].digest, version_id='synthetic-test')
    try:
        assert selector()(target, registry=db, synthetic_capability=capability) is True
    finally:
        asyncio.run(db.close())


@pytest.mark.parametrize('field', ['profile', 'manifest_value', 'bundle', 'account_value'])
def test_factory_never_invokes_hostile_input_hooks(field):
    assets = runtime_config.load_assets(); db = registry(); calls = []
    class Hostile(str):
        def __eq__(self, other): calls.append('eq'); return True
        def __hash__(self): calls.append('hash'); return 1
    profile = 'legacy'
    if field == 'profile': profile = Hostile('legacy')
    if field == 'manifest_value': assets.manifest.__dict__['name'] = Hostile(assets.manifest.name)
    if field == 'account_value': assets.accounts[0]['id'] = Hostile('demo-north')
    if field == 'bundle':
        from gateway.adapters.bundles import BundleCatalog
        class HostileCatalog(BundleCatalog):
            def resolve(self, digest): calls.append('resolve'); return assets.bundles.resolve(digest)
        changed = object.__new__(HostileCatalog)
        changed._entries = assets.bundles._entries
        assets = replace(assets, bundles=changed)
    try:
        with pytest.raises(ValueError): runtime_config._synthetic_capability(assets, db, profile=profile)
        assert calls == []
    finally:
        asyncio.run(db.close())


def configured_assets(tmp_path, provider='bigquery'):
    from scripts.spaceport_setup import template, build
    from tests.unit.test_source_pilot_builder import write_files
    from gateway.source_pilot_config import SourcePilotConfig
    from gateway.source_pilot_assets import load_source_assets
    files = build(template(provider).model_dump(mode='json'))
    write_files(tmp_path, files)
    config = SourcePilotConfig.model_validate(files['pilot.json'])
    return config, load_source_assets(config, tmp_path)


def test_offline_review_packet_is_structural_and_never_claims_approval(tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(socket, 'socket', lambda *a, **k: pytest.fail('offline network I/O'))
    config, assets = configured_assets(tmp_path)
    from gateway.source_access_operator import build_review_request
    packet = build_review_request(assets.packages.packages[0])
    assert packet['status'] == 'requires-owner-review'
    assert packet['approval_state'] == 'unapproved'
    assert packet['lineage_inventory']
    assert all('classification' not in row for row in packet['lineage_inventory'])
    assert set(packet['required_owner_review']) == {'lineage', 'input_domains', 'filter_domains', 'audiences', 'review', 'expiry'}
    assert 'expected_rows' not in str(packet) and 'client_secret_ref' not in str(packet)


def test_source_approval_refs_are_offline_optional_but_serving_requires_complete_inventory(tmp_path):
    config, assets = configured_assets(tmp_path)
    from gateway.source_access_operator import validate_configured_approvals
    assert config.source_approval_refs == ()
    with pytest.raises(ValueError, match='Source approval configuration is unavailable'):
        validate_configured_approvals(config, assets, tmp_path)


@pytest.mark.parametrize('change', ['duplicate', 'unknown', 'fraction', 'bool'])
def test_source_approval_ref_shape_is_closed_and_exact(change):
    from tests.unit.test_source_pilot_config import config
    from gateway.source_pilot_config import SourcePilotConfig
    value = config()
    ref = {'package_id': 'weekly-usage', 'revision': 1, 'approval_id': '11111111-1111-1111-1111-111111111111', 'approval_revision': 1, 'approval_digest': 'a'*64}
    value['source_approval_refs'] = [ref]
    if change == 'duplicate': value['source_approval_refs'].append(ref.copy())
    if change == 'unknown': ref['active'] = True
    if change == 'fraction': ref['revision'] = 1.5
    if change == 'bool': ref['revision'] = True
    with pytest.raises(ValueError): SourcePilotConfig.model_validate(value)


def test_factory_rejects_proxy_with_hostile_backing_without_hooks():
    from types import MappingProxyType
    assets = runtime_config.load_assets(); db = registry(); calls = []
    class HostileDict(dict):
        def __len__(self): calls.append('len'); return super().__len__()
        def __iter__(self): calls.append('iter'); return super().__iter__()
        def __getitem__(self, key): calls.append('getitem'); return super().__getitem__(key)
    assets.bundles._entries = MappingProxyType(HostileDict(assets.bundles._entries))
    try:
        with pytest.raises(ValueError): runtime_config._synthetic_capability(assets, db, profile='legacy')
        assert calls == []
    finally:
        asyncio.run(db.close())


def synthetic_approval(retained):
    from gateway.source_access_operator import _structural_pins
    from gateway.source_access_contracts import SourceApprovalDocument
    pins, inventory = _structural_pins(retained)
    return SourceApprovalDocument.model_validate({**pins, 'schema_version': 1, 'mode': 'shared_non_pii',
        'revision': 1, 'supersedes_id': None, 'expires_at': '2030-01-01T00:00:00Z',
        'lineage': [dict(row, classification='non-personal') for row in inventory],
        'input_domains': [dict(name=name, kind='range', type='date', values=[], minimum='2026-10-01',
            maximum='2026-11-01', nullable=False, classification='non-personal') for name in ('start', 'end')] +
            [dict(name='account', kind='enum', type='string', values=['acct-a', 'acct-b'],
                minimum=None, maximum=None, nullable=True, classification='non-personal')],
        'filter_domains': [],
        'audiences': [dict(id='synthetic-consumer', context='consumer', principal_id='usage-consumer-all',
            principal_kind='user', client_ids=['reviewed-client'], actions=['execute','result'], tenant_id='north',
            row_constraints=[dict(member='Usage.tenant_id', operator='eq', values=['north']),
                dict(member='Usage.access_group', operator='in', values=['core', 'special'])],
            visible_fields=['day', 'account', 'usage_events'], fixture_case_ids=[])],
        'review': dict(review_id='synthetic-review', operator_id='synthetic-owner', evidence_ref='synthetic-receipt',
            evidence_digest='e'*64, purpose='synthetic-test-only', lineage_reviewed=True,
            source_publication_controlled=True, shared_identity_approved=True, inference_risk_reviewed=True)})


def with_approval(config, assets, tmp_path, identity='11111111-1111-1111-1111-111111111111'):
    from gateway.source_pilot_config import SourcePilotConfig
    approval = synthetic_approval(assets.packages.packages[0])
    package = assets.packages.packages[0].package
    return SourcePilotConfig.model_validate(config.model_dump(mode='json') | {'source_approval_refs': [dict(
        package_id=package.package_id, revision=package.revision, approval_id=identity,
        approval_revision=approval.revision, approval_digest=approval.content_digest)]}), approval


def test_complete_static_review_is_not_registry_authority(tmp_path):
    config, assets = configured_assets(tmp_path)
    config, approval = with_approval(config, assets, tmp_path)
    from gateway.source_access_operator import validate_configured_approvals
    pins = validate_configured_approvals(config, assets, tmp_path)
    assert len(pins) == 1 and pins[0][0].approval_digest == approval.content_digest


@pytest.mark.parametrize('field', ['manifest_digest', 'physical_identity', 'fixture_catalog', 'lineage', 'domain'])
def test_approval_cannot_borrow_other_package_or_incomplete_inventory(tmp_path, field):
    from gateway.source_access_operator import validate_package_approval
    from gateway.source_access_contracts import SourceApprovalDocument
    config, assets = configured_assets(tmp_path)
    retained = assets.packages.packages[0]
    approval = synthetic_approval(retained).model_dump(mode='json')
    if field == 'manifest_digest': approval['product']['manifest_digest'] = 'f'*64
    if field == 'physical_identity': approval['sources']['consumer']['physical_identity_digest'] = 'f'*64
    if field == 'fixture_catalog': approval['artifacts']['fixture_catalog_digest'] = 'f'*64
    if field == 'lineage': approval['lineage'].pop()
    if field == 'domain': approval['input_domains'].pop()
    changed = SourceApprovalDocument.model_validate(approval)
    with pytest.raises(ValueError, match='Source approval configuration is unavailable'):
        validate_package_approval(retained, changed)


@pytest.mark.parametrize('change', ['hostile_engine', 'rewritten_url', 'session_bind', 'pool'])
def test_factory_requires_original_inert_registry_construction(change):
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool
    assets = runtime_config.load_assets(); db = registry(); calls = []
    production = Registry('postgresql+psycopg://gateway_app:synthetic@production.example/organization')
    original_engine = db.engine
    if change == 'hostile_engine':
        class Hostile:
            @property
            def url(self): calls.append('url'); return original_engine.url
        db.engine = Hostile()
    if change == 'rewritten_url':
        production.engine.sync_engine.url = original_engine.url
        db = production
    if change == 'session_bind': db.sessions.kw['bind'] = production.engine
    if change == 'pool': db.engine.sync_engine.pool = NullPool(lambda: None)
    try:
        with pytest.raises(ValueError): runtime_config._synthetic_capability(assets, db, profile='legacy')
        assert calls == []
    finally:
        if change == 'hostile_engine': db.engine = original_engine
        asyncio.run(db.close())
        if production is not db: asyncio.run(production.close())


@pytest.mark.parametrize('change', ['engine', 'url', 'session_bind', 'pool'])
def test_selector_rechecks_registry_provenance_after_capability_minted(change):
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool
    assets = runtime_config.load_assets(); db = registry(); original_engine = db.engine
    capability = runtime_config._synthetic_capability(assets, db, profile='legacy')
    if change == 'engine': db.engine = object()
    if change == 'url': db.engine.sync_engine.url = make_url('postgresql+psycopg://gateway_app:synthetic@production.example/organization')
    if change == 'session_bind': db.sessions.kw['bind'] = None
    if change == 'pool': db.engine.sync_engine.pool = NullPool(lambda: None)
    try:
        assert selector()(pinned(assets), registry=db, synthetic_capability=capability) is True
    finally:
        db.engine = original_engine
        asyncio.run(db.close())


@pytest.mark.parametrize('profile', ['legacy', 'reviewed'])
def test_only_exact_runtime_factories_pass_scoped_capability_to_policy(profile, monkeypatch):
    module = importlib.import_module('gateway.runtime' if profile == 'legacy' else 'gateway.reviewed_runtime')
    db = registry(); assets = runtime_config.load_assets() if profile == 'legacy' else module.load_assets()
    captured = []
    class StopComposition(Exception): pass
    def policy(*args, **kwargs):
        token = kwargs['synthetic_capability']
        captured.append(token)
        assert selector()(pinned(assets), registry=db, synthetic_capability=token) is False
        raise StopComposition
    monkeypatch.setattr(module, 'PolicyService', policy)
    try:
        with pytest.raises(StopComposition):
            if profile == 'legacy':
                monkeypatch.setattr(module, 'Registry', lambda _: db)
                monkeypatch.setattr(module, 'database_url', lambda _: 'unused')
                monkeypatch.setattr(module, 'secret_key', lambda _: b'a'*32)
                module.build_runtime()
            else:
                module.build_runtime(registry=db, authenticator=object(), secret_resolver=lambda _: 'unused',
                    audit_key=b'a'*32, cursor_key=b'b'*32, assets=assets)
        assert len(captured) == 1
    finally:
        asyncio.run(db.close())


@pytest.mark.parametrize('after_mint', [False, True])
@pytest.mark.parametrize('option', ['binds', 'sync_session_class', 'unknown'])
def test_registry_provenance_rejects_per_mapper_or_session_option_overrides(after_mint, option):
    from gateway.registry.models import ProductRow
    assets = runtime_config.load_assets(); db = registry(); calls = []
    production = Registry('postgresql+psycopg://gateway_app:synthetic@production.example/organization')
    class HostileMeta(type):
        def __eq__(self, other): calls.append('eq'); return True
        def __hash__(self): calls.append('hash'); return 1
    class SessionOverride(metaclass=HostileMeta): pass
    capability = runtime_config._synthetic_capability(assets, db, profile='legacy') if after_mint else None
    value = {ProductRow: production.engine} if option == 'binds' else SessionOverride
    db.sessions.configure(**{option: value})
    try:
        if after_mint:
            assert selector()(pinned(assets), registry=db, synthetic_capability=capability) is True
        else:
            with pytest.raises(ValueError): runtime_config._synthetic_capability(assets, db, profile='legacy')
        assert calls == []
    finally:
        asyncio.run(db.close()); asyncio.run(production.close())


class OwnerRegistrySpy:
    def __init__(self, response, *, fail_commit=False):
        self.response = response; self.fail_commit = fail_commit; self.events = []
    def transaction(self):
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def transaction():
            self.events.append('begin')
            async def scalar(statement, parameters):
                self.events.append((str(statement), parameters)); return self.response
            yield SimpleNamespace(session=SimpleNamespace(scalar=scalar))
            if self.fail_commit: raise RuntimeError('private-server-content')
            self.events.append('commit')
        return transaction()


def test_owner_import_returns_only_sanitized_receipt_after_commit(tmp_path):
    from gateway.source_access_operator import install_approval
    _, assets = configured_assets(tmp_path)
    retained = assets.packages.packages[0]; approval = synthetic_approval(retained)
    db = OwnerRegistrySpy(dict(id='11111111-1111-1111-1111-111111111111', revision=1,
        content_digest=approval.content_digest, state='active', replayed=False,
        created_at='2026-10-15T00:00:00+00:00', expires_at=approval.expires_at))
    receipt = asyncio.run(install_approval(db, retained, approval.canonical_content))
    assert db.events[-1] == 'commit'
    assert receipt == dict(schema_version=1, operation='import', approval_id='11111111-1111-1111-1111-111111111111',
        revision=1, content_digest=approval.content_digest, state='active', replayed=False)
    assert 'source_approval_install' in db.events[1][0]
    assert 'review' not in receipt and 'audiences' not in receipt


def test_owner_commit_failure_has_no_success_receipt_or_private_error(tmp_path):
    from gateway.source_access_operator import install_approval, OperatorError
    _, assets = configured_assets(tmp_path); retained = assets.packages.packages[0]
    approval = synthetic_approval(retained)
    db = OwnerRegistrySpy(dict(id='11111111-1111-1111-1111-111111111111', revision=1,
        content_digest=approval.content_digest, state='active', replayed=False,
        created_at='2026-10-15T00:00:00Z', expires_at=approval.expires_at), fail_commit=True)
    with pytest.raises(OperatorError) as failure: asyncio.run(install_approval(db, retained, approval.canonical_content))
    assert failure.value.uncertain is True and 'private-server-content' not in str(failure.value)
    assert 'commit' not in db.events


def test_owner_revoke_is_exact_and_never_reimports():
    from gateway.source_access_operator import revoke_approval
    identity = '11111111-1111-1111-1111-111111111111'
    db = OwnerRegistrySpy(dict(id=identity, state='revoked', state_revision=2, replayed=False))
    result = asyncio.run(revoke_approval(db, identity, 'a'*64, 'operator_revoked'))
    assert result['state'] == 'revoked' and result['approval_id'] == identity
    assert 'source_approval_revoke' in db.events[1][0] and db.events[-1] == 'commit'
    assert 'source_approval_install' not in str(db.events)


def test_owner_invalid_document_never_opens_transaction(tmp_path):
    from gateway.source_access_operator import install_approval, OperatorError
    _, assets = configured_assets(tmp_path); db = OwnerRegistrySpy(None)
    with pytest.raises(OperatorError): asyncio.run(install_approval(db, assets.packages.packages[0], b'{"secret":"private"}'))
    assert db.events == []


@pytest.mark.parametrize('target', ['registry', 'sessions', 'sync_engine', 'pool'])
def test_registry_metadata_keys_reject_hostile_string_subclasses_without_lookup(target):
    assets = runtime_config.load_assets(); db = registry(); calls = []
    class HostileKey(str):
        def __eq__(self, other): calls.append('eq'); return str.__eq__(self, other)
        def __hash__(self): return str.__hash__(self)
    owner, key = {'registry': (db, 'engine'), 'sessions': (db.sessions, 'kw'),
        'sync_engine': (db.engine.sync_engine, 'pool'), 'pool': (db.engine.pool, '_creator_arg')}[target]
    value = owner.__dict__.pop(key); owner.__dict__[HostileKey(key)] = value; calls.clear()
    try:
        with pytest.raises(ValueError): runtime_config._synthetic_capability(assets, db, profile='legacy')
        assert calls == []
    finally:
        owner.__dict__.pop(HostileKey(key)); owner.__dict__[key] = value
        asyncio.run(db.close())


def test_explicit_cli_review_request_is_private_offline_and_unapproved(tmp_path, capsys):
    from scripts.source_access_approvals import main
    config, assets = configured_assets(tmp_path)
    output = tmp_path/'review-request.json'
    package = assets.packages.packages[0].package
    assert main(['review-request', '--config', str(tmp_path/'pilot.json'), '--package-id', package.package_id,
        '--revision', str(package.revision), '--output', str(output)]) == 0
    import json
    packet = json.loads(output.read_text())
    assert packet['approval_state'] == 'unapproved' and output.stat().st_mode & 0o077 == 0
    stdout = capsys.readouterr().out
    assert 'audiences' not in stdout and 'source_id' not in stdout and 'requires-owner-review' in stdout


def test_cli_canonicalize_validates_without_installing_and_rejects_duplicate_keys(tmp_path, capsys):
    from scripts.source_access_approvals import main
    import json
    _, assets = configured_assets(tmp_path)
    approval = synthetic_approval(assets.packages.packages[0]); source = tmp_path/'reviewed.json'
    source.write_text(json.dumps(approval.model_dump(mode='json'), indent=2)); source.chmod(0o600)
    output = tmp_path/'canonical.json'
    assert main(['canonicalize', '--approval-file', str(source), '--output', str(output)]) == 0
    assert output.read_bytes() == approval.canonical_content
    assert 'unapproved' in capsys.readouterr().out
    bad = tmp_path/'bad.json'; bad.write_text('{"schema_version":1,"schema_version":1}'); bad.chmod(0o600)
    assert main(['canonicalize', '--approval-file', str(bad), '--output', str(tmp_path/'bad-output.json')]) == 2
    assert not (tmp_path/'bad-output.json').exists()


def test_domo_offline_request_does_not_invent_provider_versions(tmp_path):
    _, assets = configured_assets(tmp_path, provider='domo')
    from gateway.source_access_operator import build_review_request, _structural_pins
    retained = assets.packages.packages[0]
    packet = build_review_request(retained)
    assert packet['structural_pins']['sources']['consumer']['credential_binding_digest'] is None
    assert 'projects/' not in str(packet)
    with pytest.raises(ValueError): _structural_pins(retained)


def test_runtime_config_accepts_only_immutable_refs_never_owner_doc_paths():
    from tests.unit.test_source_pilot_config import config
    from gateway.source_pilot_config import SourcePilotConfig
    value = config(); value['source_approval_refs'] = [dict(package_id='usage',revision=1,
        approval_id='11111111-1111-1111-1111-111111111111',approval_revision=1,approval_digest='a'*64)]
    checked = SourcePilotConfig.model_validate(value)
    assert set(checked.source_approval_refs[0].model_dump()) == {
        'package_id','revision','approval_id','approval_revision','approval_digest'}
    for key in ('document_ref', 'pins_ref', 'audiences', 'review'):
        bad = {**value,'source_approval_refs':[{**value['source_approval_refs'][0],key:'private-owner-document'}]}
        with pytest.raises(ValueError): SourcePilotConfig.model_validate(bad)


def test_reference_cli_is_explicit_and_requires_installed_identity():
    from scripts.spaceport import parser
    args = parser().parse_args(['approval','reference','--config','pilot.json','--package-id','usage','--revision','1',
        '--approval-file','operator-review.json','--approval-id','11111111-1111-1111-1111-111111111111','--output','runtime-ref.json'])
    assert args.action == 'reference' and args.approval_id == '11111111-1111-1111-1111-111111111111'


def test_static_approval_ref_revalidation_rejects_hidden_model_extras(tmp_path):
    from gateway.source_access_operator import validate_configured_approvals
    config, assets = configured_assets(tmp_path)
    config, _ = with_approval(config, assets, tmp_path)
    object.__setattr__(config.source_approval_refs[0], '__pydantic_extra__', {'active': True})
    with pytest.raises(ValueError, match='Source approval configuration is unavailable'):
        validate_configured_approvals(config, assets)


@pytest.mark.parametrize('change', ['unknown', 'package_id', 'revision', 'approval_revision', 'duplicate'])
def test_projection_loader_revalidates_exact_refs_before_registry_io(tmp_path, change):
    from gateway.source_access_operator import load_registry_approvals, validate_configured_approvals
    config, assets = configured_assets(tmp_path)
    config, approval = with_approval(config, assets, tmp_path)
    configured = validate_configured_approvals(config, assets)
    ref, retained = configured[0]
    if change == 'unknown': object.__setattr__(ref, '__pydantic_extra__', {'active': True})
    if change == 'package_id': ref.__dict__['package_id'] = 'different-package'
    if change == 'revision': ref.__dict__['revision'] += 1
    if change == 'approval_revision': ref.__dict__['approval_revision'] = True
    if change == 'duplicate': configured = configured + configured
    db = OwnerRegistrySpy(approval.runtime_projection(ref.approval_id).model_dump(mode='json'))
    with pytest.raises(ValueError, match='Source approval configuration is unavailable'):
        asyncio.run(load_registry_approvals(db, configured))
    assert db.events == []

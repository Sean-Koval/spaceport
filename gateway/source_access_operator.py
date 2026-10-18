"""Explicit offline source review and protected owner operations.

Generated requests are unapproved. Neither parsing nor package construction
installs authority, reads provider credentials or proves upstream identity.
"""
from gateway.contracts.canonical import canonical_bytes, canonical_digest, json_value, manifest_digest
from gateway.contracts.errors import GatewayError
from gateway.source_access_contracts import (MAX_APPROVAL_BYTES, ReviewedPackagePins, RuntimeApprovalProjection, PackageApprovalFields,
    expected_binding_inventory, physical_source_identity_digest, source_credential_binding_digest, validate_source_approval, _fresh)

_ERROR = 'Source approval configuration is unavailable'


def _structural_pins(retained, *, require_credentials=True):
    from gateway.adapters.reviewed import parse_binding
    from gateway.publishing.test_runner import parse_fixture_catalog
    package = retained.package
    manifest = package.manifest
    consumer = parse_binding(json_value(retained.assets['consumer_binding_ref']))
    fixture = parse_binding(json_value(retained.assets['fixture_binding_ref']))
    catalog = parse_fixture_catalog(json_value(retained.assets['fixture_catalog_ref']))
    catalog.approve(manifest, consumer)
    provider = retained.consumer_source.provider
    if provider not in {'bigquery', 'domo'}:
        raise ValueError(_ERROR)
    def binding_pin(binding):
        return dict(binding_id=binding.binding_id, revision=binding.revision, content_digest=binding.digest)
    def source_pin(source):
        try:
            credential_digest = source_credential_binding_digest(source)
        except ValueError:
            if require_credentials:
                raise
            credential_digest = None
        return dict(source_id=source.source_id, revision=source.revision, provider=source.provider,
            context=source.context, descriptor_digest=source.digest,
            physical_identity_digest=physical_source_identity_digest(source), credential_binding_digest=credential_digest)
    return {
        'product': dict(product_id=manifest.product_id, version=manifest.version,
            manifest_digest=manifest_digest(manifest), policy_id=manifest.policy_id),
        'bindings': dict(consumer=binding_pin(consumer), fixture=binding_pin(fixture)),
        'sources': dict(consumer=source_pin(retained.consumer_source), fixture=source_pin(retained.fixture_source)),
        'artifacts': dict(model_bundle_digest=consumer.model_bundle_digest,
            fixture_model_bundle_digest=fixture.model_bundle_digest,
            statement_catalog_digest=consumer.model_bundle_digest if provider == 'domo' else None,
            fixture_statement_catalog_digest=fixture.model_bundle_digest if provider == 'domo' else None,
            fixture_catalog_digest=canonical_digest(catalog), schema_profile='result-v1',
            normalization_profile='domo-query-positional-v1' if provider == 'domo' else 'cube-v1'),
    }, tuple(expected_binding_inventory(consumer, tuple(manifest.inputs))) + tuple(
        expected_binding_inventory(fixture, tuple(manifest.inputs)))


def build_review_request(retained):
    """Derive structural inventory only; never infer classifications or lineage."""
    try:
        pins, inventory = _structural_pins(retained, require_credentials=False)
        return dict(schema_version=1, status='requires-owner-review', approval_state='unapproved',
            configured_package_digest=retained.package.digest, structural_pins=pins,
            lineage_inventory=list(inventory),
            required_owner_review=['lineage', 'input_domains', 'filter_domains', 'audiences', 'review', 'expiry'],
            source_observation='unverified', provider_identity='unverified')
    except (ValueError, TypeError, AttributeError, KeyError, GatewayError):
        raise ValueError(_ERROR) from None


def validate_package_pins(retained, pins):
    """Runtime files contain only exact immutable pins, never owner evidence."""
    try:
        pins = _fresh(pins, ReviewedPackagePins)
        if pins.approval_digest is None:
            raise ValueError
        expected, inventory = _structural_pins(retained)
        for field, value in expected.items():
            if canonical_bytes(getattr(pins, field)) != canonical_bytes(value):
                raise ValueError
        approved = [entry.model_dump(exclude={'classification'}) for entry in pins.lineage]
        key = lambda entry: (entry['source_context'], entry['purpose'], entry['id'], entry['source_member'])
        if sorted(approved, key=key) != sorted(inventory, key=key):
            raise ValueError
        if canonical_bytes(pins.manifest) != canonical_bytes(retained.package.manifest):
            raise ValueError
        return pins
    except (ValueError, TypeError, AttributeError, KeyError, GatewayError):
        raise ValueError(_ERROR) from None


def validate_package_approval(retained, approval):
    """Owner-only review document is stripped before any serving composition."""
    try:
        approval = validate_source_approval(approval.canonical_content)
        pins = approval.package_pins(retained.package.manifest, approval_digest=approval.content_digest)
        return validate_package_pins(retained, pins)
    except (ValueError, TypeError, AttributeError, KeyError, GatewayError):
        raise ValueError(_ERROR) from None


def _fresh_reference(original):
    from gateway.source_pilot_config import SourceApprovalRef
    if type(original) is not SourceApprovalRef:
        raise ValueError(_ERROR)
    data, extra = original.__dict__, original.__pydantic_extra__
    if (type(data) is not dict or any(type(key) is not str for key in data)
        or set(data) != set(SourceApprovalRef.model_fields)
        or extra is not None and (type(extra) is not dict or len(extra) != 0)
        or any(type(data[key]) is not str for key in ('package_id', 'approval_id', 'approval_digest'))):
        raise ValueError(_ERROR)
    return SourceApprovalRef.model_validate(dict(data))


def validate_configured_approvals(config, assets, root=None):
    """Static all-package/ref/credential coverage, before any registry credential.

    Configuration contains only immutable references. The app obtains the
    closed runtime projection from protected registry state, never owner docs.
    """
    try:
        refs = {}
        if type(config.source_approval_refs) is not tuple or not 1 <= len(config.source_approval_refs) <= 25:
            raise ValueError
        for original in config.source_approval_refs:
            ref = _fresh_reference(original)
            key = (ref.package_id, ref.revision)
            if key in refs:
                raise ValueError
            refs[key] = ref
        packages = {(row.package.package_id, row.package.revision): row for row in assets.packages.packages}
        if not packages or set(refs) != set(packages):
            raise ValueError
        result = []
        for key, retained in packages.items():
            _structural_pins(retained)  # Includes required immutable provider credential versions.
            result.append((refs[key], retained))
        return tuple(result)
    except (ValueError, TypeError, AttributeError, KeyError, OSError, GatewayError):
        raise ValueError(_ERROR) from None


async def load_registry_approvals(registry, configured):
    """Read only app-safe projections, check exact refs/assets, then one batch."""
    from sqlalchemy import text
    try:
        if type(configured) is not tuple or not 1 <= len(configured) <= 25:
            raise ValueError
        checked, identities, package_keys = [], set(), set()
        for row in configured:
            if type(row) is not tuple or len(row) != 2:
                raise ValueError
            ref, retained = _fresh_reference(row[0]), row[1]
            key = (ref.package_id, ref.revision)
            if (key != (retained.package.package_id, retained.package.revision)
                or key in package_keys or ref.approval_id in identities):
                raise ValueError
            package_keys.add(key); identities.add(ref.approval_id)
            checked.append((ref, retained))
        packages = []
        async with registry.transaction() as uow:
            for ref, retained in checked:
                value = await uow.session.scalar(text('SELECT gateway.source_approval_read(CAST(:id AS uuid))'), {'id': ref.approval_id})
                projection = RuntimeApprovalProjection.model_validate(value)
                if (projection.approval_id, projection.approval_revision, projection.approval_digest) != (
                    ref.approval_id, ref.approval_revision, ref.approval_digest):
                    raise ValueError
                pins = ReviewedPackagePins.model_validate({name: getattr(projection, name)
                    for name in PackageApprovalFields.model_fields} | {
                    'manifest': retained.package.manifest, 'approval_digest': ref.approval_digest})
                packages.append(validate_package_pins(retained, pins))
        packages = tuple(packages)
        await validate_active_approvals(registry, packages)
        return packages
    except Exception:
        raise ValueError(_ERROR) from None


async def validate_active_approvals(registry, packages):
    """Registry-only active/expiry gate; absence of the protected service denies."""
    try:
        from gateway.source_access import validate_registry_packages
        await validate_registry_packages(registry, packages)
    except Exception:
        raise ValueError(_ERROR) from None


class OperatorError(ValueError):
    def __init__(self, *, uncertain=False):
        self.uncertain = uncertain
        super().__init__('Source approval operation is unavailable or its outcome is uncertain')


def _uuid(value):
    from uuid import UUID
    if type(value) is not str or len(value) != 36 or str(UUID(value)) != value:
        raise ValueError(_ERROR)
    return value


def _receipt(value, *, operation, approval=None, approval_id=None):
    """Whitelist exact receipt shape; never reflect database errors/content."""
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise ValueError(_ERROR)
    expected = ({'id', 'revision', 'content_digest', 'created_at', 'expires_at', 'state', 'replayed'}
        if operation == 'import' else {'id', 'state', 'state_revision', 'replayed'})
    if set(value) != expected or type(value['replayed']) is not bool:
        raise ValueError(_ERROR)
    identity = _uuid(value['id'])
    result = dict(schema_version=1, operation=operation, approval_id=identity, replayed=value['replayed'])
    if type(value['state']) is not str:
        raise ValueError(_ERROR)
    if operation == 'import':
        if (type(value['revision']) is not int or value['revision'] != approval.revision
            or type(value['content_digest']) is not str or value['content_digest'] != approval.content_digest
            or value['state'] not in {'active', 'revoked'}):
            raise ValueError(_ERROR)
        from datetime import datetime
        for name in ('created_at', 'expires_at'):
            if type(value[name]) is not str or len(value[name]) > 40 or datetime.fromisoformat(value[name]).tzinfo is None:
                raise ValueError(_ERROR)
        result.update(revision=value['revision'], content_digest=value['content_digest'], state=value['state'])
    else:
        if (identity != approval_id or value['state'] != 'revoked'
            or type(value['state_revision']) is not int or value['state_revision'] != 2):
            raise ValueError(_ERROR)
        result.update(state='revoked', state_revision=2)
    return result


async def install_approval(registry, retained, raw):
    """Explicit owner-only SQL transaction; no auto-grant or caller pin overrides."""
    from sqlalchemy import text
    dispatched = False
    try:
        approval = validate_source_approval(raw)
        validate_package_approval(retained, approval)
        async with registry.transaction() as uow:
            dispatched = True
            value = await uow.session.scalar(text('SELECT gateway.source_approval_install(:document)'),
                {'document': approval.canonical_content.decode('utf-8')})
            receipt = _receipt(value, operation='import', approval=approval)
        # Successful exit, including commit acknowledgement, precedes any receipt.
        return receipt
    except Exception:
        raise OperatorError(uncertain=dispatched) from None


async def revoke_approval(registry, approval_id, expected_digest, reason_code):
    """Terminal exact-reference revocation; no policy/source/credential mutation."""
    from pydantic import TypeAdapter
    from sqlalchemy import text
    from gateway.contracts.canonical import Digest, Identifier
    from gateway.contracts.manifest import reject_secret
    dispatched = False
    try:
        approval_id = _uuid(approval_id)
        TypeAdapter(Digest).validate_python(expected_digest, strict=True)
        TypeAdapter(Identifier).validate_python(reason_code, strict=True)
        reject_secret(reason_code)
        async with registry.transaction() as uow:
            dispatched = True
            value = await uow.session.scalar(text('SELECT gateway.source_approval_revoke(CAST(:approval_id AS uuid),:digest,:reason)'),
                {'approval_id': approval_id, 'digest': expected_digest, 'reason': reason_code})
            receipt = _receipt(value, operation='revoke', approval_id=approval_id)
        return receipt
    except Exception:
        raise OperatorError(uncertain=dispatched) from None

"""Validated synthetic deployment configuration; no request supplies these values."""
from dataclasses import dataclass
import base64
import json
import os
from pathlib import Path
import re
import stat
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import URL

from gateway.adapters.base import CubeBinding
from gateway.adapters.reviewed import Binding
from gateway.adapters.bundles import BundleCatalog
from gateway.contracts.canonical import StrictModel
from gateway.contracts.manifest import Manifest, validate_manifest
from gateway.publishing.test_runner import FixtureCatalog
from gateway.policy.service import PolicyContent

ROOT = Path(__file__).resolve().parents[1]


class RuntimeConfig(StrictModel):
    identity_mode: Literal['demo'] = 'demo'
    secret_directory: Literal['/run/secrets'] = '/run/secrets'
    registry_host: Literal['registry-db'] = 'registry-db'
    registry_database: Literal['gateway_registry'] = 'gateway_registry'
    probe_principal_id: Literal['demo-north'] = 'demo-north'
    probe_product_id: Literal['revenue'] = 'revenue'
    vendor_connections: tuple[()] = ()
    ingress_port: int = Field(default=8080,ge=1024,le=65535)

    @classmethod
    def from_env(cls):
        from gateway.auth.settings import Settings
        Settings.from_env()  # Refuse unsafe mode/source/ingress/OIDC combinations before sockets.
        value=os.environ.get('GATEWAY_DEMO_PORT','8080')
        if not value.isdecimal():raise ValueError('Invalid private demo port')
        return cls(identity_mode=os.environ.get('GATEWAY_IDENTITY_MODE','demo'),ingress_port=int(value))


def read_secret(path: Path | str) -> str:
    """Read one bounded no-follow mounted secret without including it in errors."""
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o022 or before.st_size > 128:
            raise ValueError
        value = os.read(descriptor, 129)
        after = os.fstat(descriptor)
        if (before.st_size,before.st_mtime_ns,before.st_ctime_ns) != (after.st_size,after.st_mtime_ns,after.st_ctime_ns):
            raise ValueError
        text = value.decode('ascii')
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}\n?', text): raise ValueError
        return text.rstrip('\n')
    except (OSError, UnicodeError, ValueError):
        raise ValueError('A required mounted synthetic secret is unavailable or invalid') from None
    finally:
        if descriptor is not None: os.close(descriptor)


def secret_key(name: str) -> bytes:
    if name not in {'audit_hmac','cursor_key'}: raise ValueError('Unknown runtime key')
    return base64.urlsafe_b64decode(read_secret('/run/secrets/' + name) + '=')


def database_url(role: str) -> str:
    names = {'owner':'registry_owner','app':'registry_app','audit_retention':'registry_audit_retention'}
    if role not in names: raise ValueError('Unknown registry role')
    return URL.create('postgresql+psycopg', username='gateway_' + role,
        password=read_secret('/run/secrets/' + names[role]), host='registry-db', port=5432,
        database='gateway_registry').render_as_string(hide_password=False)


@dataclass(frozen=True)
class Assets:
    manifest: Manifest
    bindings: tuple[Binding, ...]
    bundles: BundleCatalog
    fixture_catalog: FixtureCatalog
    policy_id: str
    policy_revision: int
    policy_content: dict
    accounts: tuple[dict, ...]


def load_assets(root: Path = ROOT) -> Assets:
    """Strictly parse only checked-in reviewed assets; never connect or mint authority."""
    manifest = validate_manifest(json.loads((root/'fixtures/products/revenue.json').read_text()))
    consumer = CubeBinding.model_validate(json.loads((root/'fixtures/bindings/revenue.json').read_text()))
    fixtures = FixtureCatalog.model_validate(json.loads((root/'fixtures/products/revenue.tests.json').read_text()))
    fixtures.approve(manifest, consumer)
    policy = json.loads((root/'fixtures/policies.json').read_text())
    if set(policy) != {'policy_id','revision','content'} or policy['policy_id'] != manifest.policy_id or policy['revision'] != 1:
        raise ValueError('Incompatible reviewed policy asset')
    PolicyContent.model_validate(policy['content'])
    accounts = json.loads((root/'fixtures/accounts.json').read_text())
    if accounts['policy_id'] != policy['policy_id']: raise ValueError('Incompatible identity scope')
    base = accounts['accounts']
    if {a['id'] for a in base} != {'demo-north','demo-south','demo-author','demo-publisher'}:
        raise ValueError('Unexpected synthetic external accounts')
    internal = [dict(id='fixture-'+tenant,tenant_id=tenant,roles=['consumer'],entitlements={'groups':[tenant]})
        for tenant in ('north','south')]
    internal.append(dict(id='fixture-denied',tenant_id='north',roles=[],entitlements={'groups':['north']}))
    return Assets(manifest,(consumer,fixtures.fixture_binding),BundleCatalog.from_file(root/'cube/bundles.json'),
        fixtures,policy['policy_id'],policy['revision'],policy['content'],tuple([*base,*internal]))


# This is an in-process capability, not a claim accepted from configuration,
# serialization, headers or a manifest. Its registry entry is the authority;
# constructing/copying an instance alone grants nothing.
class _SyntheticCapability:
    __slots__ = ('__weakref__',)


from weakref import WeakKeyDictionary
_SYNTHETIC_CAPABILITIES = WeakKeyDictionary()


def _synthetic_scope(assets):
    from gateway.contracts.canonical import canonical_digest
    return canonical_digest({
        'manifest': assets.manifest,
        'bindings': assets.bindings,
        'fixture_catalog': assets.fixture_catalog,
        'policy_id': assets.policy_id,
        'policy_revision': assets.policy_revision,
        'policy_content': assets.policy_content,
        'accounts': assets.accounts,
    })


def _synthetic_registry(registry):
    from gateway.registry.repository import registry_construction_endpoint
    endpoint = registry_construction_endpoint(registry)
    if endpoint is None:
        return False
    driver, username, host, port, database = endpoint
    if driver != 'postgresql+psycopg' or host not in {'registry-db', '127.0.0.1', 'localhost'}:
        return False
    if host == 'registry-db':
        return database == 'gateway_registry' and username == 'gateway_app' and port in {None, 5432}
    match = re.fullmatch(r'gateway_test_([a-f0-9]{16})', database)
    return bool(match and username == 'gwt_' + match.group(1) + '_app' and port)


def _exact_synthetic_value(value, expected, *, depth=0):
    """Compare only inert exact types against freshly parsed fixed assets.

    Never serialize candidate models or call their digest/resolve methods: a
    subclass or a hostile nested scalar must not choose the exempted identity.
    """
    from pydantic import BaseModel
    from types import MappingProxyType
    from gateway.contracts.canonical import FrozenDict
    if depth > 24 or type(value) is not type(expected):
        return False
    recurse = lambda left, right: _exact_synthetic_value(left, right, depth=depth + 1)
    if expected is None or type(expected) in (str, int, float, bool):
        return value == expected
    if type(expected) in (tuple, list):
        return len(value) == len(expected) and all(recurse(v, e) for v, e in zip(value, expected))
    if type(expected) is MappingProxyType:
        # An exact proxy can still wrap an arbitrary hostile mapping. Inspect
        # its C-level referent without invoking len/iteration/lookup hooks.
        import gc
        backing = gc.get_referents(value)
        expected_backing = gc.get_referents(expected)
        return (len(backing) == 1 and len(expected_backing) == 1
            and type(backing[0]) is dict and type(expected_backing[0]) is dict
            and recurse(backing[0], expected_backing[0]))
    if type(expected) in (dict, FrozenDict):
        return (len(value) == len(expected) and all(type(key) is str for key in value)
            and set(value) == set(expected) and all(recurse(value[key], expected[key]) for key in expected))
    if isinstance(expected, BaseModel):
        extra = value.__pydantic_extra__
        return ((extra is None or type(extra) is dict and len(extra) == 0)
            and recurse(value.__dict__, expected.__dict__))
    if type(expected) in (Assets, BundleCatalog):
        return recurse(value.__dict__, expected.__dict__)
    return False


def _synthetic_capability(assets, registry, *, profile):
    """Private fixed-endpoint factory; generalized composition is not synthetic."""
    from gateway.contracts.canonical import canonical_bytes
    from gateway.contracts.errors import GatewayError
    try:
        if type(profile) is not str or profile not in {'legacy', 'reviewed'}:
            raise ValueError
        if type(assets) is not Assets or not _synthetic_registry(registry):
            raise ValueError
        if profile == 'legacy':
            expected = load_assets()
        else:
            from gateway.reviewed_runtime import load_assets as reviewed_assets
            expected = reviewed_assets()
        if (not _exact_synthetic_value(assets, expected)
            or _synthetic_scope(expected) != _SYNTHETIC_SCOPE_DIGESTS[profile]):
            raise ValueError
        # Only trusted, freshly validated values can enter the capability.
        entries = []
        for binding in expected.bindings:
            manifest = expected.manifest if binding.context == 'consumer' else expected.fixture_catalog.execution_manifest(expected.manifest)
            entries.append((binding.context, canonical_bytes(manifest), binding.digest))
        capability = _SyntheticCapability()
        _SYNTHETIC_CAPABILITIES[capability] = (registry, tuple(entries))
        return capability
    except (ValueError, TypeError, AttributeError, RecursionError, GatewayError):
        raise ValueError('Unsupported synthetic composition') from None


def _revoke_synthetic_capability(capability):
    if type(capability) is _SyntheticCapability:
        _SYNTHETIC_CAPABILITIES.pop(capability, None)


def source_approval_required(product, *, synthetic_capability=None, registry=None, context='consumer'):
    """Fail closed, including for v1: only exact trusted composition is exempt."""
    from gateway.contracts.canonical import canonical_bytes
    from gateway.contracts.execution import PinnedProduct
    from gateway.source_access_contracts import _plain
    from gateway.contracts.errors import GatewayError
    try:
        if type(synthetic_capability) is not _SyntheticCapability or type(product) is not PinnedProduct:
            return True
        if type(context) is not str or context not in {'consumer', 'fixture_test'}:
            return True
        data = product.__dict__
        extra = product.__pydantic_extra__
        if (type(data) is not dict or any(type(key) is not str for key in data)
            or set(data) != set(PinnedProduct.model_fields)
            or (extra is not None and (type(extra) is not dict or len(extra) != 0))
            or type(data['binding_digest']) is not str or type(data['version_id']) is not str):
            return True
        checked = PinnedProduct.model_validate({
            'manifest': _plain(data['manifest']), 'binding_digest': data['binding_digest'],
            'version_id': data['version_id'],
        })
        scope = _SYNTHETIC_CAPABILITIES.get(synthetic_capability)
        if scope is None or scope[0] is not registry or not _synthetic_registry(registry):
            return True
        # Fresh manifest validation detects model_copy/model_construct bypasses.
        manifest = validate_manifest(_plain(checked.manifest))
        return (context, canonical_bytes(manifest), checked.binding_digest) not in scope[1]
    except (ValueError, TypeError, AttributeError, RecursionError, GatewayError):
        return True


_SYNTHETIC_SCOPE_DIGESTS = {
    'legacy': 'c3458c9d7b77a5607aaf9855c2f1c4f7c14219c297373c15fa45f1930d24ef7a',
    'reviewed': 'b39550f72224fd355a804388714767d9f7aca564ac098ca14f945020a928f983',
}

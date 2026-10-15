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

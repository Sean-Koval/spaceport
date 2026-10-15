"""Explicit owner approvals for synthetic authoring tests, never runtime setup.

Every intended positive variant must call approve_variant before save/read. The
helper never intercepts service calls or auto-approves mutation-negative inputs.
Fixture execution/publication provenance is deliberately outside this helper.
"""
from copy import deepcopy
from dataclasses import dataclass

from gateway.adapters.base import StaticBindingResolver, check_binding
from gateway.adapters.bundles import BundleCatalog
from gateway.auth.principals import PrincipalStore
from gateway.contracts.canonical import manifest_digest
from gateway.policy.audit import AuditWriter
from gateway.policy.service import PolicyService
from gateway.publishing.service import PublishingService
from gateway.source_access import SourceAccessService
from tests.integration.test_source_approval_registry import install
from tests.integration.test_source_fixture_owners import isolated_approval_assets as approval_assets
from tests.unit.test_source_approval_contracts import principal


@dataclass
class ReviewedAuthoringFixture:
    database: object
    service: PublishingService
    manifest: object
    binding: object
    document: dict
    bindings: tuple
    sources: tuple

    def approve_variant(self, manifest):
        """Test owner explicitly approves exactly this compatible package hash."""
        check_binding(self.binding, manifest)
        document = deepcopy(self.document)
        document['product'] = dict(product_id=manifest.product_id, version=manifest.version,
            manifest_digest=manifest_digest(manifest), policy_id=manifest.policy_id)
        return install(self.database, document)


def reviewed_authoring_fixture(database, raw_manifest, *, actions=('author', 'publish'), input_domains=None, additional_audiences=()):
    raw = deepcopy(raw_manifest)
    # The fixture uses a real supported BigQuery binding with honest unknown
    # freshness; it never simulates a provider watermark or grants live access.
    raw['freshness']['required'] = False
    document, manifest, bindings, sources = approval_assets(database, raw)
    binding = bindings[0]
    check_binding(binding, manifest)
    document['audiences'][0]['actions'] = list(actions)
    document['audiences'].extend(deepcopy(additional_audiences))
    if input_domains is not None:
        document['input_domains'] = deepcopy(input_domains)
    audit = AuditWriter(b'reviewed-authoring-fixture-audit-32')
    source = SourceAccessService(database.registry, audit)
    policy = PolicyService(database.registry, PrincipalStore(database.registry.sessions), audit, source_access=source)
    service = PublishingService(registry=database.registry, policy=policy,
        bindings=StaticBindingResolver(bindings), bundles=BundleCatalog.from_file('cube/bundles.json'))
    fixture = ReviewedAuthoringFixture(database, service, manifest, binding, document, tuple(bindings), tuple(sources))
    fixture.approve_variant(manifest)
    return fixture

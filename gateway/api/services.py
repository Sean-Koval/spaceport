"""Explicit shared dependency container. REST and MCP call the same services."""
from dataclasses import dataclass
from typing import TYPE_CHECKING
from gateway.auth.tokens import Authenticator
from gateway.registry.repository import Registry
from gateway.policy.service import PolicyService
from gateway.adapters.base import BindingResolver
from gateway.adapters.bundles import BundleCatalog
from gateway.execution.service import ExecutionService
from gateway.execution.results import ResultService
from gateway.publishing.service import PublishingService
from gateway.contracts.clock import Clock
if TYPE_CHECKING:
    from .catalog import CatalogService
    from .choices import ChoiceService
    from .audit import AuditReadService
    from .readiness import ReadinessService


@dataclass(frozen=True)
class Services:
    authenticator: Authenticator
    registry: Registry
    policy: PolicyService
    bindings: BindingResolver
    bundles: BundleCatalog
    execution: ExecutionService
    results: ResultService
    publishing: PublishingService
    clock: Clock
    catalog: 'CatalogService'
    choices: 'ChoiceService'
    audit_reader: 'AuditReadService'
    readiness: 'ReadinessService'

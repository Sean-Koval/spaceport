"""Operator-pinned fixture evidence; this module is not a pytest test module."""
__test__ = False

from collections import Counter, defaultdict, deque
from decimal import Decimal, localcontext
import re
from typing import Literal

from pydantic import Field

from gateway.contracts.canonical import Array, Scalar, StrictModel, canonical_bytes
from gateway.contracts.errors import invalid_input
from gateway.contracts.manifest import Manifest, validate_row


class RowComparison(StrictModel):
    passed: bool
    expected: Array[dict[str, Scalar]]
    actual: Array[dict[str, Scalar]]
    absolute_tolerance: str
    relative_tolerance: str


class FixtureCheck(StrictModel):
    fixture_id: str
    kind: Literal['known_answer', 'input_negative', 'permission_negative']
    passed: bool
    expected: Array[dict[str, Scalar]] = ()
    actual: Array[dict[str, Scalar]] = ()
    expected_error: str | None = None
    actual_error: str | None = None
    # Nulls are used only in field-filtered review views, never stored proof.
    absolute_tolerance: str | None = '0'
    relative_tolerance: str | None = '0'
    attempt_id: str | None = None
    decision_digest: str | None = None
    reference_watermark: str | None = None
    observed_source_load_watermark: str | None = None
    freshness_status: Literal['fresh','stale','unknown'] | None = None


def _tolerance(value: str) -> Decimal:
    if type(value) is not str or not re.fullmatch(r'(?:0|[1-9][0-9]{0,37})(?:\.[0-9]{1,38})?', value):
        raise invalid_input('Fixture tolerances require bounded nonnegative decimal strings.')
    return Decimal(value)


def _perfect_row_matching(edges: list[list[int]]) -> bool:
    """Iterative Hopcroft–Karp: O(E sqrt(V)), without recursive augmenting paths."""
    size = len(edges)
    left_match, right_match = [-1] * size, [-1] * size
    matched, infinity = 0, size + 1
    while matched < size:
        distance = [infinity] * size
        queue = deque()
        for left in range(size):
            if left_match[left] == -1:
                distance[left] = 0
                queue.append(left)
        shortest = infinity
        while queue:
            left = queue.popleft()
            if distance[left] >= shortest:
                continue
            for right in edges[left]:
                partner = right_match[right]
                if partner == -1:
                    shortest = distance[left] + 1
                elif distance[partner] == infinity:
                    distance[partner] = distance[left] + 1
                    queue.append(partner)
        if shortest == infinity:
            return False
        # Each edge is visited at most once in this layered-search phase.
        remaining = [iter(candidates) for candidates in edges]
        for start in range(size):
            if left_match[start] != -1:
                continue
            path, chosen = [start], []
            while path:
                left = path[-1]
                for right in remaining[left]:
                    partner = right_match[right]
                    if partner == -1 and distance[left] + 1 == shortest:
                        for source, target in zip(path, [*chosen, right]):
                            left_match[source], right_match[target] = target, source
                        matched += 1
                        path.clear()
                        break
                    if partner != -1 and distance[partner] == distance[left] + 1:
                        path.append(partner)
                        chosen.append(right)
                        break
                else:
                    distance[left] = infinity
                    path.pop()
                    if chosen:
                        chosen.pop()
    return True


def compare_rows(manifest: Manifest, expected, actual, *, absolute: str, relative: str) -> RowComparison:
    """Fixtures request no sort: compare row multisets, preserving raw evidence.

    Tolerance is directional and non-transitive. Neither sorting then zipping
    nor greedily consuming candidates proves that a whole-row bijection exists.
    """
    absolute_value, relative_value = _tolerance(absolute), _tolerance(relative)
    # Fixture.expected_rows and normalized results both have a 1,000-row bound.
    passed = len(expected) == len(actual) and len(expected) <= 1000
    try:
        for row in (*expected, *actual):
            validate_row(manifest.outputs, row)
        if passed:
            def prepared(row):
                exact, decimals = [], []
                for name, field in manifest.outputs.items():
                    value = row[name]
                    if field.type == 'decimal' and value is not None:
                        exact.append(None)
                        decimals.append(Decimal(value))
                    else:
                        exact.append(canonical_bytes(value))
                return tuple(exact), tuple(decimals)

            wanted_rows = [prepared(row) for row in expected]
            observed_rows = [prepared(row) for row in actual]
            if Counter(wanted_rows) != Counter(observed_rows):
                observed_groups = defaultdict(list)
                for index, (key, values) in enumerate(observed_rows):
                    observed_groups[key].append((index, values))
                # decimal(38,s) arithmetic must not inherit the 28-digit default.
                with localcontext() as context:
                    context.prec = 160
                    edges = []
                    for key, wanted in wanted_rows:
                        tolerances = [max(absolute_value, relative_value * abs(value)) for value in wanted]
                        edges.append([index for index, observed in observed_groups[key]
                            if all(abs(left - right) <= tolerance
                                for left, right, tolerance in zip(wanted, observed, tolerances))])
                    passed = _perfect_row_matching(edges)
    except (ValueError, TypeError, KeyError):
        passed = False
    return RowComparison(passed=bool(passed), expected=expected, actual=actual,
        absolute_tolerance=absolute, relative_tolerance=relative)


def ordered_checks(checks) -> tuple[FixtureCheck, ...]:
    return tuple(sorted(checks, key=lambda check: (check.passed, check.fixture_id)))


def successful_evidence(checks) -> bool:
    """A green label alone cannot stand in for an actual current-policy denial."""
    return bool(checks) and all(check.passed for check in checks) and any(
        check.kind == 'permission_negative' and check.expected_error == 'NOT_FOUND_OR_FORBIDDEN'
        and check.actual_error == 'NOT_FOUND_OR_FORBIDDEN' for check in checks) and all(
        check.expected_error == check.actual_error for check in checks)

from pydantic import model_validator
from gateway.adapters.base import check_binding
from gateway.adapters.reviewed import Binding
from gateway.contracts.canonical import Digest, Identifier, canonical_digest
from gateway.contracts.errors import ErrorCode, GatewayError
from gateway.contracts.manifest import Fixture, validate_manifest


def fixture_unavailable():
    return GatewayError(ErrorCode.BINDING_DRIFT, 'fixture-catalog', 'The approved fixture configuration is unavailable.')


class ApprovedFixture(StrictModel):
    """Operator configuration only, never accepted through author request APIs."""
    fixture: Fixture
    principal_id: Identifier
    kind: Literal['known_answer', 'input_negative', 'permission_negative']
    relative_tolerance: str = '0'

    @model_validator(mode='after')
    def coherent_case(self):
        _tolerance(self.fixture.tolerance)
        _tolerance(self.relative_tolerance)
        wanted = {'known_answer': None, 'input_negative': 'INVALID_INPUT',
                  'permission_negative': 'NOT_FOUND_OR_FORBIDDEN'}[self.kind]
        if self.fixture.expected_error != wanted:
            raise ValueError('Fixture kind and expected error must agree')
        return self


class FixtureCatalog(StrictModel):
    """Explicit consumer→isolated-fixture model mapping, supplied by the operator.

    Matching metadata is not evidence of model equivalence. The catalog is the
    operator's reviewed attestation; its complete digest is pinned in each run.
    """
    product_id: Identifier
    consumer_binding: Binding
    fixture_binding: Binding
    execution_contract_digest: Digest
    cases: Array[ApprovedFixture] = Field(min_length=2, max_length=100)

    @model_validator(mode='after')
    def isolated_mapping(self):
        consumer, fixture = self.consumer_binding, self.fixture_binding
        if consumer.context != 'consumer' or fixture.context != 'fixture_test':
            raise ValueError('Fixture and consumer bindings must have distinct contexts')
        if consumer.model_bundle_digest == fixture.model_bundle_digest:
            raise ValueError('Fixture bundle must use the separately isolated deployment')
        if (consumer.binding_id,consumer.revision) == (fixture.binding_id,fixture.revision):
            raise ValueError('Fixture binding must have a distinct immutable identity')
        ignored = {'context','binding_id','revision','model_bundle_digest'}
        if consumer.model_dump(exclude=ignored) != fixture.model_dump(exclude=ignored):
            raise ValueError('Reviewed fixture mappings must preserve field and scope semantics')
        if len({case.fixture.id for case in self.cases}) != len(self.cases):
            raise ValueError('Approved fixture identities must be distinct')
        if not any(case.kind == 'permission_negative' for case in self.cases):
            raise ValueError('A current-policy permission-negative case is required')
        if not any(case.kind == 'known_answer' for case in self.cases):
            raise ValueError('A known-answer case is required')
        return self

    @property
    def digest(self):
        return canonical_digest(self)

    def approve(self, manifest: Manifest, binding: Binding) -> None:
        check_binding(binding, manifest, digest=self.consumer_binding.digest)
        approved = {case.fixture.id:case for case in self.cases}
        if (execution_contract_digest(manifest) != self.execution_contract_digest or manifest.product_id != self.product_id or binding.digest != self.consumer_binding.digest
            or any(fixture.id not in approved or canonical_digest(fixture) != canonical_digest(approved[fixture.id].fixture)
                   for fixture in manifest.fixtures)
            or {case.fixture.id for case in self.cases if case.kind != 'permission_negative'} != {f.id for f in manifest.fixtures if f.expected_error != 'NOT_FOUND_OR_FORBIDDEN'}):
            raise fixture_unavailable()

    def execution_manifest(self, manifest: Manifest) -> Manifest:
        """Separate execution snapshot. The original draft digest is never changed."""
        raw = manifest.model_dump(mode='json', exclude={'content_digest'})
        raw.update(binding_id=self.fixture_binding.binding_id, binding_revision=self.fixture_binding.revision,
                   model_bundle_digest=self.fixture_binding.model_bundle_digest)
        result = validate_manifest(raw)
        check_binding(self.fixture_binding, result, digest=self.fixture_binding.digest)
        return result


from gateway.adapters.domo_runtime import DomoRuntimeBinding, DomoSource, validate_domo_source_isolation


class DomoFixtureCatalog(FixtureCatalog):
    """Operator-reviewed equivalence with exact isolated source identities."""
    consumer_binding: DomoRuntimeBinding
    fixture_binding: DomoRuntimeBinding
    consumer_source: DomoSource
    fixture_source: DomoSource
    cases: Array[ApprovedFixture] = Field(min_length=2,max_length=8)

    @model_validator(mode='after')
    def isolated_mapping(self):
        c,f=self.consumer_binding,self.fixture_binding
        cs,fs=validate_domo_source_isolation(self.consumer_source,self.fixture_source)
        for binding,source in ((c,cs),(f,fs)):
            if ((binding.source_id,binding.source_revision,binding.source_digest,binding.context)
                != (source.source_id,source.revision,source.digest,source.context)
                or binding.dataset_id not in source.dataset_ids):
                raise ValueError('Exact fixture source mapping is required')
        if (c.context!='consumer' or f.context!='fixture_test' or c.dataset_id==f.dataset_id
            or c.model_bundle_digest==f.model_bundle_digest
            or (c.binding_id,c.revision)==(f.binding_id,f.revision)):
            raise ValueError('Distinct reviewed fixture identities are required')
        ignored={'context','binding_id','revision','model_bundle_digest','source_id','source_revision',
            'source_digest','dataset_id','authoritative_definition_digest','variants'}
        if (c.model_dump(exclude=ignored)!=f.model_dump(exclude=ignored)
            or Counter(v.query_digest for v in c.variants)!=Counter(v.query_digest for v in f.variants)):
            raise ValueError('Fixture field, scope and finite request semantics must match')
        if (len({case.fixture.id for case in self.cases})!=len(self.cases)
            or not any(case.kind=='permission_negative' for case in self.cases)
            or not 1<=sum(case.kind=='known_answer' for case in self.cases)<=4):
            raise ValueError('A bounded fixed source suite and permission denial are required')
        return self


from gateway.adapters.bigquery_runtime import BigQueryBinding, BigQuerySource


class BigQueryFixtureCatalog(FixtureCatalog):
    """Exact reviewed source isolation; configuration does not certify live IAM."""
    consumer_binding: BigQueryBinding
    fixture_binding: BigQueryBinding
    consumer_source: BigQuerySource
    fixture_source: BigQuerySource
    cases: Array[ApprovedFixture] = Field(min_length=2,max_length=8)

    @model_validator(mode='after')
    def isolated_mapping(self):
        c,f=self.consumer_binding,self.fixture_binding
        cs,fs=self.consumer_source,self.fixture_source
        for binding,source in ((c,cs),(f,fs)):
            if ((binding.source_id,binding.source_revision,binding.source_digest,binding.context,binding.model_bundle_digest)
                !=(source.source_id,source.revision,source.digest,source.context,source.model_bundle_digest)):
                raise ValueError('Exact fixture source mapping is required')
        if (c.context!='consumer' or f.context!='fixture_test' or cs.source_id==fs.source_id
            or (cs.data_project,cs.dataset)==(fs.data_project,fs.dataset)
            or {(cs.data_project,cs.dataset,t) for t in cs.tables}&{(fs.data_project,fs.dataset,t) for t in fs.tables}
            or cs.auth.expected_principal==fs.auth.expected_principal or cs.cube_endpoint==fs.cube_endpoint
            or c.model_bundle_digest==f.model_bundle_digest or (c.binding_id,c.revision)==(f.binding_id,f.revision)):
            raise ValueError('Distinct reviewed fixture identities are required')
        ignored={'context','binding_id','revision','model_bundle_digest','source_id','source_revision','source_digest'}
        if c.model_dump(exclude=ignored)!=f.model_dump(exclude=ignored):
            raise ValueError('Reviewed source fixture field and scope semantics must match')
        if (len({case.fixture.id for case in self.cases})!=len(self.cases)
            or not any(case.kind=='permission_negative' for case in self.cases)
            or not 1<=sum(case.kind=='known_answer' for case in self.cases)<=4):
            raise ValueError('A bounded fixed source suite and permission denial are required')
        return self


def parse_fixture_catalog(value):
    if isinstance(value,FixtureCatalog):
        value=value.model_dump(mode='python')
    if (type(value) is dict and type(value.get('consumer_binding')) is dict
        and value['consumer_binding'].get('adapter_revision')=='domo-named-v2'):
        return DomoFixtureCatalog.model_validate(value)
    if (type(value) is dict and type(value.get('consumer_binding')) is dict
        and value['consumer_binding'].get('adapter_revision')=='cube-bigquery-v1'):
        return BigQueryFixtureCatalog.model_validate(value)
    return FixtureCatalog.model_validate(value)


def execution_contract_digest(manifest: Manifest) -> str:
    # Deny-by-default projection: future manifest fields affect approval unless
    # explicitly reviewed as presentation-only. Fixtures are pinned separately.
    return canonical_digest(manifest.model_dump(mode='json', exclude={
        'name','description','examples','owner','domain','links','version','content_digest','fixtures'}))

import asyncio
from contextlib import suppress
from dataclasses import dataclass, replace
from uuid import uuid4
from sqlalchemy import func, select, update, text

from gateway.adapters.normalize import NormalizationContext, normalize
from gateway.contracts.clock import SystemClock
from gateway.contracts.execution import Completed, ExecutionSpec, ExecutionView, Pending, PinnedProduct, is_completed
from gateway.contracts.query import QueryRequest, validate_query
from gateway.execution.queue import lock_execution_capacity, source_capacity_counts
from gateway.execution.service import FixtureCapability, check_current_scope
from gateway.execution.worker import backoff_seconds
from gateway.policy.service import derive_decision
from gateway.registry.models import BindingRevision, DraftRevision, ProductRow, TestRun
from gateway.registry.repository import conflict, not_found
from .models import FixtureAttempt, FixtureAttemptState, FixtureSourceReservation
from .service import review_target


class FixtureEvidence(StrictModel):
    schema_version: Literal['1'] = '1'
    catalog_digest: Digest
    execution_contract_digest: Digest
    fixture_binding_digest: Digest
    fixture_model_bundle_digest: Digest
    checks: Array[FixtureCheck] = Field(min_length=1,max_length=100)


def filtered_test_run(stored: TestRun, visible_fields: tuple[str, ...], manifest: Manifest) -> TestRun:
    """A detached review view; the database retains complete immutable proof.

    Only server-derived author visibility is passed here. The resulting view is
    never inserted or used as publication authority; publish loads its own row.
    """
    try:
        evidence = FixtureEvidence.model_validate(stored.evidence).model_dump(mode='json')
    except (ValueError,TypeError):
        raise fixture_unavailable() from None
    visible = set(visible_fields)
    visible_decimal = any(field in visible and schema.type == 'decimal' for field, schema in manifest.outputs.items())
    for check in evidence['checks']:
        if not visible_decimal:
            check['absolute_tolerance'] = check['relative_tolerance'] = None
        for name in ('expected', 'actual'):
            check[name] = [{field: value for field, value in row.items() if field in visible}
                           for row in check[name]]
    values = {column.key: getattr(stored, column.key) for column in TestRun.__table__.columns}
    values['evidence'] = evidence
    return TestRun(**values)


@dataclass(frozen=True)
class _DraftAuthority:
    """Internal run authority, never accepted from a transport or serialized."""
    issuer: object
    actor: object
    draft: object
    product: PinnedProduct
    binding_row: object
    fixture_binding_row: object
    policy_revision: int
    review_visible_fields: tuple[str, ...]
    catalog: FixtureCatalog
    source_group: object | None = None

    def __reduce__(self):
        raise TypeError('Draft fixture authority cannot be serialized')

    def __reduce_ex__(self, protocol):
        raise TypeError('Draft fixture authority cannot be serialized')


@dataclass(frozen=True, eq=False)
class _FixtureCapability(FixtureCapability):
    issuer: object
    authority: _DraftAuthority
    case: ApprovedFixture
    attempt: FixtureAttempt
    decision: object

    def __new__(cls, *args, **kwargs):
        return object.__new__(cls)


def validate_fixture_source_completed(completed,source_context):
    """A source candidate cannot borrow another case/session's terminal proof."""
    from gateway.source_lifecycle import SourceAttemptContext
    from gateway.adapters.domo_completed import DomoCompleted
    try:
        if type(source_context) is not SourceAttemptContext:
            raise ValueError
        current=source_context.wire_context()
        if source_context.spec.provider=='domo':
            if (type(completed) is not DomoCompleted
                or canonical_digest(completed.permit.context.wire_context())!=canonical_digest(current)
                or completed.prepared.digest!=source_context.spec.prepared_digest):
                raise ValueError
            completed.evidence()
        elif type(completed) is not Completed:
            raise ValueError
    except (ValueError,TypeError,AttributeError,GatewayError):
        raise fixture_unavailable() from None


class FixtureRunner:
    """Functional draft-only execution with fixed operator-approved identities.

    Direct admission has no queue or reclaim. A crash, uncertain source reply,
    stale authority, or revoked access leaves its source slot quarantined. No
    timer fabricates source completion. There are no consumer execution/results.
    """
    def __init__(self, *, registry, policy, bindings, bundles, catalog_provider,
                 adapter_factory, clock=None, source_lifecycle=None):
        self.registry,self.policy,self.bindings,self.bundles = registry,policy,bindings,bundles
        self.catalog_provider,self.adapter_factory = catalog_provider,adapter_factory
        self.source_lifecycle = source_lifecycle
        self.clock = clock or SystemClock()
        self._issuer = object()
        self._issued = {}

    def _issue(self, authority, case, attempt, decision):
        capability = _FixtureCapability(self._issuer,authority,case,attempt,decision)
        self._issued[id(capability)] = capability
        return capability

    async def submit_fixture(self, principal, spec, capability) -> ExecutionView:
        view, _ = await self._dispatch(principal,spec,capability)
        return view

    async def _dispatch(self, principal, spec, capability):
        try:
            if (type(capability) is not _FixtureCapability
                or self._issued.get(id(capability)) is not capability
                or capability.issuer is not self._issuer
                or capability.authority.issuer is not self._issuer
                or capability.authority.actor != principal
                or capability.case not in capability.authority.catalog.cases
                or canonical_digest(spec) != canonical_digest(self._spec(capability.attempt))):
                raise not_found()
        except (ValueError,TypeError,AttributeError):
            raise not_found() from None
        finally:
            self._issued.pop(id(capability),None)
        authority,case,attempt,decision = capability.authority,capability.case,capability.attempt,capability.decision
        check = await self._execute_case(authority,case,attempt,decision)
        async with self.registry.transaction() as uow:
            # The terminal view is fixture-only and still author-gated.
            await self._authority(uow,principal,authority.draft.id,authority)
            state = await uow.session.get(FixtureAttemptState,attempt.id)
            if state is None or state.state == 'running':
                raise conflict()
            view = ExecutionView(execution_id=attempt.id,state=state.state,accepted_at=attempt.created_at,
                started_at=attempt.created_at,completed_at=state.completed_at,
                error=GatewayError(state.error_code,attempt.request_id,'The fixture execution failed.').as_dict()
                    if state.error_code else None)
        return view,check

    def catalog(self, manifest=None, binding=None) -> FixtureCatalog:
        try:
            if manifest is not None and manifest.adapter_revision in {'domo-named-v2','cube-bigquery-v1'}:
                if binding is None:
                    raise ValueError
                supplied = self.catalog_provider.resolve(manifest,binding)
            else:
                supplied = self.catalog_provider()
            return parse_fixture_catalog(supplied)
        except (ValueError,TypeError,AttributeError,GatewayError):
            raise fixture_unavailable() from None

    def evidence_current(self, manifest, binding, test) -> bool:
        try:
            catalog = self.catalog(manifest,binding)
            catalog.approve(manifest,binding)
            evidence = FixtureEvidence.model_validate(test.evidence)
            if (test.fixture_version != catalog.digest or test.status != 'passed'
                or evidence.catalog_digest != catalog.digest
                or evidence.execution_contract_digest != execution_contract_digest(manifest)
                or evidence.fixture_binding_digest != catalog.fixture_binding.digest
                or evidence.fixture_model_bundle_digest != catalog.fixture_binding.model_bundle_digest
                or not successful_evidence(evidence.checks)
                or len(evidence.checks) != len(catalog.cases)
                or {check.fixture_id for check in evidence.checks} != {case.fixture.id for case in catalog.cases}):
                return False
            expected = {case.fixture.id:case for case in catalog.cases}
            for check in evidence.checks:
                case = expected[check.fixture_id]
                if (check.kind != case.kind or check.expected_error != case.fixture.expected_error
                    or canonical_digest(check.expected) != canonical_digest(case.fixture.expected_rows)
                    or check.absolute_tolerance != case.fixture.tolerance
                    or check.relative_tolerance != case.relative_tolerance
                    or check.reference_watermark != case.fixture.source_watermark):
                    return False
                if case.kind == 'known_answer':
                    if (not check.attempt_id or check.freshness_status != 'fresh' and manifest.freshness.required
                        or not compare_rows(manifest,check.expected,check.actual,
                            absolute=check.absolute_tolerance,relative=check.relative_tolerance).passed):
                        return False
            return True
        except (ValueError,TypeError,AttributeError,GatewayError):
            return False

    async def evidence_stale_causes(self, manifest, binding, test, uow) -> tuple[str, ...]:
        """Recheck live fixture pins and fixed grants in the publication UoW.

        Publisher permission never becomes data permission. Only the catalog's
        fixed identities are read, and no credential, grant, or policy is added.
        This gate has the same current-grant-read linearization as result access.
        """
        if not self.evidence_current(manifest, binding, test):
            return ('fixtures',)
        catalog = self.catalog(manifest,binding)
        evidence = FixtureEvidence.model_validate(test.evidence)
        if evidence.catalog_digest != catalog.digest:
            return ('fixtures',)
        try:
            catalog.approve(manifest, binding)
            for approved in (catalog.consumer_binding, catalog.fixture_binding):
                await self._binding_row(uow, approved)
                if self.bundles.resolve(approved.model_bundle_digest).context != approved.context:
                    raise fixture_unavailable()
        except GatewayError as error:
            if error.code == ErrorCode.BINDING_DRIFT:
                return ('fixtures',)
            raise
        if not await self._attempts_current(manifest, catalog, test, evidence, uow):
            return ('fixtures',)
        target = review_target(manifest, test.draft_id, binding.digest)
        checks = {check.fixture_id: check for check in evidence.checks}
        for case in catalog.cases:
            try:
                _, decision = await self._decision_for_case(uow, target, test.policy_revision, case)
            except GatewayError as error:
                if error.code in {ErrorCode.BINDING_DRIFT, ErrorCode.CONFLICT, ErrorCode.NOT_FOUND_OR_FORBIDDEN}:
                    return ('policy',)
                raise
            check = checks[case.fixture.id]
            if case.kind == 'permission_negative':
                if decision is not None:
                    return ('policy',)
            elif decision is None or canonical_digest(decision) != check.decision_digest:
                return ('policy',)
        return ()

    async def _attempts_current(self, manifest, catalog, test, evidence, uow) -> bool:
        """A green check must name this exact successful immutable attempt.

        These are stored provenance checks, not a substitute for actual Cube
        acceptance. Never permit a made-up, failed, or different draft's ID to
        lend execution authority to otherwise plausible expected/actual JSON.
        """
        cases = {case.fixture.id: case for case in catalog.cases}
        used = set()
        expected_manifest = catalog.execution_manifest(manifest)
        for check in evidence.checks:
            if check.kind != 'known_answer':
                continue
            if check.attempt_id in used:
                return False
            used.add(check.attempt_id)
            attempt = await uow.session.get(FixtureAttempt, check.attempt_id)
            state = await uow.session.get(FixtureAttemptState, check.attempt_id)
            if attempt is None or state is None:
                return False
            if catalog.fixture_binding.adapter_revision in {'domo-named-v2','cube-bigquery-v1'}:
                source=(await uow.session.execute(text('''SELECT s.id,s.sealed,s.deadline_at,g.closed,g.settled_at,g.owner_id
                    FROM gateway.source_sessions s JOIN gateway.source_operation_groups g ON g.id=s.group_id
                    WHERE s.context='fixture_test' AND s.owner_id=:owner'''),{'owner':attempt.id})).mappings().one_or_none()
                if (source is None or not source['sealed'] or not source['closed'] or source['settled_at'] is None
                    or source['owner_id']!=test.id or state.completed_at is None or state.completed_at>=source['deadline_at']):
                    return False
            case = cases[check.fixture_id]
            query = validate_query(manifest, QueryRequest(product_id=manifest.product_id,
                version=manifest.version, inputs=case.fixture.inputs)).model_dump(mode='json', exclude={'idempotency_key'})
            if (attempt.draft_id != test.draft_id or attempt.draft_digest != manifest.content_digest
                or attempt.product_id != manifest.product_id or attempt.actor_id != test.actor_id
                or attempt.consumer_binding_id != test.binding_id
                or attempt.consumer_binding_digest != catalog.consumer_binding.digest
                or attempt.fixture_binding_digest != catalog.fixture_binding.digest
                or attempt.fixture_manifest_digest != expected_manifest.content_digest
                or canonical_digest(attempt.fixture_manifest) != canonical_digest(expected_manifest)
                or attempt.execution_contract_digest != execution_contract_digest(manifest)
                or attempt.catalog_digest != catalog.digest or attempt.policy_id != manifest.policy_id
                or attempt.policy_revision != test.policy_revision or attempt.fixture_id != case.fixture.id
                or attempt.fixture_version != case.fixture.version or attempt.fixed_principal_id != case.principal_id
                or canonical_digest(attempt.decision) != check.decision_digest
                or attempt.canonical_hash != canonical_digest(query)
                or canonical_digest(attempt.canonical_query) != canonical_digest(query)
                or state.state != 'succeeded' or not state.source_completed or state.error_code is not None
                or state.check_digest != canonical_digest(check)
                or state.completed_at is None or not attempt.created_at <= state.completed_at < attempt.deadline_at):
                return False
        return True

    async def _binding_row(self,uow,binding):
        row = await uow.session.scalar(select(BindingRevision).where(
            BindingRevision.binding_id==binding.binding_id, BindingRevision.revision==binding.revision))
        resolved = await self.bindings.resolve(binding.binding_id,binding.revision)
        if (row is None or row.content_digest != binding.digest or resolved.digest != binding.digest
            or row.model_bundle_digest != binding.model_bundle_digest or row.adapter_revision != binding.adapter_revision):
            raise fixture_unavailable()
        return row

    async def _authority(self,uow,actor,draft_id,expected=None,*,correlation_id=None):
        product_id = await uow.session.scalar(select(DraftRevision.product_id).where(DraftRevision.id==draft_id))
        if product_id is None:
            raise not_found()
        head = await uow.lock_product(product_id)
        draft = await uow.lock_draft(draft_id)
        manifest = validate_manifest(draft.manifest)
        target = review_target(manifest,draft.id)
        decision = await self.policy.authorize(actor,'author',target,uow,correlation_id=correlation_id)
        if head.latest_draft_id != draft.id or head.draft_revision != draft.revision:
            raise conflict()
        bound = await self.bindings.resolve(manifest.binding_id,manifest.binding_revision)
        catalog = self.catalog(manifest,bound)
        catalog.approve(manifest,catalog.consumer_binding)
        consumer = await self._binding_row(uow,catalog.consumer_binding)
        fixture = await self._binding_row(uow,catalog.fixture_binding)
        for binding in (catalog.consumer_binding,catalog.fixture_binding):
            if self.bundles.resolve(binding.model_bundle_digest).context != binding.context:
                raise fixture_unavailable()
        authority = _DraftAuthority(self._issuer,actor,draft,
            review_target(manifest,draft.id,consumer.content_digest),consumer,fixture,decision.policy_revision,
            decision.visible_fields,catalog)
        if expected is not None and (expected.issuer is not self._issuer or expected.actor != actor
            or expected.draft.id != draft.id or expected.draft.content_digest != draft.content_digest
            or expected.policy_revision != decision.policy_revision or expected.catalog.digest != catalog.digest
            or expected.binding_row.content_digest != consumer.content_digest
            or expected.fixture_binding_row.content_digest != fixture.content_digest):
            raise conflict()
        return authority

    async def _data_decision(self,uow,authority,case,*,action='execute',correlation_id=None):
        """Actual current policy semantics, explicitly skipping published lifecycle.

        This omission is only reached after the run's current author permission,
        exact draft/catalog/binding checks. Missing fixed identities are NOT a
        permission-negative success and no grants are created or overridden.
        """
        if authority.issuer is not self._issuer or case not in authority.catalog.cases:
            raise not_found()
        return await self._decision_for_case(uow, authority.product, authority.policy_revision, case, action=action,correlation_id=correlation_id)

    async def _decision_for_case(self, uow, product, policy_revision, case, *, action='execute',correlation_id=None):
        try:
            principal = await self.policy.principals.resolve_principal(case.principal_id)
            await self.policy._identity(principal,uow)
        except GatewayError:
            raise fixture_unavailable() from None
        grants = await self.policy.principals.get_current(principal.id,session=uow.session)
        revision,content = await self.policy._current(product.manifest.policy_id,uow)
        if revision.revision != policy_revision:
            raise conflict()
        decision = None
        try:
            decision = derive_decision(grants,action,product,content.rules,revision.revision)
        except GatewayError as error:
            if error.code != ErrorCode.NOT_FOUND_OR_FORBIDDEN:
                raise
        await self.policy.audit.append(uow,self.policy.audit.event(principal,action,
            product=product,policy_revision=revision.revision,
            decision='allow' if decision is not None else 'deny',parameters=dict(case.fixture.inputs),
            correlation_id=correlation_id))
        return principal,decision

    async def _case_start(self,authority,case):
        async with self.registry.transaction() as uow:
            await lock_execution_capacity(uow)
            request_id = str(uuid4())
            current = await self._authority(uow,authority.actor,authority.draft.id,authority,correlation_id=request_id)
            principal,decision = await self._data_decision(uow,current,case,correlation_id=request_id)
            if case.kind == 'permission_negative':
                return None,principal,decision
            if decision is None:
                return None,principal,None
            try:
                request = validate_query(current.product.manifest,QueryRequest(
                    product_id=current.product.manifest.product_id,version=current.product.manifest.version,
                    inputs=case.fixture.inputs))
            except GatewayError as error:
                if error.code == ErrorCode.INVALID_INPUT and case.kind == 'input_negative':
                    return ErrorCode.INVALID_INPUT,principal,decision
                raise
            if case.kind == 'input_negative':
                return None,principal,decision
            manifest = current.catalog.execution_manifest(current.product.manifest)
            target = PinnedProduct(manifest=manifest,binding_digest=current.catalog.fixture_binding.digest,
                version_id=current.draft.id)
            check_current_scope(target,request,current.catalog.fixture_binding,decision,decision,'fixture-case')
            if getattr(authority,"source_group",None) is not None:
                await self.source_lifecycle.check_fixture_group(uow,getattr(authority,"source_group",None),authority,case)
            else:
                own,total = await source_capacity_counts(uow,current.actor.id)
                if (own >= decision.limits.per_principal_concurrency
                    or total >= decision.limits.deployment_concurrency):
                    raise GatewayError(ErrorCode.RESOURCE_LIMIT,'fixture-capacity','Fixture source capacity is unavailable.')
            canonical = request.model_dump(mode='json',exclude={'idempotency_key'})
            attempt = FixtureAttempt(id=str(uuid4()),product_id=manifest.product_id,draft_id=current.draft.id,
                draft_digest=current.draft.content_digest,consumer_binding_id=current.binding_row.id,
                consumer_binding_digest=current.binding_row.content_digest,fixture_binding_id=current.fixture_binding_row.id,
                fixture_binding_digest=current.fixture_binding_row.content_digest,fixture_manifest=manifest.model_dump(mode='json'),
                fixture_manifest_digest=manifest.content_digest,execution_contract_digest=execution_contract_digest(current.product.manifest),
                policy_id=manifest.policy_id,policy_revision=decision.policy_revision,catalog_digest=current.catalog.digest,
                fixture_id=case.fixture.id,fixture_version=case.fixture.version,actor_id=current.actor.id,
                fixed_principal_id=principal.id,principal_snapshot=principal.model_dump(mode='json'),decision=decision.model_dump(mode='json'),
                canonical_query=canonical,canonical_hash=canonical_digest(canonical),runner_token=str(uuid4()),
                request_id=request_id,deadline_at=func.clock_timestamp())
            uow.session.add(attempt)
            await uow.session.flush()
            await uow.session.refresh(attempt)
            uow.session.add(FixtureAttemptState(attempt_id=attempt.id,state='running',source_completed=False))
            if getattr(authority,"source_group",None) is None:
                uow.session.add(FixtureSourceReservation(attempt_id=attempt.id,actor_id=current.actor.id,
                    fixed_principal_id=principal.id,runner_token=attempt.runner_token))
            await uow.session.flush()
            return attempt,principal,decision

    def _spec(self,attempt):
        return ExecutionSpec(principal_id=attempt.fixed_principal_id,
            pinned_product=PinnedProduct(manifest=validate_manifest(attempt.fixture_manifest),
                binding_digest=attempt.fixture_binding_digest,version_id=attempt.draft_id),
            canonical_query=attempt.canonical_query,canonical_hash=attempt.canonical_hash,
            policy_revision=attempt.policy_revision,context='fixture_test',request_id=attempt.request_id)

    async def _gate(self,uow,authority,case,attempt,decision,*,action='execute'):
        await lock_execution_capacity(uow)
        current = await self._authority(uow,authority.actor,authority.draft.id,authority,correlation_id=attempt.request_id)
        principal,fresh = await self._data_decision(uow,current,case,action=action,correlation_id=attempt.request_id)
        if fresh is None or canonical_digest(fresh) != canonical_digest(decision):
            # Preserve this denial independently before aborting the caller.
            await self.policy._deny(uow,principal,action,product=authority.product,
                policy_revision=current.policy_revision,correlation_id=attempt.request_id,parameters=dict(case.fixture.inputs))
            raise not_found()
        state = await uow.session.scalar(select(FixtureAttemptState).where(
            FixtureAttemptState.attempt_id==attempt.id).with_for_update())
        stored = await uow.session.get(FixtureAttempt,attempt.id)
        now = await uow.session.scalar(select(func.clock_timestamp()))
        if state is None or state.state != 'running' or stored is None or stored.runner_token != attempt.runner_token:
            raise conflict()
        if getattr(authority,"source_group",None) is not None:
            await self.source_lifecycle.check_fixture_group(uow,getattr(authority,"source_group",None),authority,case)
        deadline=min(attempt.deadline_at,getattr(authority,"source_group",None).deadline) if getattr(authority,"source_group",None) else attempt.deadline_at
        if now >= deadline:
            raise GatewayError(ErrorCode.EXECUTION_TIMEOUT,attempt.request_id,'The fixture execution deadline elapsed.')
        return current,fresh,state,now

    async def _finish_failed(self,attempt,code):
        # Record no successful rows. Legacy reservations stay conservative; new
        # source groups settle separately only from retained provider proof.
        async with self.registry.transaction() as uow:
            await lock_execution_capacity(uow)
            state = await uow.session.scalar(select(FixtureAttemptState).where(
                FixtureAttemptState.attempt_id==attempt.id).with_for_update())
            if state is not None and state.state == 'running':
                state.state = 'timed_out' if code == ErrorCode.EXECUTION_TIMEOUT else 'failed'
                state.error_code = ErrorCode(code).value
                state.source_completed = False
                await uow.session.flush()

    async def _execute_case(self,authority,case,attempt,decision):
        spec = self._spec(attempt)
        adapter = None
        source_context = None
        deadline=min(attempt.deadline_at,getattr(authority,"source_group",None).deadline) if getattr(authority,"source_group",None) else attempt.deadline_at
        try:
            remaining = (deadline-self.clock.now()).total_seconds()
            if remaining <= 0:
                raise GatewayError(ErrorCode.EXECUTION_TIMEOUT,attempt.request_id,'The fixture execution deadline elapsed.')
            async with asyncio.timeout(min(120,remaining)):
                async with self.registry.transaction() as uow:
                    await self._gate(uow,authority,case,attempt,decision)
                adapter = self.adapter_factory()
                prepared = await adapter.prepare(spec.pinned_product,QueryRequest.model_validate(spec.canonical_query),decision,
                    context='fixture_test',deadline=deadline,request_id=attempt.request_id)
                endpoint = (self.bundles.resolve(prepared.model_bundle_digest) if getattr(authority,"source_group",None) is not None
                    else self.bundles.resolve_query(prepared.endpoint_id,prepared.model_bundle_digest))
                if endpoint.context != 'fixture_test' or prepared.model_bundle_digest != spec.pinned_product.manifest.model_bundle_digest:
                    raise fixture_unavailable()
                poll_number = 0
                while True:
                    async with self.registry.transaction() as uow:
                        _,fresh,_,_ = await self._gate(uow,authority,case,attempt,decision)
                    authority_context={'spec':spec,'executed_decision':decision} if spec.pinned_product.manifest.adapter_revision in {'cube-rest-v2','domo-named-v2','cube-bigquery-v1'} else {}
                    if getattr(authority,"source_group",None) is not None:
                        if source_context is None:
                            plan=await adapter.source_plan(spec,prepared,decision,catalog_digest=authority.catalog.digest)
                            source_context=await self.source_lifecycle.fixture_attempt(getattr(authority,"source_group",None),authority,case,
                                attempt,prepared,fresh,plan)
                        authority_context['source_context']=source_context
                    completed = await adapter.poll(prepared,fresh,**authority_context,context='fixture_test',
                        deadline=deadline,request_id=attempt.request_id)
                    if is_completed(completed):
                        break
                    if not isinstance(completed,Pending):
                        raise GatewayError(ErrorCode.UPSTREAM_UNAVAILABLE,attempt.request_id,'The fixture source is unavailable.')
                    duration = backoff_seconds(poll_number,self.clock.now(),deadline)
                    if duration <= 0:
                        raise GatewayError(ErrorCode.EXECUTION_TIMEOUT,attempt.request_id,'The fixture execution deadline elapsed.')
                    await self.clock.sleep(duration)
                    poll_number += 1
                if source_context is not None:
                    await adapter.close()
                    validate_fixture_source_completed(completed,source_context)
                    status=await self.source_lifecycle.seal_source(source_context,'completed')
                    if not status.can_complete_session:
                        raise fixture_unavailable()
                async with self.registry.transaction() as uow:
                    _,fresh,state,now = await self._gate(uow,authority,case,attempt,decision,action='result')
                    view = ExecutionView(execution_id=attempt.id,state='succeeded',accepted_at=attempt.created_at,
                        started_at=attempt.created_at,completed_at=now)
                    class CompletionClock:
                        def now(self): return now
                    try:
                        envelope = normalize(spec.pinned_product,completed,view,context=NormalizationContext(
                            spec=spec,decision=fresh,executed_decision=decision,binding=authority.catalog.fixture_binding,
                            clock=CompletionClock()))
                    except GatewayError as error:
                        if error.code not in {ErrorCode.STALE_DATA, ErrorCode.FRESHNESS_UNKNOWN,
                                              ErrorCode.RESOURCE_LIMIT, ErrorCode.BINDING_DRIFT}:
                            raise
                        # A typed Completed plus the successful current result
                        # gate proves this exact source call has ended. Answer
                        # rejection is distinct from uncertain source completion.
                        # Commit failed proof and its release in this same UoW;
                        # no rejected rows enter the check or a result store.
                        state.state, state.source_completed = 'failed', True
                        state.error_code = error.code.value
                        await uow.session.flush()
                        if source_context is None:
                            await uow.session.execute(update(FixtureSourceReservation).where(
                                FixtureSourceReservation.attempt_id==attempt.id,
                                FixtureSourceReservation.runner_token==attempt.runner_token).values(release_reason='completed'))
                        return FixtureCheck(fixture_id=case.fixture.id,kind=case.kind,passed=False,
                            expected=case.fixture.expected_rows,expected_error=case.fixture.expected_error,
                            actual_error=error.code.value,absolute_tolerance=case.fixture.tolerance,
                            relative_tolerance=case.relative_tolerance,attempt_id=attempt.id,
                            decision_digest=canonical_digest(decision),reference_watermark=case.fixture.source_watermark,
                            observed_source_load_watermark=completed.source_watermark.isoformat() if completed.source_watermark else None,
                            freshness_status='stale' if error.code==ErrorCode.STALE_DATA else
                                'unknown' if error.code==ErrorCode.FRESHNESS_UNKNOWN else None)
                    comparison = compare_rows(authority.product.manifest,case.fixture.expected_rows,envelope.rows,
                        absolute=case.fixture.tolerance,relative=case.relative_tolerance)
                    check = FixtureCheck(fixture_id=case.fixture.id,kind=case.kind,passed=comparison.passed,
                        expected=comparison.expected,actual=comparison.actual,absolute_tolerance=case.fixture.tolerance,
                        relative_tolerance=case.relative_tolerance,attempt_id=attempt.id,decision_digest=canonical_digest(decision),
                        reference_watermark=case.fixture.source_watermark,
                        observed_source_load_watermark=completed.source_watermark.isoformat() if completed.source_watermark else None,
                        freshness_status=envelope.freshness.status)
                    state.state,state.source_completed = 'succeeded',True
                    state.check_digest = canonical_digest(check)
                    await uow.session.flush()
                    if source_context is None:
                        await uow.session.execute(update(FixtureSourceReservation).where(
                            FixtureSourceReservation.attempt_id==attempt.id,
                            FixtureSourceReservation.runner_token==attempt.runner_token).values(release_reason='completed'))
                    return check
        except TimeoutError:
            await self._finish_failed(attempt,ErrorCode.EXECUTION_TIMEOUT)
            raise GatewayError(ErrorCode.EXECUTION_TIMEOUT,attempt.request_id,'The fixture execution deadline elapsed.') from None
        except GatewayError as error:
            await self._finish_failed(attempt,error.code)
            raise
        except Exception:
            await self._finish_failed(attempt,ErrorCode.UPSTREAM_UNAVAILABLE)
            raise GatewayError(ErrorCode.UPSTREAM_UNAVAILABLE,attempt.request_id,'The fixture source is unavailable.') from None
        finally:
            if adapter is not None:
                # Cleanup is not source-completion proof and cannot replace the
                # safe terminal error or keep a completed fixture call open.
                with suppress(Exception):
                    async with asyncio.timeout(1):
                        await adapter.close()

    async def run(self,actor,draft_id) -> TestRun:
        async with self.registry.transaction() as uow:
            authority = await self._authority(uow,actor,draft_id)
        run_id=str(uuid4())
        if authority.catalog.fixture_binding.adapter_revision in {'domo-named-v2','cube-bigquery-v1'}:
            if self.source_lifecycle is None:
                raise fixture_unavailable()
            group=await self.source_lifecycle.begin_fixture_run(authority,run_id)
            authority=replace(authority,source_group=group)
        reason='failure'
        try:
            result=await self._run_cases(authority,run_id,actor,draft_id)
            reason='completed'
            return result
        finally:
            if getattr(authority,"source_group",None) is not None:
                await self.source_lifecycle.close_group(getattr(authority,"source_group",None).group_id,reason)

    async def _run_cases(self,authority,run_id,actor,draft_id):
        checks = []
        for case in authority.catalog.cases:
            attempt,decision = None,None
            try:
                attempt,principal,decision = await self._case_start(authority,case)
                if isinstance(attempt,FixtureAttempt):
                    capability = self._issue(authority,case,attempt,decision)
                    _,check = await self._dispatch(actor,self._spec(attempt),capability)
                else:
                    actual_error = (ErrorCode.NOT_FOUND_OR_FORBIDDEN.value if decision is None else
                                    ErrorCode.INVALID_INPUT.value if attempt == ErrorCode.INVALID_INPUT else None)
                    check = FixtureCheck(fixture_id=case.fixture.id,kind=case.kind,
                        passed=case.fixture.expected_error is not None and actual_error==case.fixture.expected_error,
                        expected=case.fixture.expected_rows,expected_error=case.fixture.expected_error,
                        actual_error=actual_error,absolute_tolerance=case.fixture.tolerance,
                        relative_tolerance=case.relative_tolerance,
                        decision_digest=canonical_digest(decision) if decision is not None else None,
                        reference_watermark=case.fixture.source_watermark)
            except GatewayError as error:
                check = FixtureCheck(fixture_id=case.fixture.id,kind=case.kind,passed=False,
                    expected=case.fixture.expected_rows,expected_error=case.fixture.expected_error,
                    actual_error=error.code.value,absolute_tolerance=case.fixture.tolerance,
                    relative_tolerance=case.relative_tolerance,reference_watermark=case.fixture.source_watermark,
                    attempt_id=attempt.id if isinstance(attempt,FixtureAttempt) else None,
                    decision_digest=canonical_digest(decision) if decision is not None else None)
            checks.append(check)
        async with self.registry.transaction() as uow:
            current = await self._authority(uow,actor,draft_id,authority)
            # A positive or negative grant can change after its individual case.
            # Revalidate all at evidence commit; changes cannot create green proof.
            final = []
            for case,check in zip(current.catalog.cases,checks):
                try:
                    _,decision = await self._data_decision(uow,current,case)
                except GatewayError as error:
                    if error.code != ErrorCode.BINDING_DRIFT:
                        raise
                    # Missing or inactive operator-fixed identities are a failed
                    # configuration check, never evidence of a policy denial.
                    final.append(check.model_copy(update={'passed':False,'actual_error':error.code.value}))
                    continue
                still_matches = (decision is None and check.kind=='permission_negative'
                    or decision is not None and canonical_digest(decision)==check.decision_digest)
                final.append(check if still_matches else check.model_copy(update={'passed':False}))
            evidence = FixtureEvidence(catalog_digest=current.catalog.digest,
                execution_contract_digest=execution_contract_digest(current.product.manifest),
                fixture_binding_digest=current.catalog.fixture_binding.digest,
                fixture_model_bundle_digest=current.catalog.fixture_binding.model_bundle_digest,
                checks=ordered_checks(final))
            row = TestRun(id=run_id,product_id=current.product.manifest.product_id,draft_id=draft_id,
                draft_digest=current.draft.content_digest,binding_id=current.binding_row.id,
                binding_digest=current.binding_row.content_digest,policy_id=current.product.manifest.policy_id,
                policy_revision=current.policy_revision,fixture_version=current.catalog.digest,
                evidence=evidence.model_dump(mode='json'),status='passed' if successful_evidence(final) else 'failed',actor_id=actor.id)
            uow.session.add(row)
            await uow.session.flush()
            return filtered_test_run(row,current.review_visible_fields,current.product.manifest)

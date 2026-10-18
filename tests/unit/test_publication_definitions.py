"""Deterministic review and evidence algorithms; no DB/runtime claims."""
from copy import deepcopy
import importlib
from types import SimpleNamespace

import pytest
from gateway.contracts.errors import GatewayError
from gateway.contracts.manifest import validate_manifest

pytestmark = pytest.mark.unit


def api():
    try:
        return SimpleNamespace(service=importlib.import_module('gateway.publishing.service'),
            runner=importlib.import_module('gateway.publishing.test_runner'))
    except ModuleNotFoundError as error:
        pytest.fail(f'Publishing implementation is missing: {error.name}')


def test_diff_is_ordered_field_readable_and_keeps_array_order():
    changes = api().service.definition_changes({'z': 2, 'removed': 'old', 'nested': {'a': 1}, 'array': ['a','b']},
        {'z': 3, 'added': None, 'nested': {'a': 1}, 'array': ['b','a']})
    assert [(c.path,c.kind,c.before,c.after) for c in changes] == [
        ('/added','added',None,None),('/array','changed',('a','b'),('b','a')),
        ('/removed','removed','old',None),('/z','changed',2,3)]


def test_diff_ignores_mapping_order_and_escapes_json_pointer():
    assert api().service.definition_changes({'a':1,'b':2},{'b':2,'a':1}) == ()
    assert api().service.definition_changes({'a/b~c':1},{'a/b~c':2})[0].path == '/a~1b~0c'


def test_diff_compares_json_scalar_types_exactly():
    assert len(api().service.definition_changes({'a':True},{'a':1})) == 1


def test_review_projection_omits_hidden_output_references(valid_manifest):
    manifest=validate_manifest(valid_manifest)
    doc=api().service.review_projection(manifest,('day','segment'))
    assert 'revenue' not in doc['outputs'] and 'revenue' not in doc['units']
    assert all('revenue' not in row for fixture in doc['fixtures'] for row in fixture['expected_rows'])


def test_decimal_evidence_is_lossless_and_records_absolute_relative_tolerance(valid_manifest):
    runner=api().runner
    manifest=validate_manifest(valid_manifest)
    expected=[{'day':'2024-03-11','segment':None,'revenue':'9007199254740993.01'}]
    actual=[{'day':'2024-03-11','segment':None,'revenue':'9007199254740993.02'}]
    exact=runner.compare_rows(manifest,expected,actual,absolute='0',relative='0')
    assert exact.passed is False
    accepted=runner.compare_rows(manifest,expected,actual,absolute='0.01',relative='0')
    assert accepted.passed is True
    assert accepted.expected == tuple(expected) and accepted.actual == tuple(actual)
    assert accepted.absolute_tolerance == '0.01' and accepted.relative_tolerance == '0'
    assert runner.compare_rows(manifest,[{**expected[0],'revenue':'100.00'}],
        [{**actual[0],'revenue':'101.00'}],absolute='0',relative='0.01').passed is True


@pytest.mark.parametrize('order', [(0, 1, 2), (0, 2, 1), (2, 0, 1), (2, 1, 0)])
def test_fixture_rows_are_an_unordered_multiset_without_rewriting_evidence(valid_manifest, order):
    manifest = validate_manifest(valid_manifest)
    expected = list(manifest.fixtures[0].expected_rows)
    actual = [expected[index] for index in order]
    comparison = api().runner.compare_rows(manifest, expected, actual, absolute='0', relative='0')
    assert comparison.passed
    assert comparison.expected == tuple(expected)
    assert comparison.actual == tuple(actual)
    assert manifest.fixtures[0].expected_rows == tuple(expected)


@pytest.mark.parametrize('actual_indices,passed', [
    ([1, 0, 0], True), ([0, 1, 1], False), ([0, 1], False),
    ([0, 0, 1, 1], False), ([0, 0, 2], False),
])
def test_fixture_row_matching_preserves_cardinality_and_duplicate_multiplicity(valid_manifest, actual_indices, passed):
    manifest = validate_manifest(valid_manifest)
    rows = manifest.fixtures[0].expected_rows
    assert api().runner.compare_rows(manifest, [rows[0], rows[0], rows[1]],
        [rows[index] for index in actual_indices], absolute='0', relative='0').passed is passed


@pytest.mark.parametrize('side', ['expected', 'actual'])
@pytest.mark.parametrize('mutation', ['missing', 'extra', 'wrong_type', 'bad_date', 'precision', 'scale', 'null'])
def test_fixture_matching_validates_every_row_before_matching(valid_manifest, side, mutation):
    manifest = validate_manifest(valid_manifest)
    expected = [dict(row) for row in manifest.fixtures[0].expected_rows]
    actual = list(reversed(deepcopy(expected)))
    row = (expected if side == 'expected' else actual)[-1]
    if mutation == 'missing': row.pop('revenue')
    elif mutation == 'extra': row['unexpected'] = 'value'
    elif mutation == 'wrong_type': row['revenue'] = 10.1
    elif mutation == 'bad_date': row['day'] = '2024-02-31'
    elif mutation == 'precision': row['revenue'] = '1' * 37 + '.00'
    elif mutation == 'scale': row['revenue'] = '1.001'
    else: row['revenue'] = None
    assert not api().runner.compare_rows(manifest, expected, actual, absolute='100', relative='100').passed


def test_unordered_fixture_null_is_not_zero_or_a_string(valid_manifest):
    valid_manifest['outputs']['revenue']['nullable'] = True
    manifest = validate_manifest(valid_manifest)
    row = {'day': '2024-03-11', 'segment': None, 'revenue': None}
    zero = {**row, 'revenue': '0.00'}
    assert api().runner.compare_rows(manifest, [row, zero], [zero, row], absolute='100', relative='100').passed
    assert not api().runner.compare_rows(manifest, [row, zero], [zero, zero], absolute='100', relative='100').passed
    assert not api().runner.compare_rows(manifest, [row], [{**row, 'segment': 'None'}], absolute='100', relative='100').passed


def test_tolerance_matching_reassigns_an_ambiguous_first_match(valid_manifest):
    manifest = validate_manifest(valid_manifest)
    row = {'day': '2024-03-11', 'segment': None}
    expected = [{**row, 'revenue': value} for value in ('0.00', '2.00')]
    actual = [{**row, 'revenue': value} for value in ('1.00', '-1.00')]
    assert api().runner.compare_rows(manifest, expected, actual, absolute='1', relative='0').passed


def test_tolerance_matching_uses_all_decimal_dimensions_in_one_assignment(valid_manifest):
    valid_manifest['outputs']['cost'] = {'type': 'decimal', 'precision': 38, 'scale': 2}
    for row in valid_manifest['fixtures'][0]['expected_rows']: row['cost'] = '0.00'
    manifest = validate_manifest(valid_manifest)
    row = {'day': '2024-03-11', 'segment': None}
    expected = [{**row, 'revenue': '0', 'cost': '1'}, {**row, 'revenue': '1', 'cost': '0'}]
    actual = [{**row, 'revenue': '0', 'cost': '0'}, {**row, 'revenue': '1', 'cost': '2'}]
    assert api().runner.compare_rows(manifest, expected, actual, absolute='1', relative='0').passed
    # Each column has a matching multiset, but no whole-row assignment exists.
    expected = [{**row, 'revenue': '0', 'cost': '0'}, {**row, 'revenue': '2', 'cost': '2'}]
    actual = [{**row, 'revenue': '0', 'cost': '2'}, {**row, 'revenue': '2', 'cost': '0'}]
    assert not api().runner.compare_rows(manifest, expected, actual, absolute='0', relative='0').passed


def test_relative_tolerance_remains_relative_to_each_expected_value(valid_manifest):
    manifest = validate_manifest(valid_manifest)
    row = {'day': '2024-03-11', 'segment': None}
    expected = [{**row, 'revenue': value} for value in ('100.00', '-200.00', '0.00')]
    actual = [{**row, 'revenue': value} for value in ('0.00', '-180.00', '90.00')]
    assert api().runner.compare_rows(manifest, expected, actual, absolute='0', relative='0.1').passed
    assert not api().runner.compare_rows(manifest, actual, expected, absolute='0', relative='0.1').passed
    actual[0]['revenue'] = '0.01'
    assert not api().runner.compare_rows(manifest, expected, actual, absolute='0', relative='0.1').passed


def test_unordered_decimal_equivalence_does_not_depend_on_string_scale(valid_manifest):
    manifest = validate_manifest(valid_manifest)
    row = {'day': '2024-03-11', 'segment': None}
    expected = [{**row, 'revenue': value} for value in ('1.00', '-0.00', '9007199254740993.01')]
    actual = [{**row, 'revenue': value} for value in ('9007199254740993.01', '0', '1')]
    assert api().runner.compare_rows(manifest, expected, actual, absolute='0', relative='0').passed


def test_tolerance_matching_handles_a_maximum_length_augmenting_path(valid_manifest):
    manifest = validate_manifest(valid_manifest)
    row = {'day': '2024-03-11', 'segment': None}
    expected = [{**row, 'revenue': str(value)} for value in [*range(2, 2000, 2), 0]]
    actual = [{**row, 'revenue': str(value)} for value in range(1, 2000, 2)]
    assert api().runner.compare_rows(manifest, expected, actual, absolute='1', relative='0').passed


def test_dense_tolerance_candidates_still_require_a_complete_one_to_one_match(valid_manifest):
    manifest = validate_manifest(valid_manifest)
    row = {'day': '2024-03-11', 'segment': None}
    # Every row has candidates; 33 expected zeros share only 32 actual ones.
    # Enumerating possible permutations here would have factorial complexity.
    expected = [{**row, 'revenue': value} for value in ['0'] * 33 + ['2'] * 31]
    actual = [{**row, 'revenue': value} for value in ['1'] * 32 + ['3'] * 32]
    assert not api().runner.compare_rows(manifest, expected, actual, absolute='1', relative='0').passed


def test_maximum_size_dense_tolerance_ambiguity_matches_without_enumerating_permutations(valid_manifest):
    manifest = validate_manifest(valid_manifest)
    row = {'day': '2024-03-11', 'segment': None}
    expected = [{**row, 'revenue': '0'}] * 1000
    actual = [{**row, 'revenue': '1'}] * 1000
    assert api().runner.compare_rows(manifest, expected, actual, absolute='1', relative='0').passed


def test_empty_fixture_multisets_match(valid_manifest):
    assert api().runner.compare_rows(validate_manifest(valid_manifest), [], [], absolute='0', relative='0').passed


def test_fixture_comparison_refuses_more_than_the_fixture_row_bound(valid_manifest):
    manifest = validate_manifest(valid_manifest)
    rows = [manifest.fixtures[0].expected_rows[0]] * 1001
    assert not api().runner.compare_rows(manifest, rows, rows, absolute='0', relative='0').passed


def test_small_multidimensional_matches_agree_with_exhaustive_assignment(valid_manifest):
    from itertools import combinations_with_replacement, permutations
    valid_manifest['outputs']['cost'] = {'type': 'decimal', 'precision': 38, 'scale': 2}
    for row in valid_manifest['fixtures'][0]['expected_rows']: row['cost'] = '0'
    manifest = validate_manifest(valid_manifest)
    row = {'day': '2024-03-11', 'segment': None}
    samples = list(combinations_with_replacement([(0, 0), (0, 2), (2, 0), (2, 2), (1, 1)], 3))
    for expected in samples:
        for actual in samples:
            possible = any(all(abs(a - c) <= 1 and abs(b - d) <= 1
                for (a, b), (c, d) in zip(expected, order)) for order in permutations(actual))
            comparison = api().runner.compare_rows(manifest,
                [{**row, 'revenue': str(a), 'cost': str(b)} for a, b in expected],
                [{**row, 'revenue': str(a), 'cost': str(b)} for a, b in actual], absolute='1', relative='0')
            assert comparison.passed is possible, (expected, actual)


@pytest.mark.parametrize('field,value',[('day','2024-03-10'),('segment','None')])
def test_only_decimal_fields_can_use_tolerance(valid_manifest,field,value):
    row={'day':'2024-03-11','segment':None,'revenue':'1.00'}
    assert not api().runner.compare_rows(validate_manifest(valid_manifest),[row],[{**row,field:value}],absolute='100',relative='100').passed


@pytest.mark.parametrize('absolute,relative',[('-1','0'),('NaN','0'),('0','Infinity'),('1e99','0')])
def test_tolerance_must_be_explicit_bounded_nonnegative_decimal(valid_manifest,absolute,relative):
    with pytest.raises(GatewayError) as caught:
        api().runner.compare_rows(validate_manifest(valid_manifest),[],[],absolute=absolute,relative=relative)
    assert caught.value.code == 'INVALID_INPUT'


def test_evidence_lists_failed_checks_first():
    check=api().runner.FixtureCheck
    passed=check(fixture_id='a',kind='known_answer',passed=True,expected=[],actual=[],absolute_tolerance='0',relative_tolerance='0')
    failed=check(fixture_id='z',kind='permission_negative',passed=False,expected_error='NOT_FOUND_OR_FORBIDDEN',actual_error=None)
    assert [c.fixture_id for c in api().runner.ordered_checks([passed,failed])] == ['z','a']


def test_required_permission_negative_cannot_be_faked_by_other_error():
    check=api().runner.FixtureCheck
    c=check(fixture_id='denied',kind='permission_negative',passed=True,
        expected_error='NOT_FOUND_OR_FORBIDDEN',actual_error='UPSTREAM_UNAVAILABLE')
    assert api().runner.successful_evidence([c]) is False


@pytest.mark.parametrize('reason',['','   ','line\nbreak','postgresql://hidden','Bearer secret', 'x'*1025], ids=['empty', 'whitespace', 'newline', 'database-url', 'bearer', 'oversized'])
def test_lifecycle_reasons_reject_unsafe_values(reason):
    with pytest.raises(GatewayError) as caught: api().service.validate_reason(reason)
    assert caught.value.code == 'INVALID_INPUT'


def test_safe_reason_is_preserved():
    assert api().service.validate_reason('Superseded after reviewed comparison') == 'Superseded after reviewed comparison'


def test_catalog_pins_fixed_principals_and_exact_approved_fixture_content(valid_manifest):
    from tests.contract.test_cube_adapter import synthetic
    binding,product,_,_=synthetic()
    runner=api().runner
    fixture_binding=binding.model_copy(update={'context':'fixture_test','binding_id':'fixture-revenue','model_bundle_digest':'f'*64})
    case=runner.ApprovedFixture(fixture=product.manifest.fixtures[0],principal_id='fixture-north',kind='known_answer')
    denied=runner.ApprovedFixture(fixture=product.manifest.fixtures[0].model_copy(update={'id':'permission-denied','expected_rows':(), 'expected_error':'NOT_FOUND_OR_FORBIDDEN'}),principal_id='fixture-denied',kind='permission_negative')
    catalog=runner.FixtureCatalog(product_id='revenue',consumer_binding=binding,execution_contract_digest=runner.execution_contract_digest(product.manifest),fixture_binding=fixture_binding,cases=[runner.ApprovedFixture(fixture=f,principal_id='fixture-north',kind='input_negative' if f.expected_error else 'known_answer') for f in product.manifest.fixtures]+[denied])
    assert len(catalog.digest)==64
    catalog.approve(product.manifest,binding)
    changed=product.manifest.model_dump(mode='json'); changed.pop('content_digest'); changed['fixtures'][0]['tolerance']='99'
    with pytest.raises(GatewayError) as caught: catalog.approve(validate_manifest(changed),binding)
    assert caught.value.code == 'BINDING_DRIFT'
    assert 'fixture-north' not in caught.value.safe_message


def test_catalog_requires_a_real_permission_negative_case():
    from tests.contract.test_cube_adapter import synthetic
    from pydantic import ValidationError
    binding,product,_,_=synthetic()
    runner=api().runner
    with pytest.raises(ValidationError):
        runner.FixtureCatalog(product_id='revenue',consumer_binding=binding,execution_contract_digest=runner.execution_contract_digest(product.manifest),
            fixture_binding=binding.model_copy(update={'context':'fixture_test','binding_id':'fixture-revenue','model_bundle_digest':'f'*64}),
            cases=[runner.ApprovedFixture(fixture=product.manifest.fixtures[0],principal_id='fixture-north',kind='known_answer')])


@pytest.mark.parametrize('field,value',[('aggregation','Average'),('timezone','UTC'),('policy_id','broader'),('classification','restricted')])
def test_execution_contract_projection_detects_semantic_security_changes(valid_manifest,field,value):
    original=validate_manifest(valid_manifest)
    changed=deepcopy(valid_manifest);changed[field]=value
    assert api().runner.execution_contract_digest(original) != api().runner.execution_contract_digest(validate_manifest(changed))


def test_execution_contract_projection_allows_presentation_changes(valid_manifest):
    original=validate_manifest(valid_manifest)
    changed=deepcopy(valid_manifest);changed.update(description='Better explanation',name='Revenue',version='1.2.0')
    assert api().runner.execution_contract_digest(original) == api().runner.execution_contract_digest(validate_manifest(changed))


def test_draft_attempt_storage_has_no_published_version_or_result_dependency():
    try: models=importlib.import_module('gateway.publishing.models')
    except ModuleNotFoundError: pytest.fail('Separate draft fixture storage is not implemented')
    table=models.FixtureAttempt.__table__
    targets={fk.target_fullname for fk in table.foreign_keys}
    assert 'gateway.draft_revisions.id' in targets
    assert not any('executions.' in target or 'product_versions.' in target for target in targets)
    assert {'draft_digest','consumer_binding_digest','fixture_binding_digest','catalog_digest','actor_id','fixed_principal_id','deadline_at'} <= set(table.columns.keys())
    assert 'reason' in models.PublicationEvent.__table__.columns


def test_passing_boolean_without_all_exact_catalog_checks_is_rejected():
    from tests.contract.test_cube_adapter import synthetic
    from gateway.registry.models import TestRun as Record
    runner=api().runner
    binding,product,_,_=synthetic()
    cases=[runner.ApprovedFixture(fixture=f,principal_id='fixture-north',kind='input_negative' if f.expected_error else 'known_answer') for f in product.manifest.fixtures]
    denied=cases[0].fixture.model_copy(update={'id':'denied','expected_rows':(),'expected_error':'NOT_FOUND_OR_FORBIDDEN'})
    cases.append(runner.ApprovedFixture(fixture=denied,principal_id='fixture-denied',kind='permission_negative'))
    catalog=runner.FixtureCatalog(product_id='revenue',consumer_binding=binding,
        fixture_binding=binding.model_copy(update={'binding_id':'fixture','context':'fixture_test','model_bundle_digest':'f'*64}),
        execution_contract_digest=runner.execution_contract_digest(product.manifest),cases=cases)
    instance=runner.FixtureRunner(registry=None,policy=None,bindings=None,bundles=None,
        catalog_provider=lambda:catalog,adapter_factory=lambda:None)
    test=Record(fixture_version=catalog.digest,status='passed',evidence={'checks':[{'fixture_id':'denied','kind':'permission_negative','passed':True,'expected_error':'NOT_FOUND_OR_FORBIDDEN','actual_error':'NOT_FOUND_OR_FORBIDDEN'}]})
    assert instance.evidence_current(product.manifest,binding,test) is False


def test_fixture_dispatch_rejects_unissued_capability_without_storage():
    import asyncio
    from tests.contract.test_cube_adapter import synthetic
    from gateway.execution.service import FixtureCapability
    runner=api().runner.FixtureRunner(registry=None,policy=None,bindings=None,bundles=None,
        catalog_provider=lambda:None,adapter_factory=lambda:None)
    with pytest.raises(GatewayError) as caught:
        asyncio.run(runner.submit_fixture(None,None,object.__new__(FixtureCapability)))
    assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'


def test_issued_fixture_capability_cannot_be_copied_or_pickled():
    import copy,pickle
    runner=api().runner.FixtureRunner(registry=None,policy=None,bindings=None,bundles=None,
        catalog_provider=lambda:None,adapter_factory=lambda:None)
    cap=runner._issue(None,None,None,None)
    for transform in (copy.copy,copy.deepcopy,pickle.dumps):
        with pytest.raises(TypeError): transform(cap)


def make_catalog_evidence():
    """A real typed catalog/evidence pair, with source calls deliberately absent."""
    from tests.contract.test_cube_adapter import synthetic
    from tests.unit.test_policy import grant, rules
    from gateway.auth.principals import PrincipalGrants
    from gateway.policy.service import PolicyRules, derive_decision
    from gateway.contracts.canonical import canonical_digest
    from gateway.registry.models import TestRun as Record
    binding,product,_,_=synthetic()
    runner=api().runner
    cases=[runner.ApprovedFixture(fixture=f,principal_id='fixture-south' if f.id=='south-total' else 'fixture-north',
        kind='input_negative' if f.expected_error else 'known_answer') for f in product.manifest.fixtures]
    denied=cases[0].fixture.model_copy(update={'id':'denied','expected_rows':(),'expected_error':'NOT_FOUND_OR_FORBIDDEN','source_watermark':None})
    cases.append(runner.ApprovedFixture(fixture=denied,principal_id='fixture-denied',kind='permission_negative'))
    catalog=runner.FixtureCatalog(product_id='revenue',consumer_binding=binding,
        fixture_binding=binding.model_copy(update={'binding_id':'fixture','context':'fixture_test','model_bundle_digest':'f'*64}),
        execution_contract_digest=runner.execution_contract_digest(product.manifest),cases=cases)
    decisions={}
    checks=[]
    for case in cases:
        if case.kind=='permission_negative':
            decision=None
        else:
            tenant='south' if case.principal_id=='fixture-south' else 'north'
            decision=derive_decision(PrincipalGrants(principal_id=case.principal_id,
                grants=[grant(tenant_id=tenant,entitlements={'groups':[tenant]})]),'execute',product,
                PolicyRules.model_validate(rules()),1)
        decisions[case.principal_id]=decision
        checks.append(runner.FixtureCheck(fixture_id=case.fixture.id,kind=case.kind,passed=True,
            expected=case.fixture.expected_rows,actual=case.fixture.expected_rows,
            expected_error=case.fixture.expected_error,actual_error=case.fixture.expected_error,
            absolute_tolerance=case.fixture.tolerance,relative_tolerance=case.relative_tolerance,
            decision_digest=canonical_digest(decision) if decision else None,
            attempt_id=case.fixture.id+'-attempt' if case.kind=='known_answer' else None,
            freshness_status='fresh' if case.kind=='known_answer' else None,
            reference_watermark=case.fixture.source_watermark))
    evidence=runner.FixtureEvidence(catalog_digest=catalog.digest,
        execution_contract_digest=runner.execution_contract_digest(product.manifest),
        fixture_binding_digest=catalog.fixture_binding.digest,
        fixture_model_bundle_digest=catalog.fixture_binding.model_bundle_digest,checks=checks)
    test=Record(draft_id='draft-1',policy_revision=1,fixture_version=catalog.digest,status='passed',
        evidence=evidence.model_dump(mode='json'))
    return binding,product,catalog,test,decisions


@pytest.mark.parametrize('change',[None,'positive_revoked','negative_allowed','fixture_binding_changed','attempt_missing','attempt_failed','attempt_other_draft','attempt_other_case','attempt_other_actor','attempt_wrong_query','attempt_wrong_decision','attempt_wrong_check'])
def test_publication_rechecks_fixed_principal_authority_and_fixture_deployment(change):
    import asyncio
    from gateway.contracts.canonical import canonical_digest
    from gateway.contracts.identity import Principal
    from gateway.auth.principals import PrincipalGrants
    from gateway.policy.service import PolicyContent
    from tests.unit.test_policy import grant, rules
    binding,product,catalog,test,decisions=make_catalog_evidence()
    class Principals:
        async def resolve_principal(self,identity): return Principal(id=identity,subject=identity,kind='demo',service_client_id=None)
        async def get_current(self,identity,**kwargs):
            if identity=='fixture-denied' and change!='negative_allowed' or identity=='fixture-north' and change=='positive_revoked':
                grants=[]
            else:
                tenant='south' if identity=='fixture-south' else 'north'
                grants=[grant(tenant_id=tenant,entitlements={'groups':[tenant]})]
            return PrincipalGrants(principal_id=identity,grants=grants)
    class Audit:
        def event(self,*args,**kwargs): return None
        async def append(self,*args): pass
    class Policy:
        principals=Principals(); audit=Audit()
        # This pure historical-evidence algorithm has no live source authority.
        def requires_source_approval(self,product): return False
        async def _identity(self,*args): pass
        async def _current(self,*args): return SimpleNamespace(revision=1),PolicyContent(rules=rules(),reason='Reviewed')
    class Session:
        async def get(self,model,identity):
            from datetime import datetime, timedelta, timezone
            from gateway.publishing.models import FixtureAttempt, FixtureAttemptState
            from gateway.contracts.query import QueryRequest, validate_query
            case=next(case for case in catalog.cases if case.fixture.id+'-attempt'==identity)
            if change=='attempt_missing': return None
            if model is FixtureAttemptState:
                return SimpleNamespace(attempt_id=identity,error_code=None,state='failed' if change=='attempt_failed' else 'succeeded',
                    source_completed=True,completed_at=datetime(2024,3,12,1,tzinfo=timezone.utc),
                    check_digest='a'*64 if change=='attempt_wrong_check' else canonical_digest(next(c for c in test.evidence['checks'] if c['fixture_id']==case.fixture.id)))
            query=validate_query(product.manifest,QueryRequest(product_id=product.manifest.product_id,
                version=product.manifest.version,inputs=case.fixture.inputs)).model_dump(mode='json',exclude={'idempotency_key'})
            fixture_manifest=catalog.execution_manifest(product.manifest)
            return FixtureAttempt(id=identity,product_id=product.manifest.product_id,
                draft_id='other-draft' if change=='attempt_other_draft' else test.draft_id,
                draft_digest=product.manifest.content_digest,actor_id='other-actor' if change=='attempt_other_actor' else test.actor_id,
                consumer_binding_digest=binding.digest,fixture_binding_digest=catalog.fixture_binding.digest,
                fixture_manifest=fixture_manifest.model_dump(mode='json'),fixture_manifest_digest=fixture_manifest.content_digest,
                execution_contract_digest=api().runner.execution_contract_digest(product.manifest),catalog_digest=catalog.digest,
                policy_id=product.manifest.policy_id,policy_revision=1,
                fixture_id='other-case' if change=='attempt_other_case' else case.fixture.id,fixture_version=case.fixture.version,
                fixed_principal_id=case.principal_id,
                decision={} if change=='attempt_wrong_decision' else decisions[case.principal_id].model_dump(mode='json'),
                canonical_query={} if change=='attempt_wrong_query' else query,canonical_hash=canonical_digest(query),
                created_at=datetime(2024,3,12,0,59,tzinfo=timezone.utc),
                deadline_at=datetime(2024,3,12,1,1,tzinfo=timezone.utc))
        async def scalar(self,statement):
            parameters=statement.compile().params
            selected=catalog.fixture_binding if parameters.get('binding_id_1')=='fixture' else binding
            digest='e'*64 if change=='fixture_binding_changed' and selected.context=='fixture_test' else selected.digest
            return SimpleNamespace(content_digest=digest,model_bundle_digest=selected.model_bundle_digest,adapter_revision=selected.adapter_revision)
    class Bindings:
        async def resolve(self,identity,revision): return catalog.fixture_binding if identity=='fixture' else binding
    class Bundles:
        def resolve(self,digest): return SimpleNamespace(context='fixture_test' if digest=='f'*64 else 'consumer')
    runner=api().runner.FixtureRunner(registry=None,policy=Policy(),bindings=Bindings(),bundles=Bundles(),
        catalog_provider=lambda:catalog,adapter_factory=lambda:None)
    assert runner.evidence_current(product.manifest,binding,test)
    causes=asyncio.run(runner.evidence_stale_causes(product.manifest,binding,test,SimpleNamespace(session=Session())))
    assert causes==(() if change is None else ('fixtures',) if change=='fixture_binding_changed' or change.startswith('attempt_') else ('policy',))


def test_catalog_evidence_must_preserve_reference_watermark():
    binding,product,catalog,test,_=make_catalog_evidence()
    runner=api().runner.FixtureRunner(registry=None,policy=None,bindings=None,bundles=None,
        catalog_provider=lambda:catalog,adapter_factory=lambda:None)
    assert runner.evidence_current(product.manifest,binding,test)
    test.evidence['checks'][0]['reference_watermark']='2025-01-01T00:00:00Z'
    assert not runner.evidence_current(product.manifest,binding,test)


def test_fixture_deadline_migration_and_model_use_typed_tight_limit(monkeypatch):
    """Offline SQL contract only; actual trigger enforcement has a DB gate."""
    from importlib.util import spec_from_file_location, module_from_spec
    from pathlib import Path
    import gateway.publishing.models as models
    spec=spec_from_file_location('fixture_migration',Path('migrations/versions/0004_publishing_fixtures.py'))
    migration=module_from_spec(spec);spec.loader.exec_module(migration)
    statements=[]
    monkeypatch.setattr(migration,'roles',lambda:{'owner':'owner','app':'app','audit_retention':'retention','source_reader':'reader'})
    monkeypatch.setattr(migration.op,'execute',statements.append)
    migration.upgrade()
    guard=next(statement for statement in statements if 'CREATE FUNCTION gateway.guard_fixture_attempt()' in statement)
    table=next(statement for statement in statements if 'CREATE TABLE gateway.fixture_attempts (' in statement)
    assert "execution_timeout_seconds" in guard
    assert "draft.manifest->'limits'" in guard and "policy_rules->'limits'" in guard
    assert "current_revision" in guard and "FOR UPDATE" in guard
    assert "least(product_seconds,policy_seconds,120)" in guard
    assert "jsonb_typeof" in guard and "timeout_seconds" in guard
    assert "deadline_at>created_at" in table and "deadline_at<=created_at+interval '120 seconds'" in table
    model_checks=' '.join(str(check.sqltext) for check in models.FixtureAttempt.__table__.constraints if hasattr(check,'sqltext'))
    assert 'deadline_at > created_at' in model_checks
    assert 'execution_timeout_seconds' in model_checks


def test_reviewed_revenue_catalog_asset_pins_existing_reference_definition():
    import json
    from pathlib import Path
    from gateway.adapters.base import CubeBinding
    raw=json.loads(Path('fixtures/products/revenue.json').read_text())
    manifest=validate_manifest(raw)
    binding=CubeBinding.model_validate(json.loads(Path('fixtures/bindings/revenue.json').read_text()))
    catalog=api().runner.FixtureCatalog.model_validate(json.loads(Path('fixtures/products/revenue.tests.json').read_text()))
    catalog.approve(manifest,binding)
    assert {c.principal_id for c in catalog.cases}=={'fixture-north','fixture-south','fixture-denied'}
    assert sum(c.kind=='permission_negative' for c in catalog.cases)==1


@pytest.mark.parametrize('closing',['raises','blocks'])
def test_fixture_adapter_cleanup_cannot_mask_or_extend_terminal_failure(clock,closing):
    import asyncio
    from contextlib import asynccontextmanager
    from datetime import timedelta
    from gateway.contracts.execution import ExecutionSpec
    from gateway.contracts.canonical import canonical_digest
    from tests.contract.test_cube_adapter import synthetic
    _,product,decision,request=synthetic()
    from gateway.contracts.query import validate_query
    request=validate_query(product.manifest,request)
    canonical=request.model_dump(mode='json',exclude={'idempotency_key'})
    spec=ExecutionSpec(principal_id=decision.principal_id,pinned_product=product,
        canonical_query=canonical,canonical_hash=canonical_digest(canonical),policy_revision=1,
        context='fixture_test',request_id='fixture-cleanup')
    class Registry:
        @asynccontextmanager
        async def transaction(self): yield None
    class BrokenAdapter:
        async def prepare(self,*args,**kwargs):
            raise GatewayError('UPSTREAM_UNAVAILABLE','fixture-cleanup','Source unavailable')
        async def close(self):
            if closing=='raises': raise RuntimeError('Internal close detail')
            await asyncio.sleep(30)
    class Runner(api().runner.FixtureRunner):
        def _spec(self,attempt): return spec
        async def _gate(self,*args,**kwargs): return None,None,None,None,()
        async def _finish_failed(self,attempt,code,check):
            assert code=='UPSTREAM_UNAVAILABLE' and check.actual_error==code and not check.actual
    runner=Runner(registry=Registry(),policy=None,bindings=None,bundles=None,
        catalog_provider=lambda:None,adapter_factory=BrokenAdapter,clock=clock)
    from gateway.publishing.models import FixtureAttempt
    attempt=FixtureAttempt(id='cleanup-attempt',deadline_at=clock.now()+timedelta(seconds=120),request_id='fixture-cleanup')
    case=next(case for case in make_catalog_evidence()[2].cases if case.kind=='known_answer')
    async def scenario():
        async with asyncio.timeout(2):
            with pytest.raises(GatewayError) as caught:
                await runner._execute_case(None,case,attempt,decision)
            assert caught.value.code=='UPSTREAM_UNAVAILABLE'
    asyncio.run(scenario())


def test_missing_fixed_identity_is_failed_evidence_not_a_permission_negative_success():
    import asyncio
    from contextlib import asynccontextmanager
    binding,product,catalog,_,_=make_catalog_evidence()
    stored=[]
    class Session:
        def add(self,row): stored.append(row)
        async def flush(self): pass
    class Registry:
        @asynccontextmanager
        async def transaction(self): yield SimpleNamespace(session=Session())
    authority=SimpleNamespace(catalog=catalog,product=product,policy_revision=1,review_visible_fields=('day','segment','revenue'),
        draft=SimpleNamespace(content_digest=product.manifest.content_digest),
        binding_row=SimpleNamespace(id='binding-1',content_digest=binding.digest))
    class Runner(api().runner.FixtureRunner):
        async def _authority(self,*args): return authority
        async def _case_start(self,authority,case):
            if case.kind!='permission_negative': raise api().runner.fixture_unavailable()
            return None,None,None
        async def _data_decision(self,uow,authority,case):
            if case.kind!='permission_negative': raise api().runner.fixture_unavailable()
            return None,None
    runner=Runner(registry=Registry(),policy=SimpleNamespace(requires_source_approval=lambda product:False),bindings=None,bundles=None,
        catalog_provider=lambda:catalog,adapter_factory=lambda:None)
    run=asyncio.run(runner.run(SimpleNamespace(id='author'),'draft-1'))
    assert run.status=='failed' and len(stored)==1
    checks=run.evidence['checks']
    assert all(not c['passed'] and c['actual_error']=='BINDING_DRIFT' for c in checks if c['kind']!='permission_negative')
    assert next(c for c in checks if c['kind']=='permission_negative')['actual_error']=='NOT_FOUND_OR_FORBIDDEN'


def test_returned_test_evidence_filters_fields_without_rewriting_immutable_proof():
    _,product,_,stored,_=make_catalog_evidence()
    stored.id='test-1';stored.actor_id='author-1';stored.draft_digest='a'*64
    before=deepcopy(stored.evidence)
    view=api().runner.filtered_test_run(stored,('day','segment'),product.manifest)
    assert view is not stored and view.id==stored.id and view.draft_digest==stored.draft_digest
    assert view.actor_id==stored.actor_id and view.status==stored.status
    assert stored.evidence==before
    assert all('revenue' not in row for check in view.evidence['checks'] for rows in ('expected','actual') for row in check[rows])
    assert all('revenue' in row for check in stored.evidence['checks'] for row in check['expected'])


def test_policy_visibility_tightening_between_run_and_review_denies_original_fixture_owner():
    import asyncio
    context=review_service(grant_kind='author',visible=('day','segment','revenue'))
    before=deepcopy(context.stored.evidence)
    async def run(*args):
        context.settings['visible']=('day','segment')
        return context.stored
    context.service.fixture_runner.run=run
    with pytest.raises(GatewayError) as caught:
        asyncio.run(context.service.test(context.actor,context.draft.id))
    assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
    assert not context.access.owner_batches
    assert context.records[-1].decision=='deny' and context.records[-1].action=='result'
    assert context.stored.evidence==before
    with pytest.raises(GatewayError):
        asyncio.run(context.service.get_test(context.actor,context.draft.id,context.stored.id))
    assert context.stored.evidence==before


def test_publishing_service_exposes_existing_route_test_read_helper():
    assert hasattr(api().service.PublishingService,'get_test'), 'Existing test-read route needs shared authorized projection'


def test_malformed_stored_evidence_never_exposes_raw_validation_details():
    _,product,_,stored,_=make_catalog_evidence()
    stored.evidence={'untrusted_detail':'http://internal-source.example/private'}
    with pytest.raises(GatewayError) as caught:
        api().runner.filtered_test_run(stored,('day','segment'),product.manifest)
    assert caught.value.code=='BINDING_DRIFT'
    assert 'internal-source' not in str(caught.value)


def test_fixture_source_authorization_uses_attempt_correlation_without_raw_parameters():
    import asyncio
    from gateway.contracts.identity import Principal
    from gateway.auth.principals import PrincipalGrants
    from gateway.policy.service import PolicyContent
    from gateway.policy.audit import AuditWriter
    from tests.unit.test_policy import grant, rules
    _,product,catalog,_,_=make_catalog_evidence()
    case=catalog.cases[0]
    written=[]
    class Principals:
        async def resolve_principal(self,identity): return Principal(id=identity,subject=identity,kind='demo',service_client_id=None)
        async def get_current(self,identity,**kwargs): return PrincipalGrants(principal_id=identity,grants=[grant()])
    class Writer(AuditWriter):
        async def append(self,uow,event): written.append(event)
    class Policy:
        principals=Principals();audit=Writer(b'operator-synthetic-hmac-key-32!!')
        # Exercise only the legacy audit shape with this inert policy double.
        def requires_source_approval(self,product): return False
        async def _identity(self,*args): pass
        async def _current(self,*args): return SimpleNamespace(revision=1),PolicyContent(rules=rules(),reason='Reviewed')
    runner=api().runner.FixtureRunner(registry=None,policy=Policy(),bindings=None,bundles=None,
        catalog_provider=lambda:catalog,adapter_factory=lambda:None)
    asyncio.run(runner._decision_for_case(SimpleNamespace(session=None),product,1,case,correlation_id='fixture-attempt-request'))
    assert written[0].correlation_id=='fixture-attempt-request'
    assert written[0].actor_id==case.principal_id and written[0].parameter_names==('end','start')
    assert '2024-03-09' not in str(written[0].model_dump())


def test_failed_fixture_check_preserves_real_attempt_identity():
    import asyncio
    from contextlib import asynccontextmanager
    from gateway.publishing.models import FixtureAttempt
    binding,product,catalog,_,decisions=make_catalog_evidence()
    class Session:
        def add(self,row): pass
        async def flush(self): pass
    class Registry:
        @asynccontextmanager
        async def transaction(self): yield SimpleNamespace(session=Session())
    authority=SimpleNamespace(catalog=catalog,product=product,policy_revision=1,review_visible_fields=('day','segment','revenue'),
        draft=SimpleNamespace(content_digest=product.manifest.content_digest),
        binding_row=SimpleNamespace(id='binding-1',content_digest=binding.digest))
    class Runner(api().runner.FixtureRunner):
        async def _authority(self,*args): return authority
        async def _case_start(self,authority,case):
            decision=decisions[case.principal_id]
            if case.kind=='known_answer': return FixtureAttempt(id=case.fixture.id+'-attempt'),None,decision
            return ('INVALID_INPUT' if case.kind=='input_negative' else None),None,decision
        def _spec(self,attempt): return None
        async def _dispatch(self,actor,spec,capability):
            self._issued.pop(id(capability),None)
            raise GatewayError('UPSTREAM_UNAVAILABLE','attempt-request','Source unavailable')
        async def _data_decision(self,uow,authority,case): return None,decisions[case.principal_id]
    runner=Runner(registry=Registry(),policy=SimpleNamespace(requires_source_approval=lambda product:False),bindings=None,bundles=None,catalog_provider=lambda:catalog,adapter_factory=lambda:None)
    run=asyncio.run(runner.run(SimpleNamespace(id='author'),'draft-1'))
    known=[c for c in run.evidence['checks'] if c['kind']=='known_answer']
    assert run.status=='failed'
    assert all(c['attempt_id']==c['fixture_id']+'-attempt' and c['decision_digest'] for c in known)


@pytest.mark.parametrize('visible,capabilities,editable',[
    (('day','segment','revenue'),('author',),True),
    (('day','segment'),('author',),False),
    (('day','segment','revenue'),('publish',),False),
])
def test_authorized_draft_view_is_filtered_detached_and_editability_is_server_derived(visible,capabilities,editable):
    import asyncio
    context=review_service(grant_kind='publisher' if 'publish' in capabilities else 'author',visible=visible)
    view=asyncio.run(context.service.get_draft(context.actor,context.draft.id))
    assert view.id==context.draft.id and view.product_id=='revenue' and view.revision==1
    assert view.digest==context.draft.content_digest and view.editable_full_definition is editable
    assert set(view.definition['outputs'])==set(visible)
    assert 'content_digest' not in view.definition
    assert view.definition['fixtures'][0]['expected_rows']
    assert all(set(row)<=set(visible) for fixture in view.definition['fixtures'] for row in fixture['expected_rows'])
    with pytest.raises((TypeError,ValueError)): view.definition['name']='Altered review'


def test_update_draft_exposes_url_bound_shared_service_seam():
    assert hasattr(api().service.PublishingService,'update_draft'), 'PUT needs atomic URL draft identity validation'


@pytest.mark.parametrize('model_name,protected',[
    ('FixtureAttemptState',{'completed_at','check_digest'}),
    ('FixtureSourceReservation',{'released_at','release_reason'}),
])
def test_fixture_orm_inserts_do_not_request_server_owned_column_privileges(model_name,protected):
    """Real ORM insert planning only; DB privilege enforcement is separately gated."""
    from sqlalchemy import inspect
    from gateway.publishing import models
    model=getattr(models,model_name)
    # SQLAlchemy's unit-of-work insert planner otherwise emits explicit NULL for
    # unassigned nullable columns, which violates the deliberately narrow GRANT.
    assert not protected & inspect(model)._insert_cols_as_none[model.__table__]


def review_service(*, grant_kind='mixed', visible=('day','segment'), tenants=('north',)):
    """Real policy/pure approval contracts; in-memory sequencing, not SQL authority."""
    from contextlib import asynccontextmanager
    from gateway.auth.principals import PrincipalGrants
    from gateway.contracts.identity import Principal
    from gateway.policy.audit import AuditWriter
    from gateway.policy.service import PolicyContent, PolicyService
    from tests.unit.test_policy import grant, rules
    from gateway.contracts.execution import PinnedProduct
    from tests.unit.source_fixture_probes import RetainedFixtureProbe
    from gateway.contracts.canonical import canonical_digest
    from gateway.policy.service import derive_decision, PolicyRules
    from uuid import uuid4
    from tests.unit.test_source_approval_contracts import package
    _, original, catalog, stored, _ = make_catalog_evidence()
    # This north-only review scope cannot borrow a retained south case owner.
    cases=tuple(case for case in catalog.cases if case.principal_id!='fixture-south')
    raw=original.manifest.model_dump(mode='json',exclude={'content_digest'})
    raw['fixtures']=[fixture for fixture in raw['fixtures'] if fixture['id'] in {case.fixture.id for case in cases}]
    document, manifest = package.__wrapped__(raw)
    catalog=catalog.model_copy(update={'cases':cases,'execution_contract_digest':api().runner.execution_contract_digest(manifest)})
    stored.fixture_version=catalog.digest
    stored.evidence['checks']=[check for check in stored.evidence['checks'] if check['fixture_id'] in {case.fixture.id for case in cases}]
    stored.evidence['catalog_digest']=catalog.digest
    stored.evidence['execution_contract_digest']=catalog.execution_contract_digest
    document['audiences'][0].update(principal_id='reviewer', client_ids=['review-client'], actions=['author','publish'])
    for domain in document['input_domains'] + document['filter_domains']:
        if domain['type'] == 'date': domain['maximum'] = '2030-01-01'
    product = PinnedProduct(manifest=manifest, binding_digest=document['bindings']['consumer']['content_digest'], version_id='draft-1')
    stored.id=str(uuid4()); stored.product_id='revenue'; stored.policy_id=product.manifest.policy_id
    stored.actor_id='original-author'
    original_actor=Principal(id='original-author',subject='original-author',kind='user',service_client_id='author-client')
    document['artifacts']['fixture_catalog_digest']=catalog.digest
    invoker=deepcopy(document['audiences'][0])
    invoker.update(id='fixture-invoker',context='fixture_test',principal_id=original_actor.id,
        client_ids=[original_actor.service_client_id],actions=['author'])
    document['audiences'].append(invoker)
    for case in catalog.cases:
        tenant='south' if case.principal_id=='fixture-south' else 'north'
        entry=deepcopy(document['audiences'][0])
        entry.update(id='fixture-'+case.fixture.id,context='fixture_test',principal_id=case.principal_id,
            principal_kind='demo',client_ids=[None],actions=['execute','result'],fixture_case_ids=[case.fixture.id],tenant_id=tenant)
        entry['row_constraints']=[dict(member='Orders.tenant_id',operator='eq',values=[tenant]),
            dict(member='Orders.access_group',operator='in',values=[tenant])]
        document['audiences'].append(entry)
    stored.draft_digest=product.manifest.content_digest
    draft=SimpleNamespace(id=stored.draft_id,product_id='revenue',revision=1,
        content_digest=stored.draft_digest,manifest=product.manifest.model_dump(mode='json'))
    head=SimpleNamespace(latest_draft_id=draft.id,draft_revision=1,current_version_id=None)
    north=grant(capabilities=('publish',) if grant_kind=='publisher' else ('author',))
    grants=[north]
    if grant_kind=='both': grants=[grant(capabilities=('author','publish'))]
    if grant_kind=='mixed': grants.append(grant(tenant_id='south',capabilities=('publish',)))
    if grant_kind=='unrelated': grants=[grant(policy_id='unrelated',capabilities=('author','publish'))]
    if grant_kind=='revoked': grants=[]
    records=[]; transactions=[]
    settings={'visible':visible}; drafts={draft.id:draft}; versions={}; selected=[]
    class Session:
        aborted=False
        async def scalar(self,statement):
            assert not self.aborted, 'Authorization rollback must never be resumed'
            from gateway.publishing.models import FixtureAttemptState
            if statement.column_descriptions[0]['entity'] is FixtureAttemptState:
                identity=statement.compile().params['attempt_id_1']
                return SimpleNamespace(attempt_id=identity,state='succeeded',source_completed=True) if identity in access.attempts else None
            return stored
        async def rollback(self): self.aborted=True
        async def get(self,model,identity): return versions.get(identity)
    class Registry:
        @asynccontextmanager
        async def transaction(self):
            session=Session(); transactions.append(session)
            yield SimpleNamespace(session=session)
    class Principals:
        async def get_current(self,identity,**kwargs):
            assert not kwargs['session'].aborted
            return PrincipalGrants(principal_id=identity,grants=grants if identity=='reviewer' else fixed_grants[identity])
    class Writer(AuditWriter):
        async def append(self,uow,event):
            assert not uow.session.aborted
            records.append(event)
    class Policy(PolicyService):
        async def _identity(self,*args):
            if settings.get('identity_error') is not None:
                raise settings['identity_error']
        async def _current(self,*args):
            return SimpleNamespace(revision=1),PolicyContent(rules=rules(tenant_ids=tenants,visible_fields=settings['visible']),reason='Reviewed')
    class FixtureRunner(api().runner.FixtureRunner):
        async def run(self,*args): return stored
    class Service(api().service.PublishingService):
        async def _draft(self,uow,draft_id):
            selected=drafts[draft_id]
            return head,selected,api().service.review_target(validate_manifest(selected.manifest),selected.id,product.binding_digest)
        async def _binding(self,*args): pass
        async def _latest_test(self,*args): return stored
        async def _stale(self,*args):
            selected.append(args[-1])
            return ()
    registry=Registry()
    audit = Writer(b'operator-synthetic-hmac-key-32!!')
    access = RetainedFixtureProbe(registry,audit)
    fixed_grants={original_actor.id:[grant(capabilities=('author',))]}
    for case in catalog.cases:
        tenant='south' if case.principal_id=='fixture-south' else 'north'
        fixed_grants[case.principal_id]=[] if case.kind=='permission_negative' else [grant(tenant_id=tenant,entitlements={'groups':[tenant]})]
    # The same current policy applies to reviewer and original owners.
    def current_decision(identity,action):
        return derive_decision(PrincipalGrants(principal_id=identity,grants=fixed_grants[identity]),
            action,product,PolicyRules.model_validate(rules(visible_fields=settings['visible'])),1)
    access.current_author_decision=lambda:current_decision(original_actor.id,'author')
    access.current_decision=current_decision
    def approve(manifest, *, actions=('author','publish')):
        from gateway.contracts.canonical import manifest_digest
        variant = deepcopy(document)
        variant['audiences'][0]['actions'] = list(actions)
        variant['product'].update(product_id=manifest.product_id,version=manifest.version,
            manifest_digest=manifest_digest(manifest),policy_id=manifest.policy_id)
        return access.install(variant,manifest)
    approve(manifest)
    decisions={identity:current_decision(identity,'author' if identity==original_actor.id else 'execute')
        for identity in fixed_grants if identity!='fixture-denied'}
    for check in stored.evidence['checks']:
        case=next(case for case in catalog.cases if case.fixture.id==check['fixture_id'])
        check['decision_digest']=canonical_digest(decisions[case.principal_id]) if case.principal_id in decisions else None
    access.retain(product,catalog,stored,original_actor,decisions)
    policy=Policy(registry,Principals(),audit,source_access=access)
    runner=FixtureRunner(registry=registry,policy=policy,bindings=None,bundles=None,
        catalog_provider=lambda:catalog,adapter_factory=lambda:None)
    service=Service(registry=registry,policy=policy,bindings=None,bundles=None,fixture_runner=runner)
    return SimpleNamespace(service=service,actor=Principal(id='reviewer',subject='reviewer',kind='user',service_client_id='review-client'),
        draft=draft,head=head,stored=stored,records=records,transactions=transactions,grants=grants,
        settings=settings,drafts=drafts,versions=versions,selected=selected,approve=approve,access=access,
        original_actor=original_actor,fixed_grants=fixed_grants)


@pytest.mark.parametrize('operation',['get_draft','diff','get_test','test'])
def test_review_uses_current_exact_tenant_author_when_other_tenant_can_publish(operation):
    import asyncio
    context=review_service()
    args=[context.actor,context.draft.id]
    if operation=='get_test': args.append(context.stored.id)
    view=asyncio.run(getattr(context.service,operation)(*args))
    assert view is not None
    assert all(record.action=='author' and record.decision=='allow' for record in context.records if record.actor_id==context.actor.id)
    if operation != 'get_draft':
        assert context.access.owner_batches and any(record.action=='result' for record in context.records)
    if operation=='get_draft': assert 'revenue' not in view.definition['outputs']
    if operation in {'get_test','test'}:
        assert all('revenue' not in row for check in view.evidence['checks'] for name in ('expected','actual') for row in check[name])


@pytest.mark.parametrize('operation',['get_draft','diff','get_test'])
@pytest.mark.parametrize('kind,tenants,allowed',[
    ('author',('north',),True),('publisher',('north',),True),
    ('unrelated',('north',),False),('revoked',('north',),False),
    ('mixed',('north','south'),False),
])
def test_review_keeps_exact_scope_roles_and_denial_audits(operation,kind,tenants,allowed):
    import asyncio
    context=review_service(grant_kind=kind,tenants=tenants)
    args=[context.actor,context.draft.id]
    if operation=='get_test': args.append(context.stored.id)
    if allowed:
        assert asyncio.run(getattr(context.service,operation)(*args)) is not None
        assert context.records[-1].decision=='allow'
    else:
        with pytest.raises(GatewayError) as caught:
            asyncio.run(getattr(context.service,operation)(*args))
        assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
        assert len(context.records)==1 and context.records[0].decision=='deny'
        assert context.transactions[0].aborted


@pytest.mark.parametrize('source_error',['STALE_DATA','FRESHNESS_UNKNOWN','BINDING_DRIFT','RESOURCE_LIMIT','UPSTREAM_UNAVAILABLE','NOT_FOUND_OR_FORBIDDEN'])
def test_completed_fixture_failure_release_requires_final_authority_and_exact_source_proof(clock,source_error):
    import asyncio
    from contextlib import asynccontextmanager
    from datetime import timedelta
    from gateway.contracts.canonical import canonical_digest
    from gateway.contracts.execution import Completed, PreparedQuery
    from gateway.contracts.query import QueryRequest, validate_query
    from gateway.publishing.models import FixtureAttempt, FixtureAttemptState
    _,product,catalog,_,decisions=make_catalog_evidence()
    case=next(c for c in catalog.cases if c.kind=='known_answer')
    decision=decisions[case.principal_id]
    manifest=catalog.execution_manifest(product.manifest)
    query=validate_query(manifest,QueryRequest(product_id=manifest.product_id,version=manifest.version,
        inputs=case.fixture.inputs)).model_dump(mode='json',exclude={'idempotency_key'})
    attempt=FixtureAttempt(id='completed-attempt',draft_id='draft-1',fixed_principal_id=case.principal_id,
        fixture_manifest=manifest.model_dump(mode='json'),fixture_binding_digest=catalog.fixture_binding.digest,
        canonical_query=query,canonical_hash=canonical_digest(query),policy_revision=1,runner_token='exact-token',
        request_id='completed-request',created_at=clock.now(),deadline_at=clock.now()+timedelta(seconds=120))
    state=FixtureAttemptState(attempt_id=attempt.id,state='running',source_completed=False)
    released=[]; terminal=[]; transaction=[]
    class Session:
        async def scalar(self,statement): return state
        async def flush(self):
            if state.state!='running': terminal.append((state.state,state.source_completed,state.error_code,len(transaction)))
        async def execute(self,statement):
            if statement.is_update:
                assert state.state=='failed' and state.source_completed
                params=statement.compile().params
                assert attempt.id in params.values() and attempt.runner_token in params.values()
                released.append((params,len(transaction)))
    class Registry:
        @asynccontextmanager
        async def transaction(self):
            transaction.append('open')
            yield SimpleNamespace(session=Session())
            transaction.append('committed')
    class Adapter:
        async def prepare(self,*args,**kwargs):
            return PreparedQuery(endpoint_id='synthetic-fixture',model_bundle_digest=catalog.fixture_binding.model_bundle_digest,cube_query={})
        async def poll(self,*args,**kwargs):
            if source_error=='UPSTREAM_UNAVAILABLE': raise GatewayError(source_error,'completed-request','Uncertain source')
            rows=[{catalog.fixture_binding.fields[k].response_key:v for k,v in row.items()} for row in case.fixture.expected_rows]
            if source_error=='BINDING_DRIFT': rows[0]['Orders.revenue']='invalid-decimal'
            if source_error=='RESOURCE_LIMIT': rows=rows[:1]*(manifest.limits.max_rows+1)
            watermark=None if source_error=='FRESHNESS_UNKNOWN' else clock.now()-timedelta(days=2 if source_error=='STALE_DATA' else 0)
            return Completed(raw_rows=rows,source_watermark=watermark)
        async def close(self): pass
    class Runner(api().runner.FixtureRunner):
        async def _gate(self,*args,action='execute',**kwargs):
            if action=='result' and source_error=='NOT_FOUND_OR_FORBIDDEN':
                raise GatewayError(source_error,'completed-request','Current authority revoked')
            return None,decision,state,clock.now(),()
    runner=Runner(registry=Registry(),policy=None,bindings=None,
        bundles=SimpleNamespace(resolve_query=lambda *args:SimpleNamespace(context='fixture_test')),
        catalog_provider=lambda:catalog,adapter_factory=Adapter,clock=clock)
    async def scenario():
        if source_error in {'UPSTREAM_UNAVAILABLE','NOT_FOUND_OR_FORBIDDEN'}:
            with pytest.raises(GatewayError) as caught:
                await runner._execute_case(SimpleNamespace(product=product,catalog=catalog),case,attempt,decision)
            assert caught.value.code==source_error
            assert not state.source_completed and not released
        else:
            check=await runner._execute_case(SimpleNamespace(product=product,catalog=catalog),case,attempt,decision)
            assert not check.passed and check.actual_error==source_error and check.actual==()
            assert check.attempt_id==attempt.id and check.decision_digest==canonical_digest(decision)
            assert state.source_completed and len(released)==1
            assert terminal[-1][-1]==released[0][-1], 'Terminal proof and exact reservation release must commit together'
        assert state.state=='failed' and state.error_code==source_error
        expected_check=(runner._failed_check(case,attempt,decision,source_error)
            if source_error in {'UPSTREAM_UNAVAILABLE','NOT_FOUND_OR_FORBIDDEN'} else check)
        assert state.check_digest==canonical_digest(expected_check)
    asyncio.run(scenario())


@pytest.mark.parametrize('kind,can_test,can_publish',[('author',True,False),('publisher',False,True),('mixed',True,False),('both',True,True)])
def test_draft_view_current_identity_and_capabilities_are_authorized_hints(kind,can_test,can_publish):
    import asyncio
    context=review_service(grant_kind=kind)
    view=asyncio.run(context.service.get_draft(context.actor,context.draft.id))
    assert view.current_draft_id==context.draft.id and view.is_current_revision
    assert view.can_test is can_test and view.can_publish is can_publish
    # Hints are neither current evidence nor a bypass for either mutation gate.
    assert view.editable_full_definition is False
    assert 'capability' in api().service.DraftView.model_json_schema()['properties']['can_publish']['description'].lower()
    assert api().service.DraftView.model_validate(view.model_dump(mode='python',warnings='error'))==view


@pytest.mark.parametrize('head_authorized',[True,False])
def test_old_draft_locator_independently_authorizes_current_head_without_resuming_denial(head_authorized):
    import asyncio
    context=review_service(grant_kind='author')
    raw=deepcopy(context.draft.manifest); raw.pop('content_digest'); raw['version']='1.1.0'
    if not head_authorized: raw['policy_id']='another-policy'
    manifest=validate_manifest(raw)
    latest=SimpleNamespace(id='draft-new',product_id='revenue',revision=2,content_digest=manifest.content_digest,
        manifest=manifest.model_dump(mode='json'))
    context.approve(manifest)  # Independently reviewed exact test package; policy denial remains independent.
    context.drafts[latest.id]=latest;context.head.latest_draft_id=latest.id;context.head.draft_revision=2
    view=asyncio.run(context.service.get_draft(context.actor,context.draft.id))
    assert view.id==context.draft.id and view.revision==1 and not view.is_current_revision
    assert view.current_draft_id==(latest.id if head_authorized else None)
    if not head_authorized:
        assert context.records[-1].decision=='deny'
        assert 'draft-new' not in str(view.model_dump())
        assert context.transactions[-2].aborted and not context.transactions[-1].aborted


@pytest.mark.parametrize('base_publish',[True,False])
def test_draft_publish_hint_requires_current_base_and_target_scopes(base_publish):
    import asyncio
    from tests.unit.test_policy import grant
    context=review_service(grant_kind='both')
    raw=deepcopy(context.draft.manifest);raw.pop('content_digest');raw['policy_id']='prior-policy'
    manifest=validate_manifest(raw)
    context.approve(manifest)  # Explicit exact base package, never an implicit source exemption.
    context.head.current_version_id='published-1'
    context.versions['published-1']=SimpleNamespace(id='published-1',manifest=manifest.model_dump(mode='json'),
        content_digest=manifest.content_digest,binding_digest='b'*64)
    if base_publish: context.grants.append(grant(policy_id='prior-policy',capabilities=('publish',)))
    view=asyncio.run(context.service.get_draft(context.actor,context.draft.id))
    assert view.can_publish is base_publish and view.can_test


@pytest.mark.parametrize('run_scope',['exact','wrong_product','wrong_draft','wrong_policy','wrong_digest','missing'])
def test_version_diff_carries_target_identity_and_only_reviewable_selected_test_id(run_scope):
    import asyncio
    context=review_service(grant_kind='author')
    if run_scope=='missing':
        async def missing(*args): return None
        context.service._latest_test=missing
    elif run_scope!='exact':
        field={'wrong_product':'product_id','wrong_draft':'draft_id','wrong_policy':'policy_id','wrong_digest':'draft_digest'}[run_scope]
        setattr(context.stored,field,'b'*64 if field=='draft_digest' else 'unrelated')
    if run_scope not in {'exact','missing'}:
        with pytest.raises(GatewayError) as caught:
            asyncio.run(context.service.diff(context.actor,context.draft.id))
        assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
        assert not context.access.owner_batches and not context.records
        return
    view=asyncio.run(context.service.diff(context.actor,context.draft.id))
    assert (view.draft_id,view.product_id,view.revision)==(context.draft.id,'revenue',1)
    assert view.latest_test_run_id==(context.stored.id if run_scope=='exact' else None)
    assert context.selected==[None if run_scope=='missing' else context.stored]
    assert api().service.VersionDiff.model_validate(view.model_dump(mode='python',warnings='error'))==view
    changes={change.path:change for change in view.changes}
    assert changes['/examples'].after
    assert any(row['segment'] is None for fixture in changes['/fixtures'].after for row in fixture['expected_rows'])
    with pytest.raises((TypeError,ValueError)):
        changes['/fixtures'].after[0]['id']='changed'


def test_review_storage_failure_retains_denial_correlation_and_hides_internal_details():
    import asyncio
    from sqlalchemy.exc import SQLAlchemyError
    context=review_service(grant_kind='author')
    context.settings['identity_error']=SQLAlchemyError('private-infrastructure-detail')
    with pytest.raises(GatewayError) as caught:
        asyncio.run(context.service.get_draft(context.actor,context.draft.id))
    assert caught.value.code=='UPSTREAM_UNAVAILABLE'
    assert context.records[-1].decision=='deny'
    assert context.records[-1].correlation_id==caught.value.request_id
    assert 'private-infrastructure-detail' not in str(caught.value)

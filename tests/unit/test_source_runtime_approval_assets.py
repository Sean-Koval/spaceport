"""Pure fixed synthetic runtime assets, not live-provider qualification."""
import pytest
from gateway.contracts.canonical import canonical_bytes
from gateway.source_access_contracts import SourceApprovalDocument
from tests.integration.source_runtime_fixture import CASES, source_profile
from tests.integration.source_runtime_approval_seed import (CLIENT, fixture_source_profile,
    runtime_actor, runtime_author, runtime_approval_assets)

pytestmark = pytest.mark.unit


def test_fixed_runtime_identities_have_exact_authenticated_clients():
    assert len({runtime_actor(name).id for name in CASES}) == 5
    assert all(runtime_actor(name).kind == 'service' and runtime_actor(name).service_client_id == CLIENT for name in CASES)
    assert runtime_author().service_client_id == CLIENT
    assert runtime_author().id not in {runtime_actor(name).id for name in CASES}
    with pytest.raises(ValueError): runtime_actor('organization-user')


def test_fixed_runtime_assets_bind_complete_source_and_schema_pins():
    source = source_profile()
    document, manifest, bindings, sources = runtime_approval_assets(source, fixture_source_profile(source))
    approval = SourceApprovalDocument.model_validate(document)
    approval.package_pins(manifest)
    assert {item.principal_id for item in approval.audiences} == {runtime_actor(name).id for name in CASES}
    assert approval.bindings.consumer.content_digest == bindings[0].digest
    assert approval.bindings.fixture.content_digest == bindings[1].digest
    assert all(item.classification == 'non-personal' for item in approval.lineage)
    assert {item.context for item in sources} == {'consumer', 'fixture_test'}
    assert sources[0].data_project != sources[1].data_project
    assert sources[0].dataset != sources[1].dataset
    assert sources[0].cube_endpoint != sources[1].cube_endpoint
    assert bindings[0].model_bundle_digest != bindings[1].model_bundle_digest
    from gateway.publishing.test_runner import BigQueryFixtureCatalog, ApprovedFixture, execution_contract_digest
    cases = [ApprovedFixture(fixture=case, principal_id='fixed-synthetic-case',
        kind='input_negative' if case.expected_error else 'known_answer') for case in manifest.fixtures]
    from gateway.contracts.manifest import Fixture
    cases.append(ApprovedFixture(fixture=Fixture(id='fixed-denial',version='1',inputs=dict(manifest.fixtures[0].inputs),expected_error='NOT_FOUND_OR_FORBIDDEN'),principal_id='fixed-denied-case',kind='permission_negative'))
    BigQueryFixtureCatalog(product_id=manifest.product_id, consumer_binding=bindings[0], fixture_binding=bindings[1],
        consumer_source=sources[0], fixture_source=sources[1], execution_contract_digest=execution_contract_digest(manifest), cases=cases)


@pytest.mark.parametrize('field,value', [('data_project', 'other-project'), ('dataset', 'other_dataset'),
    ('model_bundle_digest', '8'*64), ('auth', {'mode':'gke-workload-identity','expected_principal':'other-executor@fixture-project.iam.gserviceaccount.com'})])
def test_changed_runtime_source_cannot_receive_test_owner_review(field, value):
    source = source_profile()
    changed = type(source).model_validate(source.model_dump(mode='json') | {field:value})
    with pytest.raises(ValueError): fixture_source_profile(changed)
    with pytest.raises(ValueError): runtime_approval_assets(changed, fixture_source_profile(source))
    fixture = fixture_source_profile(source)
    changed_fixture = type(fixture).model_validate(fixture.model_dump(mode='json') | {field:value})
    with pytest.raises(ValueError): runtime_approval_assets(source, changed_fixture)

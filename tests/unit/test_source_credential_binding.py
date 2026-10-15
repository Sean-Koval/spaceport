"""Configured source identity is pinned; secret bytes and entitlement are not inferred."""
import copy
import hashlib
import pytest
from gateway.contracts.canonical import canonical_bytes
from gateway.adapters.domo_runtime import DomoSource
from gateway.adapters.bigquery_runtime import BigQuerySource
from gateway import source_access_contracts as access
from tests.unit.test_domo_source_config import source_dict
from tests.unit.test_bigquery_serving_source import source_value
from tests.unit.test_source_approval_contracts import package

pytestmark=pytest.mark.unit

def versioned_source(**auth_changes):
    value=source_dict()
    value['auth'].update(client_id_version='projects/synthetic-project/secrets/domo_id/versions/1',
        client_secret_version='projects/123456/secrets/domo_secret/versions/2')
    value['auth'].update(auth_changes)
    return DomoSource.model_validate(value)

def test_versioned_domo_binding_is_domain_separated_and_rotation_changes_identity():
    source=versioned_source()
    expected=dict(provider='domo',mode='oauth-client-credentials',client_id_sha256=source.auth.client_id_sha256,
        expected_customer='acme',expected_user_id=123,expected_role=None,
        client_id_version=source.auth.client_id_version,client_secret_version=source.auth.client_secret_version)
    digest=access.source_credential_binding_digest(source)
    assert digest==hashlib.sha256(b'spaceport-source-credential-binding-v1\0'+canonical_bytes(expected)).hexdigest()
    raw=source.model_dump(mode='python');raw['auth']['client_secret_version']='projects/123456/secrets/domo_secret/versions/3'
    rotated=DomoSource.model_validate(raw)
    assert access.source_credential_binding_digest(rotated)!=digest
    assert rotated.digest!=source.digest
    assert access.physical_source_identity_digest(rotated)==access.physical_source_identity_digest(source)

@pytest.mark.parametrize('suffix',['latest','0','01','+1','1?version=2','1 ','1/2'])
def test_noncanonical_secret_versions_are_never_identity(suffix):
    with pytest.raises(ValueError):
        versioned_source(client_secret_version='projects/123456/secrets/domo_secret/versions/'+suffix)

def test_historical_unversioned_descriptor_stays_byte_identical_but_cannot_approve():
    source=DomoSource.model_validate(source_dict())
    assert 'client_id_version' not in source.model_dump(mode='json')['auth']
    assert 'client_secret_version' not in source.model_dump(mode='json')['auth']
    assert source.digest=='7130b503240af8c128fc901b9f0add975a440659df00f980956be64052852b00'
    with pytest.raises(ValueError,match='Invalid source credential binding'):
        access.source_credential_binding_digest(source)

def test_partial_version_mapping_and_mutated_model_cannot_approve():
    source=versioned_source()
    raw=source.model_dump(mode='python');raw['auth'].pop('client_id_version')
    with pytest.raises(ValueError):DomoSource.model_validate(raw)
    with pytest.raises(ValueError):access.source_credential_binding_digest(source.model_copy(update={'unexpected':'synthetic'}))

def test_bigquery_uses_workload_identity_without_a_fictitious_secret():
    source=BigQuerySource.model_validate(source_value())
    expected=dict(provider='bigquery',mode=source.auth.mode,expected_principal=source.auth.expected_principal)
    assert access.source_credential_binding_digest(source)==hashlib.sha256(
        b'spaceport-source-credential-binding-v1\0'+canonical_bytes(expected)).hexdigest()
    changed=source.model_copy(update={'auth':source.auth.model_copy(update={'expected_principal':'worker-other@synthetic-project.iam.gserviceaccount.com'})})
    assert access.source_credential_binding_digest(changed)!=access.source_credential_binding_digest(source)

def test_approval_requires_each_explicit_credential_binding_pin(package):
    raw=copy.deepcopy(package[0])
    raw['sources']['consumer'].pop('credential_binding_digest',None)
    with pytest.raises(ValueError):access.validate_source_approval(canonical_bytes(raw))

"""Source configuration is immutable, offline and never implicit authority."""
import hashlib
import importlib
from copy import deepcopy
import pytest
from pydantic import ValidationError

pytestmark = pytest.mark.unit
DATASET = '00000000-0000-0000-0000-000000000001'


def source_dict():
    return dict(provider='domo', source_id='domo-consumer', revision=1, context='consumer',
        auth=dict(mode='oauth-client-credentials',client_id_ref='/run/secrets/domo_id',
            client_secret_ref='/run/secrets/domo_secret', client_id_sha256=hashlib.sha256(b'client-id').hexdigest(),
            expected_customer='acme',expected_user_id=123),
        dataset_ids=[DATASET],source_policy_digest='a'*64,max_dispatches=10,max_active_sends=2)


def api():
    spec = importlib.util.find_spec('gateway.adapters.domo_runtime')
    assert spec is not None, 'Domo v2 source contract is missing'
    return importlib.import_module(spec.name)


def test_source_identity_offline_and_stable_capacity():
    source = api().DomoSource.model_validate(source_dict())
    changed = source_dict(); changed.update(source_id='alias',revision=2)
    changed['auth']['expected_user_id'] = 456
    alias = api().DomoSource.model_validate(changed)
    assert source.capacity_pool_id == alias.capacity_pool_id
    assert source.digest != alias.digest
    assert source.api_origin == 'https://api.domo.com'
    with pytest.raises(ValidationError): source.revision = 2


@pytest.mark.parametrize('change', ['url','unknown','dataset','duplicate','secret_path','secret_url',
    'bool_revision','customer','scope','budget','capacity_alias'])
def test_source_rejects_unreviewed_configuration(change):
    value = source_dict()
    if change == 'url': value['api_origin'] = 'https://evil.example'
    elif change == 'unknown': value['verified'] = True
    elif change == 'dataset': value['dataset_ids'] = ['not-a-uuid']
    elif change == 'duplicate': value['dataset_ids'] *= 2
    elif change == 'secret_path': value['auth']['client_secret_ref'] = '/run/secrets/../secret'
    elif change == 'secret_url': value['auth']['client_secret_ref'] = 'https://secret.example/key'
    elif change == 'bool_revision': value['revision'] = True
    elif change == 'customer': value['auth']['expected_customer'] = 'acme/other'
    elif change == 'scope': value['auth']['scope'] = 'data user'
    elif change == 'budget': value['max_dispatches'] = 0
    elif change == 'capacity_alias': value['capacity_pool_id'] = 'fresh-pool'
    with pytest.raises(ValidationError): api().DomoSource.model_validate(value)


def test_fixture_isolation_uses_dataset_and_credential_owner_not_alias(valid_manifest):
    from tests.contract.test_domo_runtime import setup
    a,consumer,*_=setup(valid_manifest)
    assert hasattr(a,'validate_domo_source_isolation')
    fixture=consumer.model_copy(update={'source_id':'fixture','context':'fixture_test',
        'dataset_ids':('00000000-0000-0000-0000-000000000002',),
        'auth':consumer.auth.model_copy(update={'expected_user_id':124,'client_id_sha256':'c'*64})})
    a.validate_domo_source_isolation(consumer,fixture)
    for update in ({'dataset_ids':consumer.dataset_ids}, {'context':'consumer'},
                   {'source_id':consumer.source_id}, {'auth':consumer.auth},
                   {'auth':fixture.auth.model_copy(update={'expected_user_id':123})},
                   {'auth':fixture.auth.model_copy(update={'client_id_sha256':consumer.auth.client_id_sha256})}):
        with pytest.raises(Exception):
            a.validate_domo_source_isolation(consumer,fixture.model_copy(update=update))

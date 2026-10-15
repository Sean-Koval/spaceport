"""Closed stdin descriptor for the exact-image, synthetic-only journey."""
import copy
import json
import pytest

pytestmark=pytest.mark.unit
TOKEN='a'*16


def descriptor():
    keys=('owner','app','audit_retention','source_reader')
    return dict(schema_version=1,source_digest='b'*64,image_id='sha256:'+'c'*64,
        registry=dict(host='127.0.0.1',port=34567,database='gateway_test_'+TOKEN,
            roles={key:'gwt_'+TOKEN+'_'+key for key in keys},passwords={key:'d'*43 for key in keys},admin_password='e'*43))


def parse(value):
    from tests.integration.source_pilot_bigquery_image import parse_descriptor
    return parse_descriptor(json.dumps(value).encode(),'b'*64)


def test_exact_image_descriptor_accepts_only_generated_same_database_roles():
    assert parse(descriptor())==descriptor()


@pytest.mark.parametrize('mutate',[
    lambda v:v.update(schema_version=True),lambda v:v.update(extra=True),
    lambda v:v.update(source_digest='a'*64),lambda v:v.update(image_id='latest'),
    lambda v:v['registry'].update(host='registry-db'),lambda v:v['registry'].update(port=True),
    lambda v:v['registry'].update(database='gateway_registry'),
    lambda v:v['registry']['roles'].update(owner='gwt_'+('b'*16)+'_owner'),
    lambda v:v['registry']['roles'].update(app='gateway_app'),
    lambda v:v['registry']['passwords'].update(app='short'),
    lambda v:v['registry'].update(admin_password='!not-a-test-password'),
    lambda v:v['registry'].update(admin_username='arbitrary'),
])
def test_invalid_image_descriptor_rejects_before_database_access(mutate):
    value=descriptor();mutate(value)
    with pytest.raises(ValueError):parse(value)


def test_duplicate_and_oversize_stdin_are_rejected_without_raw_values():
    from tests.integration.source_pilot_bigquery_image import parse_descriptor
    for raw in (b'{"schema_version":1,"schema_version":1}',b'X'*16385,b'SYNTHETIC_SECRET_CANARY'):
        with pytest.raises(ValueError) as caught:parse_descriptor(raw,'b'*64)
        assert 'SYNTHETIC_SECRET_CANARY' not in str(caught.value)


@pytest.mark.parametrize('metadata',[0,9,True])
def test_completion_counts_require_metadata_proof_for_all_ten_jobs(metadata):
    from tests.integration.source_pilot_bigquery_image import validate_counts
    with pytest.raises(ValueError):validate_counts(dict(inserts=10,metadata=metadata,results=10,provider_errors=0))


def test_coherent_completion_counts_accept_ten_exact_job_proofs():
    from tests.integration.source_pilot_bigquery_image import validate_counts
    counts=dict(inserts=10,metadata=10,results=10,provider_errors=0)
    assert validate_counts(counts)==counts


@pytest.mark.parametrize('relative',['node_modules','tests/node_modules','tests/runtime/node_modules',
    'cube/node_modules','cube/bigquery-runtime/node_modules'])
@pytest.mark.parametrize('kind',['directory','file','symlink','dangling_symlink'])
def test_image_rejects_every_source_ancestor_package_shadow(tmp_path,relative,kind):
    from tests.integration.source_pilot_bigquery_image import reject_source_package_shadowing
    path=tmp_path/relative;path.parent.mkdir(parents=True,exist_ok=True)
    if kind=='directory':path.mkdir()
    elif kind=='file':path.write_text('synthetic')
    else:
        target=tmp_path/'target'
        if kind=='symlink':target.mkdir()
        path.symlink_to(target,target_is_directory=True)
    with pytest.raises(ValueError,match='Immutable image package resolution required'):
        reject_source_package_shadowing(tmp_path)


def test_image_package_guard_allows_client_dependencies_outside_resolution_ancestors(tmp_path):
    from tests.integration.source_pilot_bigquery_image import reject_source_package_shadowing
    (tmp_path/'clients/node_modules').mkdir(parents=True)
    reject_source_package_shadowing(tmp_path)

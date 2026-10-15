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


def test_image_smoke_imports_without_stdin_or_database(monkeypatch, capsys):
    import sys
    from tests.integration import source_pilot_bigquery_image as image
    monkeypatch.setattr(image, '__file__', '/app/tests/integration/source_pilot_bigquery_image.py')
    monkeypatch.setattr(image, 'reject_source_package_shadowing', lambda root: None)
    monkeypatch.setattr(sys, 'argv', ['source_pilot_bigquery_image', '--smoke'])
    monkeypatch.delenv('GATEWAY_SOURCE_TEST_AUTHORIZATION', raising=False)
    assert image.main() == 0
    assert capsys.readouterr() == ('GATEWAY_BQ_GATEWAY_JOURNEY_SMOKE {"schema_version":1,"status":"passed"}\n', '')


def test_image_import_failure_is_allowlisted_without_exception_text(monkeypatch, capsys):
    import builtins
    import sys
    from tests.integration import source_pilot_bigquery_image as image
    monkeypatch.setattr(image, '__file__', '/app/tests/integration/source_pilot_bigquery_image.py')
    monkeypatch.setattr(image, 'reject_source_package_shadowing', lambda root: None)
    monkeypatch.setattr(sys, 'argv', ['source_pilot_bigquery_image', '--smoke'])
    original = builtins.__import__
    def deny(name, *args, **kwargs):
        if name == 'psycopg': raise ImportError('PRIVATE-IMPORT-CANARY')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', deny)
    assert image.main() == 1
    assert json.loads(capsys.readouterr().out.removeprefix(image.MARKER)) == {
        'schema_version': 1, 'kind': 'bigquery_gateway_journey', 'status': 'failed',
        'stage': 'bootstrap', 'error': 'import'}


@pytest.mark.parametrize(('reported', 'expected_stage', 'expected_error'), [
    ('worker', 'worker', 'assertion'), ('PRIVATE-STAGE-CANARY', 'package_bootstrap', 'value')])
def test_image_scenario_passes_fixed_stage_callback(monkeypatch, capsys, reported, expected_stage, expected_error):
    import sys
    from types import SimpleNamespace
    from tests.integration import source_pilot_bigquery_image as image
    from tests.integration import test_bigquery_gateway as scenario
    from scripts import demo_runtime
    monkeypatch.setattr(image, '__file__', '/app/tests/integration/source_pilot_bigquery_image.py')
    monkeypatch.setattr(image, 'reject_source_package_shadowing', lambda root: None)
    monkeypatch.setattr(sys, 'argv', ['source_pilot_bigquery_image'])
    monkeypatch.setenv('GATEWAY_SOURCE_TEST_AUTHORIZATION', 'private-synthetic-source-only')
    monkeypatch.setattr(demo_runtime, 'source_inventory', lambda: {})
    monkeypatch.setattr(demo_runtime, 'source_digest', lambda inventory: 'b'*64)
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=__import__('io').BytesIO(json.dumps(descriptor()).encode())))
    monkeypatch.setattr(image, 'database_from_descriptor', lambda value: object())
    async def fail(database, root, *, stage):
        stage(reported)
        raise AssertionError('PRIVATE-SCENARIO-CANARY')
    monkeypatch.setattr(scenario, 'gateway_journey', fail)
    assert image.main() == 1
    assert json.loads(capsys.readouterr().out.removeprefix(image.MARKER)) == {
        'schema_version': 1, 'kind': 'bigquery_gateway_journey', 'status': 'failed',
        'stage': expected_stage, 'error': expected_error}


@pytest.mark.parametrize(('arguments', 'authorization'), [
    ([], None), ([], 'arbitrary'), (['--smoke', 'extra'], None), (['--arbitrary'], None)])
def test_smoke_mode_does_not_expand_the_normal_journey_authority(monkeypatch, capsys, arguments, authorization):
    import sys
    from tests.integration import source_pilot_bigquery_image as image
    monkeypatch.setattr(image, '__file__', '/app/tests/integration/source_pilot_bigquery_image.py')
    monkeypatch.setattr(sys, 'argv', ['source_pilot_bigquery_image', *arguments])
    if authorization is None: monkeypatch.delenv('GATEWAY_SOURCE_TEST_AUTHORIZATION', raising=False)
    else: monkeypatch.setenv('GATEWAY_SOURCE_TEST_AUTHORIZATION', authorization)
    assert image.main() == 1
    assert json.loads(capsys.readouterr().out.removeprefix(image.MARKER)) == {
        'schema_version': 1, 'kind': 'bigquery_gateway_journey', 'status': 'failed',
        'stage': 'bootstrap', 'error': 'value'}

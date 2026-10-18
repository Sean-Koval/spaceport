"""Operational contracts; real-stack cases must never silently skip."""
import asyncio
import importlib
import json
import logging
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        pytest.fail(f"Operational entrypoint is missing: {error.name}")


@pytest.mark.unit
def test_runtime_configuration_rejects_non_demo_and_non_private_secret_paths():
    api = module('gateway.runtime_config')
    with pytest.raises(ValueError): api.RuntimeConfig(identity_mode='production')
    with pytest.raises(ValueError): api.RuntimeConfig(secret_directory='/tmp/secrets')
    config = api.RuntimeConfig()
    assert config.probe_principal_id == 'demo-north'
    assert config.probe_product_id == 'revenue'
    assert config.vendor_connections == ()


@pytest.mark.unit
def test_reviewed_demo_configuration_validates_without_database_or_secrets():
    config = module('gateway.runtime_config').load_assets(ROOT)
    assert config.manifest.product_id == 'revenue'
    assert config.fixture_catalog.consumer_binding.digest == config.bindings[0].digest
    assert config.fixture_catalog.fixture_binding.context == 'fixture_test'
    config.fixture_catalog.approve(config.manifest, config.bindings[0])
    assert {a['id'] for a in config.accounts} == {'demo-north','demo-south','demo-author','demo-publisher','fixture-north','fixture-south','fixture-denied'}
    assert not next(a for a in config.accounts if a['id'] == 'fixture-denied')['roles']
    assert next(a for a in config.accounts if a['id'] == 'demo-author')['roles'] == ['author']


@pytest.mark.unit
def test_mounted_secret_read_rejects_symlink_and_bad_material(tmp_path):
    read = module('gateway.runtime_config').read_secret
    valid = tmp_path / 'valid'; valid.write_text('a' * 43 + '\n'); valid.chmod(0o444)
    assert read(valid) == 'a' * 43
    link = tmp_path / 'link'; link.symlink_to(valid)
    with pytest.raises(ValueError): read(link)
    valid.chmod(0o600); valid.write_text('password\n')
    with pytest.raises(ValueError): read(valid)


@pytest.mark.unit
def test_structured_logs_discard_raw_messages_args_exception_and_unapproved_fields():
    formatter = module('gateway.logging').SafeFormatter()
    record = logging.LogRecord('httpx',logging.ERROR,__file__,1,'CANARY %s',('RAW_ROW',),None)
    record.token = 'CANARY'; record.rows = [{'secret':'RAW_ROW'}]
    result = formatter.format(record)
    assert 'CANARY' not in result and 'RAW_ROW' not in result and 'secret' not in result
    value = json.loads(result)
    assert value['level'] == 'ERROR' and value['event'] == 'runtime_event'


@pytest.mark.unit
def test_verifier_missing_mandatory_suite_cannot_pass(tmp_path):
    api = module('scripts.verify_demo')
    report = api.summarize([],source='a'*40,versions={},evidence_root=tmp_path)
    assert report['status'] == 'BLOCKED'
    assert report['missing']
    assert {'publisher_browser','mcp_clients','offline_egress','backup_restore'} <= set(report['missing'])


@pytest.mark.unit
def test_verifier_mocked_or_skipped_does_not_satisfy_actual_slot(tmp_path):
    api = module('scripts.verify_demo')
    records = [{'suite':name,'classification':'mocked','status':'PASS','evidence_paths':[]} for name in api.MANDATORY]
    report = api.summarize(records,source='a'*40,versions={},evidence_root=tmp_path)
    assert report['status'] != 'PASS'
    assert set(report['missing']) == set(api.MANDATORY)


@pytest.mark.unit
def test_packaging_overlay_is_separate_and_has_no_external_runtime_accounts():
    path = ROOT / 'infra/compose.demo.yaml'
    assert path.is_file(), 'Full application composition is missing'
    config = json.loads(path.read_text())
    assert {'gateway','worker','retention','migrate','bootstrap','edge'} <= set(config['services'])
    for name, service in config['services'].items():
        assert service['networks'] == ['private']
        assert service.get('dns') == ['127.0.0.1']
        assert service.get('pull_policy') == 'never'
        assert not service.get('privileged')
    for name in ('gateway','worker'):
        assert not {'registry_owner','registry_admin'} & set(config['services'][name]['secrets'])


@pytest.mark.e2e
def test_actual_operations_requires_verified_disposable_stack():
    api = module('scripts.verify_demo')
    state = api.require_actual_stack()
    assert state['kind'] == 'private-synthetic-gateway-demo'


@pytest.mark.unit
def test_runtime_assembly_wires_shared_services_without_opening_database(monkeypatch):
    api = module('gateway.runtime')
    monkeypatch.setattr(api,'database_url',lambda role:'postgresql+psycopg://gwt_0123456789abcdef_app:synthetic@127.0.0.1:1/gateway_test_0123456789abcdef')
    monkeypatch.setattr(api,'secret_key',lambda name:b'x'*32)
    async def scenario():
        runtime = api.build_runtime()
        try:
            services = runtime.services
            assert services.execution.registry is services.registry
            assert services.publishing.policy is services.policy
            assert services.results.policy is services.policy
            assert services.readiness.adapter is runtime.adapter
            assert services.publishing.fixture_runner.catalog().fixture_binding.context == 'fixture_test'
        finally:
            await runtime.close()
    asyncio.run(scenario())


@pytest.mark.postgres
def test_operator_bootstrap_is_idempotent_and_never_publishes(registry_database):
    api = module('gateway.bootstrap')
    registry_type = module('gateway.registry.repository').Registry
    async def scenario():
        owner = registry_type(registry_database.urls['owner'].render_as_string(hide_password=False))
        try:
            await api.provision(owner)
            await api.provision(owner)
        finally: await owner.close()
    asyncio.run(scenario())
    assert registry_database.scalar('SELECT count(*) FROM gateway.principals') == 7
    assert registry_database.scalar('SELECT count(*) FROM gateway.principal_grants') == 6
    assert registry_database.scalar('SELECT count(*) FROM gateway.binding_revisions') == 2
    assert registry_database.scalar('SELECT count(*) FROM gateway.product_versions') == 0
    assert registry_database.scalar('SELECT count(*) FROM gateway.test_runs') == 0
    registry_database.execute_owner("UPDATE gateway.principal_grants SET active=false WHERE principal_id='demo-north'")
    asyncio.run(scenario())
    assert registry_database.scalar("SELECT active FROM gateway.principal_grants WHERE principal_id='demo-north'") is False


@pytest.mark.unit
def test_backup_archive_validation_rejects_traversal_and_secret_values(tmp_path):
    api = module('scripts.demo_backup')
    import io,tarfile
    path = tmp_path / 'hostile.tar'
    with tarfile.open(path,'w') as archive:
        info = tarfile.TarInfo('../escape'); info.size = 1
        archive.addfile(info,io.BytesIO(b'x'))
    with pytest.raises(ValueError): api.inspect_backup(path)


@pytest.mark.unit
def test_state_loader_rejects_relative_symlink_and_unknown_project(tmp_path):
    api = module('scripts.demo_runtime')
    with pytest.raises(ValueError): api.load_state(Path('relative'))
    path = tmp_path / 'state'; path.mkdir(mode=0o700)
    (path/'state.json').write_text(json.dumps({'kind':'private-synthetic-gateway-demo','project':'production'}))
    with pytest.raises(ValueError): api.load_state(path)


@pytest.mark.unit
def test_cleanup_tolerates_removal_error_only_after_verified_absence():
    api = module('scripts.demo_runtime')
    commands=[]
    def command(args,**kwargs):
        commands.append(args)
        if args[:3] == ['docker','container','rm']: return subprocess.CompletedProcess(args,1,'','already absent')
        if args[:3] == ['docker','container','ls']: return subprocess.CompletedProcess(args,0,'','')
        raise AssertionError(args)
    api.remove_exact('container','a'*64,command=command)
    assert commands[-1][:3] == ['docker','container','ls']
    def denied(args,**kwargs): return subprocess.CompletedProcess(args,1,'','daemon denied')
    with pytest.raises(RuntimeError): api.remove_exact('container','a'*64,command=denied)


@pytest.fixture
def actual_demo():
    api=module('scripts.verify_demo')
    try: state=api.require_actual_stack()
    except (ValueError,RuntimeError,OSError) as error: pytest.fail(str(error),pytrace=False)
    import httpx
    private=Path(state['state_directory'])/'identities/demo-tokens.json'
    tokens=json.loads(private.read_text())
    client=httpx.Client(base_url='http://127.0.0.1:'+str(state['port']),trust_env=False,timeout=8)
    try: yield state,client,tokens
    finally: client.close()


def demo_request(client,tokens,account,method,path,**kwargs):
    return client.request(method,path,headers={'Authorization':'Bearer '+tokens[account]},**kwargs)


def query_actual(client,tokens,account):
    import time
    response=demo_request(client,tokens,account,'POST','/v1/executions',json={
        'product_id':'revenue','version':'1.0.0','inputs':{'start':'2024-03-09','end':'2024-03-12'},
        'sort':[{'field':'day','direction':'asc'},{'field':'segment','direction':'asc'}]})
    assert response.status_code==200,'Actual execution admission failed'
    identifier=response.json()['execution_id'];deadline=time.monotonic()+125
    while time.monotonic()<deadline:
        response=demo_request(client,tokens,account,'GET','/v1/executions/'+identifier)
        assert response.status_code==200,'Actual result access failed'
        result=response.json()
        if result['state']=='succeeded':return result
        assert result['state'] in {'accepted','running'},'Actual execution did not succeed'
        time.sleep(.25)
    pytest.fail('Actual worker did not complete within its deadline')


def record_actual(name,value):
    import os
    root=Path(os.environ['GATEWAY_OPERATIONS_EVIDENCE_DIR'])
    module('scripts.demo_runtime').private_json(root/(name+'.json'),value)


def wait_ready(client):
    import time
    deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        try:
            if client.get('/readyz').status_code==200:return
        except Exception:pass
        time.sleep(1)
    pytest.fail('Actual readiness failed to recover')


@pytest.mark.e2e
def test_actual_runtime_dependencies(actual_demo):
    state,client,tokens=actual_demo
    records=module('scripts.demo_runtime').inspect_stack(state)
    assert len(records)==8
    result=query_actual(client,tokens,'demo-north')
    assert result['source_watermark'] and result['provenance']['adapter_revision']=='cube-rest-v1'
    assert result['product_digest']==module('gateway.runtime_config').load_assets().manifest.content_digest
    record_actual('runtime_dependencies',{'containers':records,'digest':result['product_digest'],'source_watermark':result['source_watermark']})


@pytest.mark.e2e
def test_actual_readiness(actual_demo):
    state,client,tokens=actual_demo
    assert client.get('/healthz').status_code==200
    assert client.get('/readyz').status_code==200
    record_actual('readiness',{'liveness':200,'published_readiness':200})


@pytest.mark.e2e
def test_actual_consumer_rest(actual_demo):
    from decimal import Decimal,localcontext
    _,client,tokens=actual_demo
    north=query_actual(client,tokens,'demo-north');south=query_actual(client,tokens,'demo-south')
    with localcontext() as precision:
        precision.prec=50
        assert sum(Decimal(row['revenue']) for row in north['rows'])==Decimal('9007199254741023.31')
        assert sum(Decimal(row['revenue']) for row in south['rows'])==Decimal('100.01')
    assert north['row_count']==3 and south['row_count']==2
    assert north['product_digest']==south['product_digest']
    assert demo_request(client,tokens,'demo-south','GET','/v1/executions/'+north['execution_id']).status_code==404
    record_actual('consumer_rest',{'digest':north['product_digest'],'north_rows':3,'south_rows':2,'cross_principal_access':404})


@pytest.mark.e2e
def test_actual_restart_persistence(actual_demo):
    state,client,tokens=actual_demo
    original_head=demo_request(client,tokens,'demo-north','GET','/v1/products/revenue').json()['digest']
    runtime=module('scripts.demo_runtime')
    before=query_actual(client,tokens,'demo-north')
    runtime.compose(state,'restart','registry-db','demo-db','cube','cube-test','gateway','worker','retention','edge',timeout=180)
    wait_ready(client)
    after=query_actual(client,tokens,'demo-north')
    assert before['product_digest']==after['product_digest']
    assert before['source_watermark']==after['source_watermark']
    assert before['rows']==after['rows']
    assert original_head==demo_request(client,tokens,'demo-north','GET','/v1/products/revenue').json()['digest']
    record_actual('restart_persistence',{'current_head':original_head,'digest':after['product_digest'],'watermark':after['source_watermark'],'preserved':True})


@pytest.mark.e2e
def test_actual_backup_restore(actual_demo):
    import httpx
    state,client,tokens=actual_demo
    runtime=module('scripts.demo_runtime');backup=module('scripts.demo_backup')
    before=query_actual(client,tokens,'demo-north')
    original_head=demo_request(client,tokens,'demo-north','GET','/v1/products/revenue').json()['digest']
    root=Path(state['state_directory']);archive=root/'evidence'/'recovery.tar.gz'
    runtime.inspect_stack(state)
    with backup.quiesced_application(state):backup.create_backup(state,archive,quiesced=True)
    wait_ready(client)
    target=root/'restored'
    restored=None
    try:
        restored=backup.restore(archive,target,18080)
        restored_tokens=json.loads((target/'identities/demo-tokens.json').read_text())
        with httpx.Client(base_url='http://127.0.0.1:18080',trust_env=False,timeout=8) as other:
            wait_ready(other);after=query_actual(other,restored_tokens,'demo-north')
            assert original_head==demo_request(other,restored_tokens,'demo-north','GET','/v1/products/revenue').json()['digest']
        assert before['product_digest']==after['product_digest']
        assert before['source_watermark']==after['source_watermark']
        assert before['rows']==after['rows']
        record_actual('backup_restore',{'source_project':state['project'],'restored_project':restored['project'],
            'digest':after['product_digest'],'watermark':after['source_watermark'],'preserved':True})
    finally:
        if restored is not None:runtime.cleanup(restored)


def registry_sql(state,statement):
    # Fixed harness-owned SQL, against the exact admitted synthetic registry.
    return module('scripts.demo_runtime').compose(state,'exec','-T','--user','999:999','registry-db',
        'psql','--no-psqlrc','-U','synthetic_admin','-d','gateway_registry','-At','-v','ON_ERROR_STOP=1',
        input=statement,timeout=20).stdout.strip()


@pytest.mark.e2e
def test_actual_readiness_faults(actual_demo):
    state,client,_=actual_demo
    runtime=module('scripts.demo_runtime')
    original=registry_sql(state,'SELECT version_num FROM public.alembic_version;')
    assert original and '\n' not in original and original.replace('_','').isalnum()
    try:
        registry_sql(state,"UPDATE public.alembic_version SET version_num='incorrect_head';")
        assert client.get('/readyz').status_code==503
    finally:registry_sql(state,"UPDATE public.alembic_version SET version_num='"+original+"';")
    wait_ready(client)
    for service in ('registry-db','cube'):
        runtime.compose(state,'stop',service,timeout=60)
        try:
            assert client.get('/healthz').status_code==200
            assert client.get('/readyz').status_code==503
        finally:runtime.compose(state,'start',service,timeout=60)
        wait_ready(client)
    record_actual('readiness_faults',{'wrong_head':503,'registry_outage':503,'cube_outage':503,'recovered':200})


@pytest.mark.e2e
def test_actual_redacted_logs(actual_demo):
    state,client,tokens=actual_demo
    runtime=module('scripts.demo_runtime')
    canary='UNTRUSTED_LOG_CANARY_'+__import__('secrets').token_hex(12)
    response=client.get('/v1/products',headers={'Authorization':'Bearer '+canary,'X-Request-ID':canary})
    assert response.status_code==401
    result=query_actual(client,tokens,'demo-north')
    capture=runtime.compose(state,'logs','--no-color','--tail','10000',timeout=30).stdout
    assert canary not in capture
    assert all(value not in capture for value in tokens.values())
    assert '9007199254740993.01' not in capture
    assert all((Path(state['state_directory'])/'secrets'/name).read_text().strip() not in capture
        for name in ('registry_owner','registry_app','demo_reader','cube_signing_key','cube_test_signing_key'))
    record_actual('redacted_logs',{'canary_absent':True,'known_tokens_absent':True,'large_result_decimal_absent':True})


@pytest.mark.e2e
def test_actual_retention(actual_demo):
    state,client,tokens=actual_demo
    result=query_actual(client,tokens,'demo-north')
    identifier=result['execution_id']
    assert __import__('re').fullmatch(r'[a-f0-9-]{36}',identifier)
    # Explicit owner-only synthetic expiry fixture. Preserve the actual result
    # bytes in a temporary table, then reinsert as an already-expired row. This
    # does not exercise the passage of 24 hours or grant app UPDATE privileges.
    registry_sql(state,f"""BEGIN;
CREATE TEMP TABLE expiry_fixture AS SELECT * FROM gateway.results WHERE execution_id='{identifier}';
DELETE FROM gateway.results WHERE execution_id='{identifier}';
INSERT INTO gateway.results (id,execution_id,principal_id,version_id,policy_revision,canonical_hash,envelope,created_at,expires_at)
SELECT id,execution_id,principal_id,version_id,policy_revision,canonical_hash,envelope,clock_timestamp()-interval '25 hours',clock_timestamp()-interval '1 hour' FROM expiry_fixture;
COMMIT;""")
    runtime=module('scripts.demo_runtime')
    code="import asyncio; from gateway.registry.repository import Registry; from gateway.runtime_config import database_url; from gateway.execution.retention import RetentionSweep; from gateway.contracts.clock import SystemClock;\nasync def main():\n a=Registry(database_url('app')); b=Registry(database_url('audit_retention'))\n try: await RetentionSweep(a,b).run_once(SystemClock().now())\n finally: await a.close(); await b.close()\nasyncio.run(main())"
    runtime.compose(state,'exec','-T','retention','python','-c',code,timeout=30)
    response=demo_request(client,tokens,'demo-north','GET','/v1/executions/'+identifier)
    assert response.status_code==200 and response.json()['state']=='expired'
    assert 'rows' not in response.json()
    assert registry_sql(state,f"SELECT count(*) FROM gateway.results WHERE execution_id='{identifier}';")=='0'
    record_actual('retention',{'expired_result_removed':True,'row_release':False,'fixture':'owner-backdated actual result; no wall-clock claim'})


@pytest.mark.unit
def test_egress_timeout_or_udp_send_is_not_denial_evidence():
    api=module('scripts.check_egress')
    assert api.denied_socket({'outcome':'error','errno':101})
    assert not api.denied_socket({'outcome':'timeout'})
    assert not api.denied_socket({'outcome':'sent'})
    assert not api.denied_socket({'outcome':'error','errno':111})


@pytest.mark.unit
def test_evidence_paths_cannot_escape_private_report_root(tmp_path):
    api=module('scripts.verify_demo')
    outside=tmp_path.parent/'outside-proof.json';outside.write_text('{}')
    records=[{'suite':name,'classification':'actual','status':'PASS','evidence_paths':['../outside-proof.json']} for name in api.MANDATORY]
    report=api.summarize(records,source='a'*40,versions={},evidence_root=tmp_path)
    assert report['status']=='BLOCKED'


@pytest.mark.unit
def test_hash_only_directory_mount_observes_atomic_replacement():
    config=json.loads((ROOT/'infra/compose.demo.yaml').read_text())
    for name in ('gateway','worker'):
        mounts=config['services'][name].get('volumes',[])
        assert any(value.endswith(':/run/auth:ro') for value in mounts), 'Hash-only auth directory must be mounted'
        assert not any('identities' in value for value in mounts), 'Plaintext token directory must not be mounted'
        assert 'credential_hashes' not in config['services'][name]['secrets']


@pytest.mark.e2e
def test_actual_authentication_replacement(actual_demo):
    import os
    state,client,tokens=actual_demo
    runtime=module('scripts.demo_runtime')
    path=Path(state['state_directory'])/'auth/credential-hashes.json'
    original=path.read_bytes();content=json.loads(original)
    for entry in content['credentials']:
        if entry['principal_id']=='demo-north':entry['active']=False
    def replace(data):
        temporary=path.with_name('replacement-'+__import__('secrets').token_hex(8))
        descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(descriptor,'wb') as file:
            file.write(data);file.flush();os.fsync(file.fileno());os.fchmod(file.fileno(),0o444)
        os.replace(temporary,path)
    assert demo_request(client,tokens,'demo-north','GET','/v1/products').status_code==200
    try:
        replace(json.dumps(content).encode())
        assert demo_request(client,tokens,'demo-north','GET','/v1/products').status_code==401
        assert demo_request(client,tokens,'demo-south','GET','/v1/products').status_code==200
        denied=runtime.compose(state,'exec','-T','gateway','python','-c',
            "from pathlib import Path; Path('/run/auth/forbidden-write').write_text('x')",timeout=15,check=False)
        assert denied.returncode!=0
    finally:replace(original)
    assert demo_request(client,tokens,'demo-north','GET','/v1/products').status_code==200
    record_actual('authentication_replacement',{'revoked_next_request':401,'other_identity_unchanged':200,
        'restored_next_request':200,'directory_write_denied':True,'plaintext_tokens_mounted':False})


@pytest.mark.unit
def test_logger_rejects_untrusted_metadata_without_fallback_traceback():
    formatter=module('gateway.logging').SafeFormatter()
    record=logging.LogRecord('source',logging.ERROR,__file__,1,'PRIVATE_VALUE',(),None)
    record.event={'unhashable':'PRIVATE_VALUE'}
    record.levelname='PRIVATE_VALUE'
    value=json.loads(formatter.format(record))
    assert value['event']=='runtime_event' and value['level']=='INFO'


@pytest.mark.unit
def test_backup_failure_never_publishes_partial_archive(monkeypatch,tmp_path):
    api=module('scripts.demo_backup')
    monkeypatch.setattr(api,'verify_images',lambda state:None)
    monkeypatch.setattr(api,'inspect_stack',lambda state,**kwargs:[])
    def failure(*args):raise RuntimeError('synthetic dump failure')
    monkeypatch.setattr(api,'dump',failure)
    target=tmp_path/'backup.tar.gz'
    with pytest.raises(RuntimeError):api.create_backup({},target)
    assert not target.exists() and list(tmp_path.iterdir())==[]


@pytest.mark.unit
def test_retained_evidence_requires_hash_identity_and_complete_actual_suites(tmp_path):
    api=module('scripts.demo_evidence')
    assert api.latest_evidence(tmp_path)['status']=='BLOCKED'


@pytest.mark.unit
def test_dependency_inventory_names_both_locks_and_records_owner_selected_license():
    path=ROOT/'docs/operations/dependency-inventory.json'
    assert path.is_file(),'License inventory is missing'
    value=json.loads(path.read_text())
    # The license is the owner's recorded choice, never inferred from dependencies.
    project=tomllib.loads((ROOT/'pyproject.toml').read_text())['project']
    assert value['original_code_license']==project['license']=='Apache-2.0'
    assert (ROOT/'LICENSE').read_text().lstrip().startswith('Apache License\n')
    assert set(project['license-files'])=={'LICENSE','NOTICE'} and (ROOT/'NOTICE').is_file()
    for name in ('publisher/package.json','clients/package.json'):
        assert json.loads((ROOT/name).read_text())['license']=='Apache-2.0'
    # Selecting a license does not authorize distribution of images or bundles.
    assert value['distribution'].startswith('BLOCKED_PENDING')
    assert {'python','publisher'} <= set(value['locks'])
    assert value['packages'] and all(row['name'] and row['version'] for row in value['packages'])
    assert all((ROOT/name).is_file() for row in value['packages'] for name in row['notice_paths'])


@pytest.mark.unit
def test_preparation_resolves_complete_private_composition_and_hash_only_mount(monkeypatch,tmp_path):
    api=module('scripts.demo_runtime')
    monkeypatch.setattr(api,'require_docker',lambda:None)
    def command(args,**kwargs):
        result='sha256:'+'a'*64 if args[:3]==['docker','image','inspect'] else ''
        return subprocess.CompletedProcess(args,0,result,'')
    monkeypatch.setattr(api,'run',command)
    state=api.prepare(tmp_path/'state')
    loaded=api.load_state(tmp_path/'state')
    assert loaded==state
    config=json.loads((tmp_path/'state/compose.json').read_text())
    assert set(config['services'])==set(api.RUNNING)|{'migrate','bootstrap'}
    assert '${' not in json.dumps(config)
    assert {name for name,row in config['services'].items() if row.get('ports')}=={'edge'}
    assert config['services']['edge']['ports'][0]['host_ip']=='127.0.0.1'
    assert config['services']['gateway']['image']==state['app_image']
    assert (tmp_path/'state/auth/credential-hashes.json').is_file()
    assert all((tmp_path/'state/identities'/(name+'.token')).stat().st_mode&0o777==0o600 for name in ('demo-north','demo-south'))


@pytest.mark.unit
def test_restore_preparation_never_pulls_or_builds(monkeypatch,tmp_path):
    api=module('scripts.demo_runtime');calls=[]
    monkeypatch.setattr(api,'require_docker',lambda:None)
    def command(args,**kwargs):calls.append(args);return subprocess.CompletedProcess(args,0,'','')
    monkeypatch.setattr(api,'run',command)
    state=api.prepare_restored_state(tmp_path/'restored',18080,{'app_image':'sha256:'+'a'*64,'edge_image':'sha256:'+'b'*64})
    assert not any(args[:2] in (['docker','pull'],['docker','build']) for args in calls)
    config=json.loads((tmp_path/'restored/compose.json').read_text())
    assert config['services']['edge']['ports']==[{'target':8080,'published':'18080','host_ip':'127.0.0.1','protocol':'tcp'}]
    assert config['services']['gateway']['environment']['GATEWAY_DEMO_PORT']=='18080'


@pytest.mark.unit
@pytest.mark.parametrize('name,value',[('GATEWAY_IDENTITY_MODE','production'),('GATEWAY_SOURCE','bigquery'),
    ('GATEWAY_PUBLIC_INGRESS','true'),('GATEWAY_BIND_HOST','0.0.0.0'),('GATEWAY_OIDC_ISSUER','https://issuer.example.test')])
def test_runtime_entrypoint_rejects_unsafe_identity_environment(monkeypatch,name,value):
    api=module('gateway.runtime_config')
    monkeypatch.setenv(name,value)
    with pytest.raises(ValueError):api.RuntimeConfig.from_env()


@pytest.mark.unit
def test_each_operation_suite_selects_one_exact_pytest_node(tmp_path):
    api=module('scripts.verify_demo')
    command=api.operation_command('readiness',tmp_path/'report.xml')
    assert 'tests/integration/test_operations.py::test_actual_readiness' in command
    assert '-k' not in command


@pytest.mark.unit
def test_runtime_factory_mounts_authenticated_mcp_and_owns_lifetime(monkeypatch):
    from starlette.testclient import TestClient
    api=module('gateway.runtime')
    monkeypatch.setattr(api,'database_url',lambda role:'postgresql+psycopg://gwt_0123456789abcdef_app:synthetic@127.0.0.1:1/gateway_test_0123456789abcdef')
    monkeypatch.setattr(api,'secret_key',lambda name:b'x'*32)
    closed=[];original=api.Runtime.close
    async def close(runtime):
        closed.append(True);await original(runtime)
    monkeypatch.setattr(api.Runtime,'close',close)
    app=api.app_factory()
    with TestClient(app,base_url='http://127.0.0.1:8080') as client:
        assert client.get('/healthz').status_code==200
        assert client.post('/mcp',json={}).status_code==401
    assert closed==[True]

# Review regressions: these simulate failures in subprocess boundaries only;
# none of these unit results substitute for actual operational evidence.
@pytest.mark.unit
@pytest.mark.parametrize('failure',[subprocess.TimeoutExpired(['stop'],1),RuntimeError('partial stop')])
def test_quiesce_recovers_original_running_services_after_stop_failure(monkeypatch,failure):
    api=module('scripts.demo_backup');calls=[]
    monkeypatch.setattr(api,'inspect_stack',lambda *a,**k:[{'service':s,'running':s!='worker'} for s in api.APPLICATION_SERVICES])
    monkeypatch.setattr(api,'availability',lambda state:{'health':200,'ready':200})
    monkeypatch.setattr(api,'recover_availability',lambda state,expected:calls.append(('checked',expected)))
    def command(state,*args,**kwargs):
        calls.append(args)
        if args[0]=='stop':raise failure
    monkeypatch.setattr(api,'compose',command)
    with pytest.raises(type(failure)):
        with api.quiesced_application({}):pytest.fail('Unquiesced work started')
    assert calls[1]==('start','edge','gateway','retention')
    assert calls[2][0]=='checked'


@pytest.mark.unit
def test_quiesce_reports_recovery_failure(monkeypatch):
    api=module('scripts.demo_backup')
    monkeypatch.setattr(api,'inspect_stack',lambda *a,**k:[{'service':'gateway','running':True}])
    monkeypatch.setattr(api,'availability',lambda state:{'health':200,'ready':503})
    def command(state,*args,**kwargs):
        if args[0]=='start':raise RuntimeError('start failed')
    monkeypatch.setattr(api,'compose',command)
    with pytest.raises(RuntimeError,match='recovery'):
        with api.quiesced_application({}):pass


@pytest.mark.unit
@pytest.mark.parametrize('stage',['pull','app','edge','config'])
def test_failed_preparation_retains_cleanup_identity_and_cleans_owned_resources(monkeypatch,tmp_path,stage):
    api=module('scripts.demo_runtime');calls=[]
    monkeypatch.setattr(api,'require_docker',lambda:None)
    def command(args,**kwargs):
        calls.append(args)
        if (stage=='pull' and args[:2]==['docker','pull']) or (stage in {'app','edge'} and args[:2]==['docker','build'] and ('gateway/Dockerfile' if stage=='app' else 'publisher/Dockerfile') in args) or (stage=='config' and '--quiet' in args and 'config' in args):
            raise RuntimeError('injected acquisition failure')
        return subprocess.CompletedProcess(args,0,'sha256:'+'a'*64 if args[:3]==['docker','image','inspect'] else '', '')
    monkeypatch.setattr(api,'run',command)
    path=tmp_path/'state'
    with pytest.raises(RuntimeError):api.prepare(path)
    identity=api.load_cleanup_state(path)
    assert identity['project'].startswith('gateway-demo-')
    assert any('label=gateway.cube.run='+identity['project'] in arg for call in calls for arg in call)
    assert json.loads((path/'cleanup-report.json').read_text())['status']=='PASS'
    assert not (path/'secrets').exists()
    with pytest.raises(ValueError):api.load_state(path)


@pytest.mark.unit
@pytest.mark.parametrize('stream',['stdout','stderr'])
def test_subprocess_stream_limit_terminates_before_unbounded_consumption(tmp_path,stream):
    api=module('scripts.demo_runtime');target=tmp_path/'output'
    code=f'import os,time; os.write({1 if stream=="stdout" else 2}, b"x"*8192); time.sleep(30)'
    with pytest.raises(RuntimeError,match='budget'):
        api.stream_command([sys.executable,'-c',code],target,stdout_limit=1024,stderr_limit=1024,timeout=2)
    assert not target.exists()


@pytest.mark.unit
def test_subprocess_timeout_reaps_child_and_removes_partial_file(tmp_path):
    api=module('scripts.demo_runtime');target=tmp_path/'output'
    with pytest.raises(subprocess.TimeoutExpired):
        api.stream_command([sys.executable,'-c','import time;time.sleep(30)'],target,timeout=.05)
    assert not target.exists()


@pytest.mark.unit
@pytest.mark.parametrize('name,size',[('registry.dump',300*1024*1024),('unexpected',1)])
def test_archive_header_rejected_before_payload_is_consumed(tmp_path,name,size,monkeypatch):
    api=module('scripts.demo_backup');import gzip,tarfile
    path=tmp_path/'bomb.gz'
    info=tarfile.TarInfo(name);info.size=size
    with gzip.open(path,'wb') as file:file.write(info.tobuf())
    # Header-only archive: safe admission rejects the declaration, not EOF from
    # trying to consume the missing expanded bytes.
    with pytest.raises(ValueError,match='member|limit'):api.inspect_backup(path)


@pytest.mark.unit
@pytest.mark.parametrize('installed_name,installed_version',[('other','1.0.0'),('example','2.0.0')])
def test_npm_notice_provenance_rejects_installed_lock_mismatch(tmp_path,installed_name,installed_version):
    api=module('scripts.license_inventory')
    directory=tmp_path/'node_modules/example';directory.mkdir(parents=True)
    (directory/'package.json').write_text(json.dumps({'name':installed_name,'version':installed_version}))
    with pytest.raises(ValueError):api.npm_provenance(tmp_path,'node_modules/example',{'version':'1.0.0','integrity':'sha512-test'},'example')


@pytest.mark.unit
def test_npm_notice_provenance_rejects_stale_install_receipt(tmp_path):
    api=module('scripts.license_inventory');directory=tmp_path/'node_modules/example';directory.mkdir(parents=True)
    (directory/'package.json').write_text(json.dumps({'name':'example','version':'1.0.0'}))
    (tmp_path/'node_modules/.package-lock.json').write_text(json.dumps({'packages':{'node_modules/example':{'version':'1.0.0','integrity':'sha512-other'}}}))
    with pytest.raises(ValueError):api.npm_provenance(tmp_path,'node_modules/example',{'version':'1.0.0','integrity':'sha512-test'},'example')

@pytest.mark.unit
@pytest.mark.parametrize('packet',[
 '12:00:00.000000 IP 172.18.0.2.43210 > 8.8.8.8.53: UDP, length 29',
 '12:00:00.000000 IP 172.18.0.2.43210 > 1.1.1.1.443: tcp 0'])
def test_egress_rejects_forbidden_captured_traffic(packet):
    api=module('scripts.check_egress')
    capture='tcpdump: listening on any\n'+packet+'\n1 packet captured\n1 packet received by filter\n0 packets dropped by kernel\n'
    with pytest.raises(ValueError):api.validate_capture(capture,{'172.18.0.2'})


@pytest.mark.unit
@pytest.mark.parametrize('capture',[
 'tcpdump: listening on any\n0 packets captured\n0 packets received by filter\n1 packet dropped by kernel\n',
 'tcpdump: listening on any\n0 packets captured\n',
 'tcpdump: listening on any\nUNKNOWN PACKET\n0 packets captured\n0 packets received by filter\n0 packets dropped by kernel\n'])
def test_egress_rejects_incomplete_or_lost_capture(capture):
    with pytest.raises(ValueError):module('scripts.check_egress').validate_capture(capture,set())


@pytest.mark.unit
def test_egress_requires_successful_workflow_coverage():
    with pytest.raises(ValueError,match='workflow'):module('scripts.check_egress').verify({},Path('/unused'))


@pytest.mark.unit
@pytest.mark.parametrize('change',['missing','source','image','skipped','identity'])
def test_prerequisite_admission_rejects_absent_stale_or_wrong_evidence(tmp_path,change):
    api=module('scripts.verify_demo')
    state={'source_digest':'a'*64};images={'postgres':'sha256:'+'b'*64}
    root=tmp_path/'prerequisites';root.mkdir()
    receipt={'source_digest':state['source_digest'],'images':images,'classification':'actual','suites':[]}
    for name,nodes in api.PREREQUISITES.items():
        path=root/(name+'.xml')
        node=nodes[0];classname,test=node.rsplit('::',1);classname=classname.removesuffix('.py').replace('/','.')
        path.write_text(f'<testsuite tests="1"><testcase classname="{classname}" name="{test}"/></testsuite>')
        receipt['suites'].append({'suite':name,'junit':path.name,'sha256':__import__('hashlib').sha256(path.read_bytes()).hexdigest()})
    if change=='source':receipt['source_digest']='c'*64
    if change=='image':receipt['images']={'postgres':'sha256:'+'d'*64}
    if change=='skipped':(root/'bootstrap.xml').write_text('<testsuite tests="1" skipped="1"/>')
    if change=='identity':receipt['suites'][0]['suite']='unrelated'
    if change!='missing':(root/'prerequisites.json').write_text(json.dumps(receipt))
    with pytest.raises(ValueError):api.validate_prerequisites(root,state,images)

@pytest.mark.unit
@pytest.mark.parametrize('stage',['up','registry_restore','source_restore','startup'])
def test_failed_restore_cleans_fresh_target_even_before_return(monkeypatch,tmp_path,stage):
    api=module('scripts.demo_backup');runtime=module('scripts.demo_runtime');calls=[]
    monkeypatch.setattr(runtime,'require_docker',lambda:None)
    def command(args,**kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args,0,'sha256:'+'a'*64 if args[:3]==['docker','image','inspect'] else '', '')
    monkeypatch.setattr(runtime,'run',command);monkeypatch.setattr(api,'run',command)
    def compose(state,*args,**kwargs):
        if stage=='up':raise RuntimeError('failed up')
    monkeypatch.setattr(api,'compose',compose)
    def restore_command(args,target,**kwargs):
        if stage=='registry_restore' and target.name.startswith('registry'):raise RuntimeError('failed pg_restore')
        if stage=='source_restore' and target.name.startswith('source'):return 1
        target.write_bytes(b'');return 0
    monkeypatch.setattr(api,'stream_command',restore_command)
    def start(state):raise RuntimeError('failed startup')
    monkeypatch.setattr(api,'start',start)
    inventory={'synthetic-boundary':'a'*64};monkeypatch.setattr(runtime,'source_inventory',lambda:inventory);deployment={'assets':{name:(ROOT/name).read_text() for name in api.ASSETS},
        'source_inventory':inventory,'source_digest':runtime.source_digest(inventory),'app_image':'sha256:'+'a'*64,'edge_image':'sha256:'+'a'*64}
    dumps=tmp_path/'dumps';dumps.mkdir()
    for name in ('registry.dump','source.dump'):(dumps/name).write_bytes(b'PGDMP')
    source=tmp_path/'working-stack';source.mkdir();(source/'sentinel').write_text('preserve')
    target=tmp_path/'restored'
    with pytest.raises(RuntimeError):api._restore(deployment,dumps,target,18080)
    identity=runtime.load_cleanup_state(target)
    assert json.loads((target/'cleanup-report.json').read_text())['status']=='PASS'
    assert not (target/'secrets').exists() and (source/'sentinel').read_text()=='preserve'
    assert all('label=gateway.cube.run='+identity['run_label'] in call for call in calls if 'ls' in call)


@pytest.mark.unit
def test_cleanup_failure_retains_verified_exact_leftover_inventory(monkeypatch,tmp_path):
    api=module('scripts.demo_runtime');path=tmp_path/'state';path.mkdir(mode=0o700)
    identity=dict(kind=api.KIND,project='gateway-demo-'+'a'*16,run_label='gateway-demo-'+'a'*16,state_directory=str(path),phase='allocated')
    api.private_json(path/'cleanup.json',identity)
    calls=[]
    def command(args,**kwargs):
        calls.append(args)
        if args[1]=='container':raise RuntimeError('denied container inventory')
        return subprocess.CompletedProcess(args,0,'','')
    monkeypatch.setattr(api,'run',command)
    with pytest.raises(RuntimeError,match='cleanup'):api.cleanup(identity)
    report=json.loads((path/'cleanup-report.json').read_text())
    assert report['status']=='BLOCKED' and report['leftovers']['container'] is None
    assert all(kind in report['leftovers'] for kind in ('network','volume','image'))
    assert all('label=gateway.cube.run='+identity['run_label'] in call for call in calls)


@pytest.mark.unit
def test_streamed_backup_output_uses_bounded_python_memory(tmp_path):
    import tracemalloc
    api=module('scripts.demo_runtime');target=tmp_path/'output'
    tracemalloc.start()
    try:
        code=api.stream_command([sys.executable,'-c','import os; [os.write(1,b"x"*65536) for _ in range(512)]'],target,stdout_limit=34*1024*1024)
        _,peak=tracemalloc.get_traced_memory()
    finally:tracemalloc.stop()
    assert code==0 and target.stat().st_size==32*1024*1024
    assert peak<3*1024*1024


@pytest.mark.unit
def test_unapproved_observer_does_not_elevate_or_block_business_workflow(monkeypatch,tmp_path):
    api=module('scripts.check_egress');runtime=module('scripts.demo_runtime');called=[]
    records=[{'service':name,'id':name,'pid':42,'image':'sha256:'+'a'*64} for name in runtime.RUNNING]
    monkeypatch.setattr(api,'require_docker',lambda:None);monkeypatch.setattr(api,'verify_images',lambda state:None)
    monkeypatch.setattr(api,'inspect_stack',lambda state:records)
    def forbidden(*args,**kwargs):pytest.fail('Unapproved observer must not launch a privileged helper or tcpdump')
    monkeypatch.setattr(api.subprocess,'Popen',forbidden)
    def workflow():
        called.append(True)
        return [{'suite':name,'classification':'actual','status':'PASS','tests':1} for name in api.WORKFLOW_SUITES]
    result=api.verify({'project':'synthetic','source_digest':'a'*64},tmp_path/'evidence',workflow=workflow)
    assert called==[True] and result['status']=='BLOCKED'
    assert result['reason']=='approved_scoped_observer_not_available'
    assert result['packet_observation_performed'] is False


@pytest.mark.unit
def test_prerequisite_positive_control_requires_every_collected_identity(monkeypatch,tmp_path):
    api=module('scripts.verify_demo');images={'postgres':'sha256:'+'b'*64};state={'source_digest':'a'*64}
    receipt={'classification':'actual','source_digest':state['source_digest'],'images':images,'suites':[]}
    monkeypatch.setattr(api,'collected_identities',lambda name:{('tests.synthetic.'+name,'test_real_case')})
    for name in api.PREREQUISITES:
        path=tmp_path/(name+'.xml');path.write_text(f'<testsuite tests="1"><testcase classname="tests.synthetic.{name}" name="test_real_case"/></testsuite>')
        receipt['suites'].append({'suite':name,'junit':path.name,'sha256':__import__('hashlib').sha256(path.read_bytes()).hexdigest()})
    (tmp_path/'prerequisites.json').write_text(json.dumps(receipt))
    assert api.validate_prerequisites(tmp_path,state,images)==receipt
    monkeypatch.setattr(api,'collected_identities',lambda name:{('tests.synthetic.'+name,'test_other_case')})
    with pytest.raises(ValueError,match='identity'):api.validate_prerequisites(tmp_path,state,images)

@pytest.mark.unit
def test_aggregate_cannot_accept_compatibility_label_without_bound_receipt(tmp_path):
    api=module('scripts.verify_demo');proof=tmp_path/'proof';proof.write_text('not a receipt')
    rows=[{'suite':name,'classification':'actual','status':'PASS','tests':1,'evidence_paths':['proof']} for name in api.MANDATORY]
    report=api.summarize(rows,source='a'*64,versions={},evidence_root=tmp_path)
    assert report['status']=='BLOCKED' and 'backend_compatibility' in report['missing']


@pytest.mark.unit
def test_npm_changed_notice_is_rejected_against_locked_tarball(tmp_path):
    import tarfile,io,base64,hashlib
    api=module('scripts.license_inventory');directory=tmp_path/'node_modules/example';directory.mkdir(parents=True)
    (directory/'package.json').write_text('{"name":"example","version":"1.0.0"}')
    (directory/'LICENSE').write_text('changed installed notice')
    tar=io.BytesIO()
    with tarfile.open(fileobj=tar,mode='w:gz') as archive:
        for name,data in [('package.json',(directory/'package.json').read_bytes()),('LICENSE',b'original')]:
            entry=tarfile.TarInfo('package/'+name);entry.size=len(data);archive.addfile(entry,io.BytesIO(data))
    data=tar.getvalue();digest=hashlib.sha512(data).digest();hexdigest=digest.hex()
    cache=tmp_path/'cache';path=cache/'_cacache/content-v2/sha512'/hexdigest[:2]/hexdigest[2:4]/hexdigest[4:]
    path.parent.mkdir(parents=True);path.write_bytes(data)
    with pytest.raises(ValueError,match='bytes differ'):
        api.npm_artifact(tmp_path,'node_modules/example',{'integrity':'sha512-'+base64.b64encode(digest).decode()},[cache])

@pytest.mark.unit
def test_archive_extra_member_is_rejected_incrementally(tmp_path):
    import io,tarfile
    api=module('scripts.demo_backup');path=tmp_path/'many.tar.gz'
    with tarfile.open(path,'w:gz',format=tarfile.USTAR_FORMAT) as archive:
        for name,data in [('registry.dump',b'PGDMP'),('source.dump',b'PGDMP'),('deployment.json',b'{}'),('checksums.json',b'{}'),('extra',b'x')]:
            entry=tarfile.TarInfo(name);entry.size=len(data);archive.addfile(entry,io.BytesIO(data))
    with pytest.raises(ValueError,match='member'):api.inspect_backup(path)


@pytest.mark.unit
def test_preparation_secret_failure_still_has_cleanup_identity(monkeypatch,tmp_path):
    api=module('scripts.demo_runtime');secrets_api=module('scripts.cube_runtime_secrets')
    monkeypatch.setattr(api,'require_docker',lambda:None)
    monkeypatch.setattr(api,'run',lambda args,**kwargs:subprocess.CompletedProcess(args,0,'',''))
    def failure(path):raise RuntimeError('synthetic secret-generation failure')
    monkeypatch.setattr(secrets_api,'prepare',failure)
    path=tmp_path/'state'
    with pytest.raises(RuntimeError):api.prepare(path)
    assert api.load_cleanup_state(path)['phase']=='allocated'
    assert not (path/'secrets').exists()
    assert json.loads((path/'cleanup-report.json').read_text())['status']=='PASS'

@pytest.mark.unit
@pytest.mark.parametrize('missing',['LICENSE','NOTICE'])
def test_locked_artifact_preserves_upstream_notice_missing_from_install(tmp_path,missing):
    import tarfile,io,base64,hashlib
    api=module('scripts.license_inventory');directory=tmp_path/'node_modules/example';directory.mkdir(parents=True)
    metadata=b'{"name":"example","version":"1.0.0"}'
    (directory/'package.json').write_bytes(metadata)
    notices={'LICENSE':b'Upstream license\r\n','NOTICE':b'Upstream notice\n'}
    for name,data in notices.items():
        if name!=missing:(directory/name).write_bytes(data)
    tar=io.BytesIO()
    with tarfile.open(fileobj=tar,mode='w:gz') as archive:
        for name,data in [('package.json',metadata),*notices.items()]:
            entry=tarfile.TarInfo('package/'+name);entry.size=len(data);archive.addfile(entry,io.BytesIO(data))
    data=tar.getvalue();digest=hashlib.sha512(data).digest();hexdigest=digest.hex()
    cache=tmp_path/'cache';path=cache/'_cacache/content-v2/sha512'/hexdigest[:2]/hexdigest[2:4]/hexdigest[4:]
    path.parent.mkdir(parents=True);path.write_bytes(data)
    result=api.npm_artifact(tmp_path,'node_modules/example',{'integrity':'sha512-'+base64.b64encode(digest).decode()},[cache])
    assert result['notice_bytes']==notices
    assert result['installed_missing_notices']==[missing]
    assert result['artifact_notice_sha256']=={name:hashlib.sha256(value).hexdigest() for name,value in notices.items()}

@pytest.mark.unit
@pytest.mark.parametrize('corruption',['none','source','image','duplicate','identity'])
def test_runtime_suite_receipt_requires_exact_binding_and_cases(tmp_path,corruption):
    import hashlib
    api=module('scripts.verify_demo');source='a'*64
    versions={'app_image':'sha256:'+'b'*64,'edge_image':'sha256:'+'c'*64,'backend_images':{'cube':{'image_id':'sha256:'+'d'*64}}}
    path=tmp_path/'readiness.xml';path.write_text('<testsuite tests="1"><testcase classname="tests.integration.test_operations" name="test_actual_readiness"/></testsuite>')
    row={'suite':'readiness','classification':'actual','status':'PASS','tests':1,'junit':path.name,
        'binding':{'source_digest':source,**versions},'evidence_paths':[path.name],'evidence_sha256':{path.name:hashlib.sha256(path.read_bytes()).hexdigest()}}
    if corruption=='source':row['binding']['source_digest']='e'*64
    if corruption=='image':row['binding']['app_image']='sha256:'+'f'*64
    if corruption=='identity':
        path.write_text('<testsuite tests="1"><testcase classname="tests.integration.test_operations" name="test_other"/></testsuite>');row['evidence_sha256'][path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
    rows=[row,row] if corruption=='duplicate' else [row]
    result=api.summarize(rows,source=source,versions=versions,evidence_root=tmp_path)
    assert ('readiness' not in result['missing'])==(corruption=='none')



def capture_controls():
    return [{'source':'127.0.0.1','source_port':32101,'target':'127.0.0.1','target_port':49101},
        {'source':'127.0.0.1','source_port':32102,'target':'127.0.0.1','target_port':49102}]


def controlled_capture(extra='',received=4,dropped=0):
    lines=['1.000001 IP 127.0.0.1.32101 > 127.0.0.1.49101: UDP, length 32']
    if extra:lines.append(extra)
    lines.append('2.000001 IP 127.0.0.1.32102 > 127.0.0.1.49102: UDP, length 32')
    return 'tcpdump: listening on any\n'+'\n'.join(lines)+f'\n{len(lines)} packets captured\n{received} packets received by filter\n{dropped} packets dropped by kernel\n'


@pytest.mark.unit
def test_linux_lossless_loopback_counter_inequality_is_admitted_with_controls():
    result=module('scripts.check_egress').validate_capture(controlled_capture(),set(),controls=capture_controls(),scope='namespace')
    assert result['captured']==2 and result['received by filter']==4


@pytest.mark.unit
@pytest.mark.parametrize('fault',['missing_begin','missing_end','kernel_loss','forbidden_tcp','forbidden_udp','forwarded_dns'])
def test_scoped_capture_rejects_missing_controls_loss_and_attributed_egress(fault):
    api=module('scripts.check_egress');extra='';drop=1 if fault=='kernel_loss' else 0
    if fault=='forbidden_tcp':extra='1.500001 IP 172.18.0.2.32100 > 1.1.1.1.443: Flags [S], length 0'
    if fault=='forbidden_udp':extra='1.500001 IP 172.18.0.2.32100 > 8.8.8.8.123: UDP, length 32'
    if fault=='forwarded_dns':extra='1.500001 IP 127.0.0.1.32100 > 8.8.8.8.53: 777+ A? probe.invalid. (31)'
    text=controlled_capture(extra,received=6,dropped=drop)
    if fault=='missing_begin':text=text.replace('127.0.0.1.32101','127.0.0.1.32201')
    if fault=='missing_end':text=text.replace('127.0.0.1.32102','127.0.0.1.32202')
    with pytest.raises(ValueError):api.validate_capture(text,{'172.18.0.2'},controls=capture_controls(),scope='namespace')


@pytest.mark.unit
def test_unrelated_runner_flow_is_excluded_only_at_the_selected_bridge():
    api=module('scripts.check_egress')
    text=controlled_capture('1.500001 IP 10.0.0.2.32100 > 140.82.112.3.443: Flags [P.], length 64',received=6)
    result=api.validate_capture(text,{'172.18.0.2'},controls=capture_controls(),scope='bridge',runtime_addresses={'172.18.0.2'})
    assert result['excluded_unrelated']==1
    with pytest.raises(ValueError):api.validate_capture(text,{'172.18.0.2'},controls=capture_controls(),scope='namespace')


@pytest.mark.unit
@pytest.mark.parametrize('resolver',[
    'nameserver 127.0.0.11\n# ExtServers: [host(127.0.0.1)]\n# Overrides: []\n',
    'nameserver 127.0.0.11\n# ExtServers: [127.0.0.1 8.8.8.8]\n# Overrides: [nameservers]\n',
    'nameserver 127.0.0.11\n# Used default nameservers.\n# ExtServers: [127.0.0.1]\n# Overrides: [nameservers]\n'])
def test_dns_attribution_rejects_host_fallback_and_extra_upstreams(resolver):
    with pytest.raises(ValueError):module('scripts.check_egress').validate_resolver(resolver,['127.0.0.1'])

@pytest.mark.unit
def test_observer_preflight_is_unprivileged_and_explicitly_incomplete(monkeypatch):
    api=module('scripts.check_egress')
    def forbidden(*a,**k):pytest.fail('Preflight must not invoke tools or request privilege')
    monkeypatch.setattr(api.subprocess,'Popen',forbidden)
    result=api.observer_preflight()
    assert result=={'status':'BLOCKED','reason':'approved_scoped_observer_not_available','privileged_action_performed':False}


@pytest.mark.unit
def test_standalone_capture_support_cannot_admit_a_fabricated_approved_report(tmp_path):
    api=module('scripts.check_egress')
    with pytest.raises(ValueError,match='not approved'):
        api.validate_report({'status':'PASS','classification':'actual','observer_profile':'claimed-root-helper'},tmp_path)


@pytest.mark.unit
def test_junit_case_outside_the_reported_suite_is_rejected(tmp_path):
    api=module('scripts.verify_demo');path=tmp_path/'outside.xml'
    path.write_text('<testsuites><testsuite tests="0"/><testcase classname="tests.somewhere" name="test_outside"/></testsuites>')
    with pytest.raises(ValueError):api.junit_count(path)


@pytest.mark.unit
def test_retained_summary_rejects_state_image_change_before_suite_admission(monkeypatch,tmp_path):
    api=module('scripts.demo_evidence');root=tmp_path/'verify-1';root.mkdir()
    (root/'report.json').write_text(json.dumps({'source':'a'*64,'versions':{'app_image':'sha256:'+'b'*64,'edge_image':'sha256:'+'c'*64},'suites':[]}))
    def forbidden(*a,**k):pytest.fail('Stale state/image report must not reach suite admission')
    monkeypatch.setattr(api,'summarize',forbidden)
    result=api.latest_evidence(tmp_path,state={'source_digest':'a'*64,'app_image':'sha256:'+'d'*64,'edge_image':'sha256:'+'c'*64})
    assert result['status']=='BLOCKED' and 'image' in result['reason']

@pytest.mark.unit
def test_integration_mode_requires_fixed_security_phase_before_mutations(tmp_path):
    api=module('scripts.verify_demo')
    assert 'acceptance_security' not in api.required_suites({})
    assert 'acceptance_security' in api.required_suites({'integration':True})
    commands=api.business_operation_commands(tmp_path,integration=True)
    assert [name for name,_ in commands]==['runtime_dependencies','readiness','consumer_rest','acceptance_security']
    assert commands[-1][1]==[sys.executable,'-m','pytest','tests/e2e/test_acceptance.py','tests/security/test_adversarial_matrix.py','-m','e2e','-q','--junitxml='+str(tmp_path/'acceptance_security.xml')]
    report=api.summarize([],source='a'*64,versions={'integration':True},evidence_root=tmp_path)
    assert 'acceptance_security' in report['missing']


@pytest.mark.unit
@pytest.mark.parametrize('variable,value', [('PYTEST_ADDOPTS','-k test_publish_race_commits_exactly_one_version'),('PYTEST_PLUGINS','scope_shrinker')])
@pytest.mark.parametrize('entrypoint', ['prerequisite','mcp','integration','static'])
def test_demo_identity_admission_rejects_ambient_pytest_scope(monkeypatch,variable,value,entrypoint):
    api=module('scripts.verify_demo')
    monkeypatch.setenv(variable,value)
    monkeypatch.setattr(api,'run',lambda *a,**k:subprocess.CompletedProcess([],0,'tests/e2e/test_two_clients.py::test_selected\ntests/integration/test_registry.py::test_selected\n',''))
    with pytest.raises(ValueError,match='pytest'):
        if entrypoint=='prerequisite':api.collected_identities('postgres_registry')
        else:api.expected_suite_identities({'mcp':'mcp_clients','integration':'acceptance_security','static':'readiness'}[entrypoint])


@pytest.mark.unit
def test_demo_pytest_environment_preserves_authority_disables_autoload_and_rejects_override(monkeypatch):
    api=module('scripts.verify_demo')
    monkeypatch.delenv('PYTEST_ADDOPTS',raising=False);monkeypatch.delenv('PYTEST_PLUGINS',raising=False)
    supplied={'GATEWAY_DEMO_TEST_AUTHORIZATION':'private-synthetic-gateway-demo','PYTEST_DISABLE_PLUGIN_AUTOLOAD':'0'}
    assert api.pytest_environment(supplied)=={**supplied,'PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1'}
    assert supplied['PYTEST_DISABLE_PLUGIN_AUTOLOAD']=='0'
    with pytest.raises(ValueError,match='pytest'):api.pytest_environment({'PYTEST_ADDOPTS':'-k omitted'})
    monkeypatch.setenv('PYTEST_ADDOPTS','-k omitted')
    with pytest.raises(ValueError,match='pytest'):api.pytest_environment({})


@pytest.mark.unit
def test_demo_suite_rejects_ambient_scope_before_launch(monkeypatch,tmp_path):
    api=module('scripts.verify_demo');marker=tmp_path/'launched'
    monkeypatch.setenv('PYTEST_ADDOPTS','-k selected')
    result=api.execute_suite('scope',[sys.executable,'-c',f'from pathlib import Path; Path({str(marker)!r}).touch()'],tmp_path)
    assert result['status']=='BLOCKED' and not marker.exists()


@pytest.mark.unit
@pytest.mark.parametrize('channel',[1,2])
def test_demo_suite_inner_log_has_streaming_byte_bound(monkeypatch,tmp_path,channel,capsys):
    api=module('scripts.verify_demo');monkeypatch.setattr(api,'MAX_SUITE_LOG_BYTES',1024,raising=False)
    code=f'import os; os.write({channel}, b"PRIVATE_CANARY"*1024)'
    result=api.execute_suite('bounded',[sys.executable,'-c',code],tmp_path,timeout=3)
    assert result['status']=='BLOCKED'
    path=tmp_path/'bounded.log'
    assert path.stat().st_size<=1024
    assert __import__('stat').S_IMODE(path.stat().st_mode)==0o600
    assert 'PRIVATE_CANARY' not in capsys.readouterr().out
    assert 'PRIVATE_CANARY' not in json.dumps(result)


@pytest.mark.unit
def test_demo_suite_timeout_reaps_child_and_blocks(tmp_path):
    api=module('scripts.verify_demo');pidfile=tmp_path/'child.pid'
    code=f'import os,time;from pathlib import Path;Path({str(pidfile)!r}).write_text(str(os.getpid()));time.sleep(30)'
    result=api.execute_suite('timeout',[sys.executable,'-c',code],tmp_path,timeout=.5)
    assert result['status']=='BLOCKED'
    pid=int(pidfile.read_text());proc=Path('/proc')/str(pid)
    assert not proc.exists(), 'Timed-out direct test child must be reaped'


@pytest.mark.unit
def test_demo_suite_does_not_pass_when_parent_leaves_silent_descendant(tmp_path):
    api=module('scripts.verify_demo');pidfile=tmp_path/'orphan.pid'
    nested=f'import os,time;from pathlib import Path;Path({str(pidfile)!r}).write_text(str(os.getpid()));time.sleep(30)'
    code=f'import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c",{nested!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);time.sleep(.2)'
    result=api.execute_suite('orphan',[sys.executable,'-c',code],tmp_path,timeout=3)
    pid=int(pidfile.read_text());proc=Path('/proc')/str(pid)
    try:
        assert result['status']=='BLOCKED'
        assert not proc.exists(), 'Silent owned descendant must not outlive the suite'
    finally:
        if proc.exists():
            try:__import__('os').kill(pid,9)
            except ProcessLookupError:pass


@pytest.mark.unit
def test_demo_suite_success_requires_supervised_clean_exit(tmp_path):
    api=module('scripts.verify_demo');junit=tmp_path/'positive.xml'
    code=f'import os;assert os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"]=="1";from pathlib import Path;Path({str(junit)!r}).write_text(\'<testsuite tests="1"><testcase classname="synthetic" name="test_control"/></testsuite>\');print("bounded positive control")'
    result=api.execute_suite('positive',[sys.executable,'-c',code],tmp_path,timeout=3,junit=junit)
    assert result['status']=='PASS' and result['tests']==1
    assert set(result['evidence_paths'])=={'positive.log','positive.xml'}


@pytest.mark.unit
def test_publisher_builder_acquires_exact_npm_before_version_gate(tmp_path):
    import os
    dockerfile=(ROOT/'publisher/Dockerfile').read_text()
    command=next(line.removeprefix('RUN ') for line in dockerfile.splitlines() if line.startswith('RUN '))
    node=tmp_path/'node';node.write_text('#!/bin/sh\nprintf "v24.19.0\\n"\n');node.chmod(0o700)
    npm=tmp_path/'npm'
    npm.write_text('''#!/bin/sh
case "$*" in
  "--version") if test -f "$NPM_TEST_STATE"; then printf '11.9.0\\n'; else printf '11.17.0\\n'; fi ;;
  "install --global --ignore-scripts --no-audit --no-fund npm@11.9.0") : > "$NPM_TEST_STATE" ;;
  "ci --ignore-scripts --no-audit --no-fund") test -f "$NPM_TEST_STATE" ;;
  *) exit 19 ;;
esac
''');npm.chmod(0o700)
    result=subprocess.run(['/bin/sh','-c',command],env={**os.environ,'PATH':str(tmp_path),'NPM_TEST_STATE':str(tmp_path/'installed')},capture_output=True,timeout=3)
    assert result.returncode==0, 'Pinned npm must be installed before asserting its version or installing app dependencies'
    assert (tmp_path/'installed').exists()
    assert 'npm@11.9.0' in command and 'node:24.19.0-bookworm-slim@sha256:' in dockerfile


@pytest.mark.unit
@pytest.mark.parametrize('fault',['unverified_cleanup','supervisor_exception','malformed_result','malformed_types','cancelled_supervisor','interrupted','ordinary_failure'])
def test_demo_sequence_stops_on_unknown_supervision_but_continues_clean_failures(monkeypatch,tmp_path,capsys,fault):
    from types import SimpleNamespace
    api=module('scripts.verify_demo');runner=module('scripts.runtime_process');egress=module('scripts.check_egress')
    state_dir=tmp_path/'state';state_dir.mkdir();(state_dir/'evidence').mkdir()
    receipts=tmp_path/'receipts';receipts.mkdir();(receipts/'prerequisites.json').write_text('{}')
    state={'source_digest':'a'*64,'app_image':'sha256:'+'b'*64,'edge_image':'sha256:'+'c'*64,'port':18080}
    monkeypatch.setattr(api,'load_state',lambda path:state)
    monkeypatch.setattr(api,'require_docker',lambda:None)
    monkeypatch.setattr(api,'verify_images',lambda state:None)
    monkeypatch.setattr(api,'inspect_stack',lambda state:[])
    monkeypatch.setattr(api,'prerequisite_images',lambda state:{})
    monkeypatch.setattr(api,'validate_prerequisites',lambda *args:{'suites':[]})
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,path):return SimpleNamespace(status_code=503)
    monkeypatch.setattr(__import__('httpx'),'Client',Client)
    def workflow_boundary(state,evidence,*,workflow):
        evidence.mkdir();workflow()
        (evidence/'network-report.json').write_text('{"status":"BLOCKED"}')
        return {'status':'BLOCKED'}
    monkeypatch.setattr(egress,'verify',workflow_boundary)
    launched=[]
    def controlled_supervisor(command,output,**kwargs):
        launched.append(output.stem);output.write_text('private synthetic control')
        result={'exit_status':1,'timed_out':False,'output_limit_exceeded':False,'interrupted':False,'descendant_cleanup':True,'orphaned_descendants':False}
        if output.stem=='consumer_rest':
            if fault=='unverified_cleanup':result['descendant_cleanup']=False
            elif fault=='supervisor_exception':raise RuntimeError('PRIVATE_SUPERVISOR_DETAIL')
            elif fault=='malformed_result':return {'descendant_cleanup':True}
            elif fault=='malformed_types':result['descendant_cleanup']=1
            elif fault=='cancelled_supervisor':raise SystemExit(143)
            elif fault=='interrupted':result['interrupted']=True
        return result
    monkeypatch.setattr(runner,'bounded_process',controlled_supervisor)
    monkeypatch.setattr(sys,'argv',['verify_demo','--integration','--state-dir',str(state_dir),'--prerequisites-dir',str(receipts)])
    assert api.main()==1
    report=json.loads(capsys.readouterr().out)
    assert report['status']=='BLOCKED' and 'PRIVATE_SUPERVISOR_DETAIL' not in json.dumps(report)
    if fault=='ordinary_failure':
        assert 'acceptance_security' in launched and launched[-1]=='authentication_replacement'
    else:
        assert launched==['publisher_browser','mcp_clients','runtime_dependencies','readiness','consumer_rest']
        row=next(row for row in report['suites'] if row['suite']=='consumer_rest')
        assert row['status']=='BLOCKED' and row['reason'] in {'unverified_supervision','suite_interrupted'}
        if row['reason']=='unverified_supervision':assert not row['evidence_paths']
        assert 'restart_persistence' in report['missing'] and 'acceptance_security' in report['missing']
        retained=json.loads(next((state_dir/'evidence').glob('verify-*/report.json')).read_text())
        assert retained==report

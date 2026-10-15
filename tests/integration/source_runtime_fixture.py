"""Fixed run-private synthetic bootstrap and controls for source serving tests.

Never accepts an organization URL, a principal selector, SQL, or a role override.
The host wrapper invokes four fixed transitions; Node sees only safe immutable
fixture claims and read-only ACK files, never owner/admin credentials or leases.
"""
import argparse
import asyncio
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[2]
AUTHORIZATION = 'private-synthetic-source-only'
OPERATIONS = ('revoke_queued', 'reclaim', 'close_main', 'close_zero')
CASES = ('main', 'revocation', 'reclaim', 'zero_job', 'unknown')
EPOCH = 'synthetic-source-epoch-v1'
DATABASE = 'gateway_source_test'
LEGACY = {'owner': 'gateway_owner', 'app': 'gateway_app',
    'audit_retention': 'gateway_audit_retention', 'source_reader': 'gateway_source_reader'}
SOURCE_ROLES = {'consumer': 'gateway_bq_consumer', 'fixture': 'gateway_bq_fixture', 'observer': 'gateway_bq_observer'}
BUNDLE_DIGEST = '59d35dbcb37d1f7237b5863699bac701b29d2aa2495672f8024b93a363bd5c27'


def validate_operation(value):
    if type(value) is not str or value not in OPERATIONS:
        raise ValueError('A fixed synthetic control operation is required')
    return value


def _read(path, maximum=65536):
    path = Path(path)
    if not path.is_absolute() or any(part.is_symlink() for part in [path, *path.parents]):
        raise ValueError('A regular private synthetic file is required')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError('Synthetic file exceeds its bound')
        return os.read(fd, maximum + 1)
    finally:
        os.close(fd)


def _secret(directory, name):
    value = _read(Path(directory)/name, 128).decode().strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', value):
        raise ValueError('Only generated private synthetic secrets are supported')
    return value


def _atomic(path, value):
    path = Path(path)
    if path.exists() or not path.parent.is_dir() or path.parent.is_symlink():
        raise ValueError('Synthetic evidence is one-shot and cannot be overwritten')
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    if len(encoded) > 65536:
        raise ValueError('Synthetic evidence exceeds its bound')
    temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(8))
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, encoded)
        os.fsync(fd)
        os.fchmod(fd, 0o444)
    finally:
        os.close(fd)
    # link is atomic and refuses an existing destination, unlike replace().
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink()


def _url(registry, user, password):
    return URL.create('postgresql+psycopg', username=user, password=password,
        host=registry['host'], port=registry['port'], database=DATABASE)


def _connect(url, **kwargs):
    return psycopg.connect(host=url.host, port=url.port, dbname=url.database,
        user=url.username, password=url.password, connect_timeout=5, **kwargs)


def source_profile():
    from gateway.adapters.bigquery_runtime import BigQuerySource
    return BigQuerySource.model_validate(dict(provider='bigquery', source_id='synthetic-usage', revision=1,
        context='consumer', auth={'mode': 'gke-workload-identity', 'expected_principal': 'executor@fixture-project.iam.gserviceaccount.com'},
        billing_project='fixture-project', data_project='fixture-project', dataset='inputs', tables=['usage'],
        location='US', dataset_location='US', cube_endpoint='https://cube.example.test',
        model_bundle_digest=BUNDLE_DIGEST, source_policy_digest='2'*64, max_jobs_per_execution=2,
        per_job_maximum_bytes_billed=1048576, total_execution_bytes=2097152,
        max_active_jobs=2, max_global_active_jobs=4, job_timeout_ms=25000))


def bundle_contract():
    root = ROOT/'tests/runtime/bigquery-serving-bundle'
    names = ['security.json', 'model/usage.yml']
    h = hashlib.sha256(b'spaceport-cube-bigquery-bundle-v1\0')
    for name in sorted(names, key=lambda name: name.encode()):
        data = _read(root/name)
        h.update(name.encode()+b'\0'+str(len(data)).encode()+b'\0'+data)
    if h.hexdigest() != BUNDLE_DIGEST:
        raise ValueError('Static serving fixture bundle drift')
    return json.loads(_read(root/'security.json'))


def _binding(source):
    from gateway.adapters.bigquery_runtime import BigQueryBinding, bigquery_security_contract
    result = BigQueryBinding.model_validate(dict(context='consumer', binding_id='usage-binding', revision=1,
        adapter_revision='cube-bigquery-v1', source_id=source.source_id, source_revision=source.revision,
        source_digest=source.digest, model_bundle_digest=source.model_bundle_digest,
        fields={'day': {'member': 'Usage.day', 'kind': 'time', 'cube_type': 'time', 'response_key': 'Usage.day.day', 'granularity': 'day'},
            'account': {'member': 'Usage.account', 'kind': 'dimension', 'cube_type': 'string', 'response_key': 'Usage.account'},
            'usage_events': {'member': 'Usage.events', 'kind': 'measure', 'cube_type': 'number', 'response_key': 'Usage.events'}},
        input_fields={'account': 'account'}, scope={'tenant_member': 'Usage.tenant', 'access_group_member': 'Usage.group'},
        freshness_mode='unknown', checkpoint_member=None))
    if bigquery_security_contract(result) != bundle_contract():
        raise ValueError('Python and retained Node source contracts differ')
    return result


def _manifest(binding):
    from gateway.contracts.manifest import Manifest
    def rename(value):
        if isinstance(value, dict): return {rename(k): rename(v) for k,v in value.items()}
        if isinstance(value, list): return [rename(v) for v in value]
        return {'segment': 'account', 'revenue': 'usage_events'}.get(value, value) if isinstance(value, str) else value
    value = rename(json.loads((ROOT/'fixtures/products/revenue.json').read_text()))
    value.update(product_id='synthetic-usage', binding_id=binding.binding_id, binding_revision=1,
        adapter_revision=binding.adapter_revision, model_bundle_digest=binding.model_bundle_digest,
        policy_id='synthetic-source-policy', content_digest=None, timezone='UTC')
    value['freshness']['required'] = False
    return Manifest.model_validate(value)


def _claims(context, decision, contract, query):
    from gateway.contracts.canonical import canonical_digest
    import time
    now = int(time.time())
    return dict(iss='dpg-gateway', aud='dpg-cube-source-v1', iat=now, exp=now+60,
        principal_id=decision.principal_id, tenant_id=decision.tenant_id, policy_revision=decision.policy_revision,
        model_bundle_digest=context.spec.model_bundle_digest, context=context.spec.context,
        row_constraints=decision.model_dump(mode='json')['row_constraints'], adapter_revision='cube-bigquery-v1',
        contract_digest=canonical_digest(contract), source_attempt=context.wire_context(), cube_query_digest=canonical_digest(query))


async def seed_sessions(owner_url, app_url, source):
    """Accepted synthetic owners, then the real audit/admission/materialization path."""
    from gateway.auth.principals import PrincipalStore
    from gateway.contracts.canonical import canonical_digest
    from gateway.contracts.execution import ExecutionSpec, PinnedProduct, PreparedQuery
    from gateway.contracts.identity import Principal
    from gateway.contracts.query import QueryRequest, validate_query
    from gateway.adapters.base import authorization_scope_digest
    from gateway.adapters.bigquery_runtime import compile_bigquery_query
    from gateway.policy.audit import AuditWriter
    from gateway.policy.service import PolicyService
    from gateway.registry.repository import Registry
    from gateway.registry.models import BindingRevision, TestRun, Execution, ExecutionState, ExecutionLease, ExecutionAuthorization
    from gateway.source_lifecycle import SourceLifecycle, SourceSessionSpec
    binding = _binding(source); manifest = _manifest(binding)
    policy = {'rules': {'tenant_ids': ['north'],
        'tenant_constraint': {'member': 'Usage.tenant', 'operator': 'eq', 'source': 'grant.tenant_id'},
        'entitlement_constraint': {'member': 'Usage.group', 'operator': 'in', 'source': 'grant.entitlements.groups'},
        'visible_fields': ['day', 'account', 'usage_events']}, 'reason': 'Fixed private synthetic serving acceptance'}
    with _connect(owner_url) as connection:
        connection.execute('INSERT INTO gateway.policies(id,current_revision) VALUES (%s,1)', (manifest.policy_id,))
        connection.execute('INSERT INTO gateway.policy_revisions(id,policy_id,revision,content,content_digest,actor_id) VALUES (%s,%s,1,%s,%s,%s)',
            ('source-policy-1', manifest.policy_id, Jsonb(policy), canonical_digest(policy), 'synthetic-operator'))
        for name in CASES:
            identity = 'source-'+name.replace('_', '-')
            connection.execute('INSERT INTO gateway.principals(id,subject,kind,active) VALUES (%s,%s,%s,true)', (identity, identity, 'demo'))
            connection.execute('INSERT INTO gateway.principal_grants(principal_id,policy_id,tenant_id,roles,entitlements) VALUES (%s,%s,%s,%s,%s)',
                (identity, manifest.policy_id, 'north', Jsonb(['consumer']), Jsonb({'groups': ['north']})))
    registry = Registry(app_url.render_as_string(hide_password=False))
    try:
        async with registry.transaction() as uow:
            uow.session.add(BindingRevision(id='source-binding-1', binding_id=binding.binding_id, revision=1,
                content=binding.model_dump(mode='json'), content_digest=binding.digest, model_bundle_digest=binding.model_bundle_digest,
                adapter_revision=binding.adapter_revision, actor_id='synthetic-operator'))
        draft = await registry.save_draft(manifest.product_id, manifest, 0, 'synthetic-editor')
        async with registry.transaction() as uow:
            uow.session.add(TestRun(id='source-test-1', product_id=manifest.product_id, draft_id=draft.id, draft_digest=draft.content_digest,
                binding_id='source-binding-1', binding_digest=binding.digest, policy_id=manifest.policy_id, policy_revision=1,
                fixture_version='1', evidence={'synthetic': 'passed'}, status='passed', actor_id='synthetic-tester'))
        async with registry.transaction() as uow:
            version = await uow.insert_version(draft_id=draft.id, expected_revision=draft.revision,
                test_run_id='source-test-1', actor_id='synthetic-publisher', version_id='source-version-1')
        product = PinnedProduct(manifest=manifest, binding_digest=binding.digest, version_id=version.id)
        policy_service = PolicyService(registry, PrincipalStore(registry.sessions), AuditWriter(b'synthetic-source-audit-key-32bytes'))
        lifecycle = SourceLifecycle(registry, policy_service, SimpleNamespace(resolve=lambda ref: source), deployment_epoch=EPOCH)
        request = validate_query(manifest, QueryRequest(product_id=manifest.product_id, version='1.0.0', inputs={'start': '2024-03-09', 'end': '2024-03-12'}))
        canonical = request.model_dump(mode='json', exclude={'idempotency_key'})
        cases = []
        for name in CASES:
            identity = 'source-'+name.replace('_', '-')
            actor = Principal(id=identity, subject=identity, kind='demo', service_client_id=None)
            spec = ExecutionSpec(principal_id=actor.id, pinned_product=product, canonical_query=canonical,
                canonical_hash=canonical_digest(canonical), policy_revision=1, context='consumer', request_id=str(uuid4()))
            async with registry.transaction() as uow:
                await lifecycle._lock_authority(uow, product)
                decision = await policy_service.authorize(actor, 'execute', product, uow, parameters=dict(request.inputs), correlation_id=spec.request_id)
                execution = Execution(id=str(uuid4()), principal_id=actor.id, product_id=manifest.product_id, version_id=version.id,
                    policy_id=manifest.policy_id, policy_revision=1, canonical_query=canonical, canonical_hash=spec.canonical_hash,
                    context='consumer', request_id=spec.request_id, deadline_at=func.clock_timestamp(), execution_timeout_seconds=120)
                uow.session.add(execution); await uow.session.flush(); await uow.session.refresh(execution)
                uow.session.add(ExecutionAuthorization(execution_id=execution.id, principal_snapshot=actor.model_dump(mode='json'),
                    decision=decision.model_dump(mode='json'), scope_digest=authorization_scope_digest(decision), full_decision_digest=canonical_digest(decision)))
                uow.session.add(ExecutionState(execution_id=execution.id, state='running', started_at=execution.created_at))
                token = str(uuid4())
                uow.session.add(ExecutionLease(execution_id=execution.id, worker_id='source-serving-test', lease_token=token,
                    generation=1, expires_at=execution.created_at+timedelta(seconds=30), source_reserved_until=execution.deadline_at))
                await uow.session.flush()
                group = await lifecycle.reserve_consumer(uow, (execution, spec, actor, decision, binding), decision, execution.deadline_at)
                prepared = PreparedQuery(endpoint_id='synthetic-source', model_bundle_digest=binding.model_bundle_digest,
                    cube_query=compile_bigquery_query(binding, manifest, request, decision))
                plan = SourceSessionSpec(provider='bigquery', source_id=source.source_id, source_revision=1, context='consumer',
                    capacity_key=source.capacity_pool_id, definition_digest=source.digest, request_digest=spec.canonical_hash,
                    prepared_digest=canonical_digest(prepared.cube_query), scope_digest=authorization_scope_digest(decision),
                    decision_digest=canonical_digest(decision), binding_digest=binding.digest, model_bundle_digest=binding.model_bundle_digest,
                    catalog_digest=None, max_submissions=source.max_jobs_per_execution,
                    max_bytes_per_submission=source.per_job_maximum_bytes_billed, max_total_bytes=source.total_execution_bytes)
                lifecycle._validate_plan(spec, prepared, decision, binding, plan)
                context = await lifecycle._materialize(uow, group=group, owner_id=execution.id, case_id=execution.id,
                    spec=spec, principal=actor, actor=actor, binding=binding, prepared=prepared, executed=decision,
                    current=decision, plan=plan, generation=1, token=token, deadline=execution.deadline_at)
            cases.append(dict(name=name, source_attempt=context.wire_context(),
                claims=_claims(context, decision, bundle_contract(), prepared.cube_query), cube_query=prepared.cube_query,
                expected_rows=[{'Usage.day': '2024-03-09T00:00:00.000', 'Usage.day.day': '2024-03-09T00:00:00.000', 'Usage.account': 'A', 'Usage.events': '7'}]))
        return cases
    finally:
        await registry.close()


async def _bootstrap(admin_url, secret_dir, output, *, local=False):
    from alembic import command
    from alembic.config import Config
    from gateway.source_lifecycle import provision_sources
    secret_dir = Path(secret_dir); output = Path(output)
    if output.exists(): raise ValueError('Synthetic bootstrap is one-shot')
    registry = dict(host=admin_url.host, port=admin_url.port, database=DATABASE)
    passwords = {key: _secret(secret_dir, 'registry_'+key) for key in ('owner', 'app')}
    with _connect(admin_url, autocommit=True) as admin:
        if local:
            for kind, role in LEGACY.items():
                password = passwords.get(kind, secrets.token_urlsafe(32))
                admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}').format(sql.Identifier(role), sql.Literal(password)))
        for kind, role in SOURCE_ROLES.items():
            admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}').format(sql.Identifier(role), sql.Literal(_secret(secret_dir, 'source_bq_'+kind))))
        admin.execute(sql.SQL('CREATE DATABASE {} OWNER {} TEMPLATE template0').format(sql.Identifier(DATABASE), sql.Identifier(LEGACY['owner'])))
        admin.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(DATABASE)))
        for role in [*LEGACY.values(), *SOURCE_ROLES.values()]:
            admin.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(DATABASE), sql.Identifier(role)))
    owner = _url(registry, LEGACY['owner'], passwords['owner']); app = _url(registry, LEGACY['app'], passwords['app'])
    engine = create_engine(owner, echo=False, hide_parameters=True)
    try:
        config = Config(str(ROOT/'alembic.ini')); config.set_main_option('script_location', str(ROOT/'migrations'))
        prior = {f'GATEWAY_{kind.upper()}_ROLE': os.environ.get(f'GATEWAY_{kind.upper()}_ROLE') for kind in LEGACY}
        try:
            os.environ.update({f'GATEWAY_{kind.upper()}_ROLE': role for kind, role in LEGACY.items()})
            with engine.begin() as connection:
                config.attributes['connection'] = connection
                command.upgrade(config, 'head')
        finally:
            for key, value in prior.items():
                if value is None: os.environ.pop(key, None)
                else: os.environ[key] = value
        source = source_profile()
        fixture_source = type(source).model_validate({**source.model_dump(mode='json'), 'source_id': 'synthetic-usage-fixture', 'context': 'fixture_test',
            'auth': {'mode': 'gke-workload-identity', 'expected_principal': 'fixture@fixture-project.iam.gserviceaccount.com'}})
        with engine.begin() as connection:
            provision_sources(connection, [source, fixture_source], [
                dict(login_name=SOURCE_ROLES['consumer'], source_id=source.source_id, revision=1, context='consumer', role_kind='runtime'),
                dict(login_name=SOURCE_ROLES['fixture'], source_id=fixture_source.source_id, revision=1, context='fixture_test', role_kind='runtime'),
                *[dict(login_name=SOURCE_ROLES['observer'], source_id=item.source_id, revision=1, context=item.context, role_kind='observer') for item in [source, fixture_source]]],
                deployment_epoch=EPOCH, max_active_operations=10)
    finally:
        engine.dispose()
    cases = await seed_sessions(owner, app, source)
    mount = str(ROOT/'tests/runtime/bigquery-serving-bundle') if local else '/cube/conf/tests/runtime/bigquery-serving-bundle'
    _atomic(output, dict(schema_version=1, deployment_epoch=EPOCH, registry=registry, source=source.model_dump(mode='json'),
        bundle=dict(root=mount, digest=BUNDLE_DIGEST, contract=bundle_contract()), signing_key_file=str(secret_dir/'cube_signing_key'),
        runtime=dict(user=SOURCE_ROLES['consumer'], password_file=str(secret_dir/'source_bq_consumer')),
        observer=dict(user=SOURCE_ROLES['observer'], password_file=str(secret_dir/'source_bq_observer')), cases=cases))
    return output


async def bootstrap_local(admin_url, directory):
    from tests.integration.conftest import validate_test_admin_url
    admin_url = validate_test_admin_url(admin_url.render_as_string(hide_password=False), os.environ.get('GATEWAY_TEST_POSTGRES_AUTHORIZATION'))
    directory = Path(directory); secret_dir = directory/'secrets'; secret_dir.mkdir(mode=0o700)
    for name in ('registry_owner', 'registry_app', 'cube_signing_key', 'source_bq_consumer', 'source_bq_fixture', 'source_bq_observer'):
        path = secret_dir/name; path.write_text(secrets.token_urlsafe(32)); path.chmod(0o600)
    return await _bootstrap(admin_url, secret_dir, directory/'source-runtime-fixture.json', local=True)


async def _control(fixture_file, operation, *, secret_dir):
    validate_operation(operation)
    fixture_file = Path(fixture_file)
    fixture = json.loads(_read(fixture_file))
    if fixture['schema_version'] != 1 or fixture['deployment_epoch'] != EPOCH or fixture['registry']['database'] != DATABASE or fixture['source'] != source_profile().model_dump(mode='json'):
        raise ValueError('Synthetic control fixture mismatch')
    if [case['name'] for case in fixture['cases']] != list(CASES): raise ValueError('Synthetic case inventory mismatch')
    acknowledgement = fixture_file.with_name('source-runtime-control-'+operation+'.json')
    if acknowledgement.exists(): raise ValueError('Synthetic control is one-shot')
    expected = OPERATIONS.index(operation)
    if any(not fixture_file.with_name('source-runtime-control-'+name+'.json').exists() for name in OPERATIONS[:expected]):
        raise ValueError('Synthetic control order mismatch')
    name = {'revoke_queued': 'revocation', 'reclaim': 'reclaim', 'close_main': 'main', 'close_zero': 'zero_job'}[operation]
    case = next(value for value in fixture['cases'] if value['name'] == name)
    if case['claims']['principal_id'] != 'source-'+name.replace('_', '-'):
        raise ValueError('Fixed synthetic principal required')
    registry = fixture['registry']
    app = _url(registry, LEGACY['app'], _secret(secret_dir, 'registry_app'))
    owner = _url(registry, LEGACY['owner'], _secret(secret_dir, 'registry_owner'))
    result = dict(schema_version=1, operation=operation)
    # The fixture's retained immutable context selects only this pre-seeded case.
    with _connect(owner) as connection:
        connection.execute('SELECT gateway.source_check_epoch(%s)', (EPOCH,))
        row = connection.execute('SELECT owner_id,plan,generation,deadline_at FROM gateway.source_sessions WHERE id=%s AND group_id=%s',
            (case['source_attempt']['session_id'], case['source_attempt']['group_id'])).fetchone()
        if row is None or row[1]['prepared_digest'] != case['source_attempt']['prepared_digest'] or row[2] != 1 or connection.execute('SELECT principal_id FROM gateway.executions WHERE id=%s', (row[0],)).fetchone()[0] != case['claims']['principal_id']:
            raise ValueError('Synthetic retained case mismatch')
        if operation == 'revoke_queued':
            changed = connection.execute('UPDATE gateway.principals SET active=false WHERE id=%s AND active', (case['claims']['principal_id'],)).rowcount
            if changed != 1: raise ValueError('Synthetic revoke was not one-shot')
    with _connect(app) as connection:
        connection.execute('SELECT gateway.source_check_epoch(%s)', (EPOCH,))
        if operation == 'reclaim':
            token = str(uuid4())
            changed = connection.execute('UPDATE gateway.execution_leases SET generation=2,lease_token=%s,expires_at=LEAST(%s,clock_timestamp()+interval \'30 seconds\') WHERE execution_id=%s AND generation=1',
                (token, row[3], row[0])).rowcount
            if changed != 1: raise ValueError('Synthetic reclaim was not one-shot')
            connection.execute('SELECT gateway.source_reclaim_session(%s,%s,%s,%s)', (case['source_attempt']['session_id'], 2, token, row[3]))
        elif operation.startswith('close_'):
            connection.execute('SELECT gateway.source_close_group(%s,%s)', (case['source_attempt']['group_id'], 'completed'))
    if operation == 'reclaim':
        from gateway.source_lifecycle import SourceLifecycle
        from psycopg.rows import dict_row
        with _connect(owner, row_factory=dict_row) as connection:
            retained = connection.execute('SELECT * FROM gateway.source_sessions WHERE id=%s', (case['source_attempt']['session_id'],)).fetchone()
        context = SourceLifecycle._context(retained)
        result.update(source_attempt=context.wire_context(), claims={**case['claims'], 'source_attempt': context.wire_context()})
    _atomic(acknowledgement, result)


async def control_local(fixture_file, operation):
    from tests.integration.conftest import validate_test_admin_url
    admin = validate_test_admin_url(os.environ.get('GATEWAY_TEST_POSTGRES_ADMIN_URL'), os.environ.get('GATEWAY_TEST_POSTGRES_AUTHORIZATION'))
    fixture = json.loads(_read(fixture_file))
    if fixture['registry'] != dict(host=admin.host, port=admin.port, database=DATABASE):
        raise ValueError('Exact disposable registry required')
    return await _control(fixture_file, operation, secret_dir=Path(fixture_file).parent/'secrets')


def main():
    parser = argparse.ArgumentParser(description='Run-private synthetic source fixture only')
    sub = parser.add_subparsers(dest='command', required=True)
    bootstrap = sub.add_parser('bootstrap'); bootstrap.add_argument('--secret-dir', required=True); bootstrap.add_argument('--output', required=True)
    control = sub.add_parser('control'); control.add_argument('--fixture-file', required=True); control.add_argument('--operation', required=True, choices=OPERATIONS)
    args = parser.parse_args()
    try:
        if os.environ.get('GATEWAY_SOURCE_TEST_AUTHORIZATION') != AUTHORIZATION: raise ValueError('Synthetic authorization required')
        if args.command == 'bootstrap':
            if args.secret_dir != '/run/secrets' or args.output != '/evidence/source-runtime-fixture.json': raise ValueError('Fixed synthetic paths required')
            admin = URL.create('postgresql+psycopg', username='synthetic_admin', password=_secret('/run/secrets', 'registry_admin'),
                host='registry-db', port=5432, database='gateway_registry')
            asyncio.run(_bootstrap(admin, Path(args.secret_dir), Path(args.output)))
        else:
            if args.fixture_file != '/evidence/source-runtime-fixture.json': raise ValueError('Fixed synthetic paths required')
            fixture = json.loads(_read(args.fixture_file))
            if fixture['registry'] != dict(host='registry-db', port=5432, database=DATABASE): raise ValueError('Fixed private registry required')
            asyncio.run(_control(args.fixture_file, args.operation, secret_dir=Path('/run/secrets')))
        return 0
    except Exception:
        # Never print DB errors, SQL, leases, keys, claims, or provider payloads.
        print('Synthetic source fixture did not complete.', flush=True)
        return 1


if __name__ == '__main__': raise SystemExit(main())

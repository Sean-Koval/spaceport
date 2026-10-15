"""Fixed exact-image journey entrypoint; closed synthetic descriptor on stdin.

The host attests the immutable image and current disposable PostgreSQL container.
This entrypoint independently checks its readonly source bytes and generated
registry identities, then runs the exact same scenario as the collected test.
"""
import asyncio
import json
import os
import re
import signal
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.integration.source_journey_host import IMAGE_STAGES, failure_reason

CHECKS=['package_bootstrap','fixture_publish','catalog_describe','consumer_result',
    'durable_settlement','provider_guard','teardown']
MARKER='GATEWAY_BQ_GATEWAY_JOURNEY '
ROLES=('owner','app','audit_retention','source_reader')


def reject_source_package_shadowing(root):
    # Node resolves these source-tree ancestors before NODE_PATH. The source
    # inventory deliberately excludes dependency trees, so none may shadow the
    # actual immutable image packages. lexists also rejects dangling symlinks.
    for relative in ('node_modules','tests/node_modules','tests/runtime/node_modules',
            'cube/node_modules','cube/bigquery-runtime/node_modules'):
        if os.path.lexists(root/relative):
            raise ValueError('Immutable image package resolution required')


def parse_descriptor(raw,source_digest):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError
            result[key]=value
        return result
    try:
        if type(raw) is not bytes or not 1<=len(raw)<=16384:raise ValueError
        value=json.loads(raw,object_pairs_hook=pairs)
        if (type(value) is not dict or set(value)!={'schema_version','source_digest','image_id','registry'}
                or type(value['schema_version']) is not int or value['schema_version']!=1
                or type(value['source_digest']) is not str or re.fullmatch('[a-f0-9]{64}',value['source_digest']) is None
                or value['source_digest']!=source_digest or type(value['image_id']) is not str
                or re.fullmatch('sha256:[a-f0-9]{64}',value['image_id']) is None):raise ValueError
        registry=value['registry']
        if (type(registry) is not dict or set(registry)!={'host','port','database','roles','passwords','admin_password'}
                or registry['host']!='127.0.0.1' or type(registry['port']) is not int or not 1<=registry['port']<=65535
                or type(registry['database']) is not str
                or re.fullmatch('gateway_test_[a-f0-9]{16}',registry['database']) is None
                or type(registry['roles']) is not dict or set(registry['roles'])!=set(ROLES)
                or type(registry['passwords']) is not dict or set(registry['passwords'])!=set(ROLES)):
            raise ValueError
        token=registry['database'].removeprefix('gateway_test_')
        for role in ROLES:
            if registry['roles'][role]!='gwt_'+token+'_'+role:raise ValueError
        for password in [*registry['passwords'].values(),registry['admin_password']]:
            if type(password) is not str or re.fullmatch('[A-Za-z0-9_-]{43}',password) is None:raise ValueError
        return value
    except (ValueError,TypeError,KeyError,UnicodeError):
        raise ValueError('Invalid synthetic journey descriptor') from None


def database_from_descriptor(value):
    from sqlalchemy.engine import URL
    from tests.integration.conftest import RegistryDatabase,connection_kwargs
    import psycopg
    registry=value['registry']
    # This is the only allowed topology translation: the host already attested
    # its published loopback port belongs to the exact joined PG namespace.
    urls={role:URL.create('postgresql+psycopg',username=registry['roles'][role],
        password=registry['passwords'][role],host='127.0.0.1',port=5432,database=registry['database']) for role in ROLES}
    admin=URL.create('postgresql+psycopg',username='gateway_test_admin',password=registry['admin_password'],
        host='127.0.0.1',port=5432,database='gateway_test_admin')
    with psycopg.connect(**connection_kwargs(urls['owner'])) as connection:
        head=connection.execute('SELECT version_num FROM public.alembic_version').fetchone()[0]
        identity=connection.execute('SELECT current_database(),current_user').fetchone()
        empty=connection.execute('SELECT count(*) FROM gateway.source_operation_groups').fetchone()[0]
        if head!='0006_source_preparations' or identity!=(registry['database'],registry['roles']['owner']) or empty!=0:
            raise ValueError('The exact empty migrated synthetic database is required')
    return RegistryDatabase(None,dict(registry['roles']),urls,head,[],[],admin_url=admin)


def validate_counts(counts):
    if (type(counts) is not dict or set(counts)!={'inserts','metadata','results','provider_errors'}
            or any(type(number) is not int for number in counts.values()) or counts['inserts']!=10
            or not 10<=counts['metadata']<=10000 or not 5<=counts['results']<=16 or counts['provider_errors']!=0):
        raise ValueError
    return counts


async def _timed_scenario(database,root,stage):
    from tests.integration.test_bigquery_gateway import gateway_journey
    return await asyncio.wait_for(gateway_journey(database,root,stage=stage),60)


def main():
    stage='bootstrap'
    def report(value):
        nonlocal stage
        if type(value) is not str or value not in IMAGE_STAGES:raise ValueError
        stage=value
    def expired(*args):raise TimeoutError('Synthetic journey deadline elapsed')
    previous=signal.signal(signal.SIGALRM,expired)
    signal.alarm(75)
    try:
        smoke=sys.argv[1:]==['--smoke']
        if not smoke and (len(sys.argv)!=1 or os.environ.get('GATEWAY_SOURCE_TEST_AUTHORIZATION')!='private-synthetic-source-only'):
            raise ValueError
        root=Path(__file__).resolve().parents[2]
        if root!=Path('/app'):raise ValueError
        reject_source_package_shadowing(root)
        # Import-only smoke uses the exact image's Python/native wheels and the
        # same readonly source. No descriptor, connection or scenario is used.
        import psycopg
        import sqlalchemy
        import pytest
        import gateway.source_pilot_bootstrap
        import gateway.adapters.bigquery_serving
        import gateway.execution.worker
        import gateway.api.catalog
        import tests.integration.source_pilot_bigquery_seed
        import tests.integration.source_pilot_bigquery_child
        import tests.integration.test_bigquery_gateway
        if smoke:
            print('GATEWAY_BQ_GATEWAY_JOURNEY_SMOKE {"schema_version":1,"status":"passed"}',flush=True)
            return 0
        stage='source'
        from scripts.demo_runtime import source_inventory,source_digest
        digest=source_digest(source_inventory())
        stage='descriptor'
        value=parse_descriptor(sys.stdin.buffer.read(16385),digest)
        stage='database';database=database_from_descriptor(value)
        stage='package_bootstrap'
        with TemporaryDirectory(prefix='spaceport-bq-journey-') as directory:
            counts=asyncio.run(_timed_scenario(database,Path(directory)/'pilot',report))
        stage='counts'
        validate_counts(counts)
        print(MARKER+json.dumps(dict(schema_version=1,kind='bigquery_gateway_journey',status='passed',
            source_digest=value['source_digest'],image_id=value['image_id'],checks=CHECKS,counts=counts),separators=(',',':')),flush=True)
        return 0
    except BaseException as error:
        # Never echo exceptions, stdin, SQL, child output, headers or credentials.
        print(MARKER+json.dumps(dict(schema_version=1,kind='bigquery_gateway_journey',status='failed',stage=stage,error=failure_reason(error)),separators=(',',':')),flush=True)
        return 1
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM,previous)


if __name__=='__main__':raise SystemExit(main())

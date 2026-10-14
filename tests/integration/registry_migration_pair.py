"""Two real owner upgrades in one fresh, bounded test-fixture interpreter.

This is setup evidence only. Each upgrade still reloads the existing Alembic
environment with a new Config and its own disposed NullPool connection.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys

from alembic import command
from alembic.config import Config
import psycopg
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
TARGETS = frozenset(('head', '0001_registry'))
ROLE_KEYS = ('owner', 'app', 'audit_retention', 'source_reader')
MAX_RECEIPT_BYTES = 256 * 1024


def fixture_url(target):
    if target not in TARGETS or os.environ.get('GATEWAY_TEST_POSTGRES_AUTHORIZATION') != 'disposable-local-postgres-only':
        raise ValueError('Disposable fixture migration authority required')
    url = make_url(os.environ.get('GATEWAY_MIGRATION_DATABASE_URL', ''))
    match = re.fullmatch(r'gateway_test_([a-f0-9]{16})', url.database or '')
    if (url.drivername != 'postgresql+psycopg' or url.host not in {'127.0.0.1', 'localhost'}
        or not url.port or not url.password or url.query or not match):
        raise ValueError('Disposable fixture owner identity required')
    roles = {key: 'gwt_' + match[1] + '_' + key for key in ROLE_KEYS}
    if url.username != roles['owner'] or any(os.environ.get('GATEWAY_' + key.upper() + '_ROLE') != role
        for key, role in roles.items()):
        raise ValueError('Disposable fixture roles changed')
    return url


def migration_snapshot(url):
    with psycopg.connect(host=url.host, port=url.port, dbname=url.database,
        user=url.username, password=url.password, connect_timeout=5) as connection:
        rows = connection.execute("SELECT n.nspname, c.relname, c.oid, c.relfilenode FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='gateway' OR (n.nspname='public' AND c.relname='alembic_version') ORDER BY n.nspname,c.relname").fetchall()
    return [list(row) for row in rows]


def collect_pair(target):
    url = fixture_url(target)
    command.upgrade(Config(str(ROOT / 'alembic.ini')), target)
    first = migration_snapshot(url)
    command.upgrade(Config(str(ROOT / 'alembic.ini')), target)
    second = migration_snapshot(url)
    return {'schema_version': 1, 'database': url.database, 'owner': url.username,
        'target': target, 'first': first, 'second': second}


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value: raise ValueError('Duplicate fixture receipt key')
        value[key] = item
    return value


def parse_receipt(wire, url, target):
    """Reject partial/foreign setup evidence without echoing any child bytes."""
    try:
        if not isinstance(wire, str) or len(wire.encode('utf-8')) > MAX_RECEIPT_BYTES:
            raise ValueError()
        value = json.loads(wire, object_pairs_hook=unique_object)
        if (type(value) is not dict or set(value) != {'schema_version', 'database', 'owner', 'target', 'first', 'second'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['database'] != url.database or value['owner'] != url.username
            or target not in TARGETS or value['target'] != target):
            raise ValueError()
        for name in ('first', 'second'):
            rows = value[name]
            if type(rows) is not list or not 1 <= len(rows) <= 4096: raise ValueError()
            identities = set()
            for row in rows:
                if (type(row) is not list or len(row) != 4 or row[0] not in {'gateway', 'public'}
                    or not isinstance(row[1], str) or not re.fullmatch('[a-z_][a-z0-9_]{0,62}', row[1])
                    or type(row[2]) is not int or not 1 <= row[2] < 2**32
                    or type(row[3]) is not int or not 0 <= row[3] < 2**32):
                    raise ValueError()
                identity = tuple(row[:2])
                if identity in identities or row[0] == 'public' and row[1] != 'alembic_version': raise ValueError()
                identities.add(identity)
            if ('public', 'alembic_version') not in identities or not any(row[0] == 'gateway' for row in rows):
                raise ValueError()
        if value['first'] != value['second']: raise ValueError()
        return value
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise ValueError('Invalid paired fixture migration evidence') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', choices=sorted(TARGETS))
    args = parser.parse_args(argv)
    try:
        value = collect_pair(args.target)
        wire = json.dumps(value, sort_keys=True)
        parse_receipt(wire, fixture_url(args.target), args.target)
    except Exception:
        print('Disposable owner migration pair failed', file=sys.stderr)
        return 1
    print(wire)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

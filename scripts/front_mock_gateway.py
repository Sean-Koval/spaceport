"""Read-only mock of the gateway's /v1 API for the README capture.

It serves the repository's own fixture product (``fixtures/products/revenue.json``
and ``revenue.tests.json``) as one saved draft whose fixture tests have passed, so
the publisher's review screen renders without a database, Cube or credentials.
``scripts/capture_front.py`` starts it; nothing else should. It accepts any bearer
and writes nothing.

Accounts: the bearer ``demo-publisher`` may publish; any other bearer is an author
who may edit and test but not publish. Writes (POST, PUT) return 405.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
DRAFT_ID = 'd-7f3a'
RUN_ID = 'r-1'
RECORDED_AT = '2026-10-15T10:00:00Z'


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def build() -> dict[str, object]:
    definition = json.loads((ROOT / 'fixtures/products/revenue.json').read_text())
    tests = json.loads((ROOT / 'fixtures/products/revenue.tests.json').read_text())
    published = {**definition, 'description': 'Daily sum of order amounts.'}
    before, after = digest(published), digest(definition)
    checks = []
    for case in tests['cases']:
        fixture, kind = case['fixture'], case['kind']
        check = {'fixture_id': fixture['id'], 'kind': kind, 'passed': True}
        if kind == 'known_answer':
            check.update(
                expected=fixture['expected_rows'],
                actual=fixture['expected_rows'],
                absolute_tolerance=fixture['tolerance'],
                relative_tolerance=case['relative_tolerance'],
                reference_watermark=fixture['source_watermark'],
                observed_source_load_watermark=fixture['source_watermark'],
                freshness_status='fresh',
            )
        else:
            check.update(expected_error=fixture['expected_error'], actual_error=fixture['expected_error'])
        checks.append(check)
    binding = tests['fixture_binding']
    return {
        'definition': definition,
        'draft': {
            'id': DRAFT_ID,
            'product_id': definition['product_id'],
            'revision': 3,
            'digest': after,
            'definition': definition,
            'editable_full_definition': True,
            'current_draft_id': DRAFT_ID,
            'is_current_revision': True,
            'can_test': True,
            'can_publish': False,
        },
        'diff': {
            'draft_id': DRAFT_ID,
            'product_id': definition['product_id'],
            'revision': 3,
            'latest_test_run_id': RUN_ID,
            'before_digest': before,
            'after_digest': after,
            'changes': [
                {
                    'path': '/description',
                    'kind': 'changed',
                    'before': published['description'],
                    'after': definition['description'],
                }
            ],
            'stale_evidence': [],
        },
        'run': {
            'id': RUN_ID,
            'draft_id': DRAFT_ID,
            'draft_digest': after,
            'binding_digest': binding['model_bundle_digest'],
            'policy_id': definition['policy_id'],
            'policy_revision': 2,
            'fixture_version': '1',
            'status': 'passed',
            'created_at': RECORDED_AT,
            'checks': checks,
        },
        'draft_summary': {
            'id': DRAFT_ID,
            'product_id': definition['product_id'],
            'version': definition['version'],
            'name': definition['name'],
            'revision': 3,
            'editable_full_definition': True,
            'can_test': True,
            'can_publish': False,
        },
    }


class Handler(BaseHTTPRequestHandler):
    state = build()

    def log_message(self, *_: object) -> None:
        pass

    def reply(self, body: object, status: int = 200) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def publisher(self) -> bool:
        return self.headers.get('Authorization', '') == 'Bearer demo-publisher'

    def route(self, path: str) -> object | None:
        state = self.state
        if path == '/v1/drafts':
            return {'items': [{**state['draft_summary'], 'can_publish': self.publisher()}], 'next_cursor': None}
        if path == f'/v1/drafts/{DRAFT_ID}':
            return {**state['draft'], 'can_publish': self.publisher()}
        if path == f'/v1/drafts/{DRAFT_ID}/diff':
            return state['diff']
        if re.fullmatch(rf'/v1/drafts/{DRAFT_ID}/tests/[A-Za-z0-9_.-]+', path):
            return state['run']
        if path in ('/v1/products', '/v1/bindings', '/v1/policies'):
            return {'items': [], 'next_cursor': None}
        return None

    def do_GET(self) -> None:
        if not self.headers.get('Authorization', '').startswith('Bearer '):
            self.reply({'code': 'UNAUTHENTICATED'}, 401)
            return
        body = self.route(urlparse(self.path).path)
        if body is None:
            self.reply({'code': 'NOT_FOUND_OR_FORBIDDEN'}, 404)
        else:
            self.reply(body)

    def do_POST(self) -> None:
        self.reply({'code': 'UPSTREAM_UNAVAILABLE'}, 405)

    do_PUT = do_POST


def main(argv: list[str]) -> None:
    port = int(argv[1]) if len(argv) > 1 else 8000
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()


if __name__ == '__main__':
    main(sys.argv)

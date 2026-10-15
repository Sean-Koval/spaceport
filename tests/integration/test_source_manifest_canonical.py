"""Repository content storage only; no serving approval or synthetic exemption."""
import asyncio
import hashlib
import json

import pytest

from gateway.contracts.canonical import canonical_bytes
from tests.integration.test_registry import publish, seed


@pytest.mark.postgres
@pytest.mark.parametrize('number', [1e-7, -0.0, 1.25])
def test_new_version_retains_exact_canonical_manifest(registry_database, valid_manifest, number):
    valid_manifest['inputs']['ratio'] = {'type': 'number', 'required': False, 'default': {'value': number}}
    valid_manifest['redaction']['ratio'] = 'omit'

    async def scenario():
        manifest, draft = await seed(registry_database, valid_manifest)
        row = await publish(registry_database.registry, draft)
        expected = canonical_bytes(manifest.model_dump(mode='python', exclude={'content_digest'}))
        assert getattr(row, 'manifest_canonical', None) == expected.decode('utf-8')
        stored = registry_database.scalar('SELECT manifest_canonical FROM gateway.product_versions WHERE id=%s', (row.id,))
        assert stored.encode('utf-8') == expected
        assert 'content_digest' not in json.loads(stored)
        assert hashlib.sha256(stored.encode('utf-8')).hexdigest() == row.content_digest

    asyncio.run(scenario())

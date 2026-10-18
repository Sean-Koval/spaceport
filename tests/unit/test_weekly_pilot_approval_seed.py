"""The test-owned approval seed cannot silently approve a changed definition."""
from dataclasses import replace

import pytest

from gateway.contracts.manifest import validate_manifest
from tests.integration.source_pilot_approval_seed import weekly_approval
from tests.unit.test_source_approval_tooling import configured_assets

pytestmark = pytest.mark.unit


def test_weekly_owner_seed_retains_exact_fixed_clients_and_roster(tmp_path):
    _, assets = configured_assets(tmp_path)
    document = weekly_approval(assets.packages.packages[0])
    assert len(document.audiences) == 9
    for audience in document.audiences:
        if audience.principal_kind == 'demo':
            assert audience.client_ids == (None,)
            assert audience.fixture_case_ids and audience.context == 'fixture_test'
        else:
            assert audience.client_ids == ('reviewed-client',)


@pytest.mark.parametrize('field,value', [('description','unreviewed metadata'),('product_id','different-product')])
def test_weekly_owner_seed_requires_explicit_review_of_changed_sample(tmp_path, field, value):
    _, assets = configured_assets(tmp_path)
    retained = assets.packages.packages[0]
    raw = retained.package.manifest.model_dump(mode='json', exclude={'content_digest'})
    manifest = validate_manifest(raw | {field:value})
    changed = replace(retained, package=retained.package.model_copy(update={'manifest':manifest}))
    with pytest.raises(ValueError):
        weekly_approval(changed)


def test_weekly_owner_seed_rejects_changed_case_tolerance_with_same_roster(tmp_path):
    from gateway.contracts.canonical import json_value
    _, assets = configured_assets(tmp_path)
    retained = assets.packages.packages[0]
    catalog = json_value(retained.assets['fixture_catalog_ref'])
    catalog['cases'][0]['relative_tolerance'] = '0.5'
    changed = replace(retained, assets=dict(retained.assets) | {'fixture_catalog_ref':catalog})
    with pytest.raises(ValueError):
        weekly_approval(changed)

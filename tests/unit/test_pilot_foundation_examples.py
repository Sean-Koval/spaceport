"""Cross-slice examples must satisfy the real config and identity contracts."""
import json
from pathlib import Path

import pytest

from gateway.auth.principals import oidc_subject_key
from gateway.pilot_bootstrap import load_identity_mapping
from gateway.pilot_config import PilotRuntimeConfig

pytestmark = [pytest.mark.unit, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[2]


def test_gke_examples_match_real_pilot_contracts_without_connections():
    config = PilotRuntimeConfig.model_validate(json.loads(
        (ROOT / 'infra/gke/pilot.example.json').read_text()))
    mapping = load_identity_mapping(ROOT / 'infra/gke/identities.example.json', config)
    assert config.identity_settings().mode == 'production'
    assert config.identity_settings().source == 'synthetic_postgres'
    assert config.identity_settings().credential_hash_file is None
    assert config.oidc.audience == config.mcp_resource_url
    assert config.registry.ca_file == '/run/pilot/config/registry-ca.pem'
    assert config.probe_principal_id in {principal.id for principal in mapping.principals}
    assert {principal.kind for principal in mapping.principals} == {'user'}
    assert len({principal.subject for principal in mapping.principals}) == len(mapping.principals)
    for principal in mapping.principals:
        assert principal.subject.startswith('oidc-v1-')
        assert len(principal.grants) == 1
        assert not {'author', 'publisher'} <= set(principal.grants[0].roles)


def test_example_subjects_are_bound_to_the_configured_issuer():
    config = PilotRuntimeConfig.model_validate(json.loads(
        (ROOT / 'infra/gke/pilot.example.json').read_text()))
    path = ROOT / 'infra/gke/identities.example.json'
    raw = json.loads(path.read_text())
    mapped = load_identity_mapping(path, config)
    expected = {row['id']: oidc_subject_key(config.oidc.issuer, row['subject'])
                for row in raw['principals']}
    assert {row.id: row.subject for row in mapped.principals} == expected
    other = config.model_dump(mode='json')
    other['oidc']['issuer'] = 'https://different-issuer.example.com'
    remapped = load_identity_mapping(path, PilotRuntimeConfig.model_validate(other))
    assert remapped.digest != mapped.digest
    assert {row.subject for row in remapped.principals}.isdisjoint(expected.values())

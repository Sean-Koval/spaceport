"""Strict RFC3339 offsets must not be normalized from invalid spellings."""
from datetime import datetime, timezone

import pytest

from gateway.contracts.canonical import timestamp

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('offset', ['+00:60', '+00:99', '-00:60', '-01:99', '+23:60', '+24:00', '-24:00'])
def test_timestamp_rejects_out_of_range_offset_components(offset):
    with pytest.raises(ValueError):
        timestamp('2024-03-12T12:00:00' + offset)


@pytest.mark.parametrize(('value', 'expected'), [
    ('2024-03-12T12:00:00Z', '2024-03-12T12:00:00+00:00'),
    ('2024-03-12T12:00:00+00:59', '2024-03-12T11:01:00+00:00'),
    ('2024-03-12T12:00:00-00:59', '2024-03-12T12:59:00+00:00'),
    ('2024-03-12T12:00:00+23:59', '2024-03-11T12:01:00+00:00'),
    ('2024-03-12T12:00:00-23:59', '2024-03-13T11:59:00+00:00'),
])
def test_timestamp_preserves_valid_boundary_offsets(value, expected):
    assert timestamp(value) == datetime.fromisoformat(expected)
    assert timestamp(value).tzinfo is timezone.utc

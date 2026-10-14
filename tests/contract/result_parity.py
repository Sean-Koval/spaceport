"""Shared assertions for REST/MCP pages of one pinned successful execution."""
from copy import deepcopy
from datetime import datetime


def assert_result_page_parity(mcp_page, rest_page, expected):
    assessments = []
    normalized = []
    for page in (mcp_page, rest_page):
        assert page['state'] == 'succeeded'
        for field in ('execution_id', 'product_id', 'version', 'product_digest',
                      'source_watermark', 'row_count', 'truncated'):
            assert page[field] == expected[field], field
        freshness = page['freshness']
        assert freshness['status'] == 'fresh'
        assert freshness['max_age_seconds'] == expected['freshness']['max_age_seconds']
        assert 0 <= freshness['age_seconds'] <= freshness['max_age_seconds']
        assessed = datetime.fromisoformat(freshness['assessed_at'])
        source = datetime.fromisoformat(page['source_watermark'])
        assert freshness['age_seconds'] == (assessed - source).total_seconds()
        assessments.append(assessed)
        value = deepcopy(page)
        value.pop('next_cursor', None)
        # These are observations of the fetch, not immutable result metadata.
        del value['freshness']['assessed_at']
        del value['freshness']['age_seconds']
        normalized.append(value)
    assert assessments[1] >= assessments[0]
    assert normalized[0] == normalized[1]

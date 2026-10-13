"""Synthetic, deterministic fixtures shared by gateway tests."""
from datetime import datetime, timedelta, timezone

import pytest


class FakeClock:
    def __init__(self, instant: datetime):
        self.instant = instant
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.instant

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.instant += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return FakeClock(datetime(2024, 3, 12, 1, tzinfo=timezone.utc))


@pytest.fixture
def north_principal():
    from gateway.contracts.identity import Principal

    return Principal(id="north-user", subject="synthetic-north", service_client_id=None, kind="demo")


@pytest.fixture
def valid_manifest():
    return {
        "schema_version": "1.0",
        "product_id": "revenue",
        "version": "1.0.0",
        "name": "Daily revenue",
        "description": "Daily sum of order amounts, including unassigned segments.",
        "examples": [{"description": "Three-day north revenue", "inputs": {"start": "2024-03-09", "end": "2024-03-12"}}],
        "owner": "analytics",
        "domain": "finance",
        "links": [{"label": "Definition", "url": "https://example.com/revenue"}],
        "inputs": {
            "start": {"type": "date"},
            "end": {"type": "date"},
            "segment": {"type": "string", "nullable": True, "required": False, "enum": ["A", "B"], "default": {"value": None}},
        },
        "outputs": {
            "day": {"type": "date"},
            "segment": {"type": "string", "nullable": True},
            "revenue": {"type": "decimal", "precision": 38, "scale": 2},
        },
        "filters": {"segment": {"operators": ["eq", "in", "is_null"], "max_values": 10}, "day": {"operators": ["eq", "gte", "lt"]}},
        "sort": {"fields": ["day", "segment"], "max_fields": 2},
        "grain": "daily",
        "units": {"revenue": {"unit": "money", "currency": "USD"}},
        "timezone": "America/New_York",
        "date_boundaries": {"start_input": "start", "end_input": "end", "interval": "[start,end)"},
        "aggregation": "Sum of order amounts",
        "freshness": {"required": True, "max_age_seconds": 86400, "meaning": "source_load"},
        "binding_id": "revenue-orders",
        "binding_revision": 1,
        "adapter_revision": "cube-rest-v1",
        "policy_id": "revenue-access",
        "classification": "internal",
        "redaction": {"start": "digest", "end": "digest", "segment": "omit"},
        "fixtures": [{"id": "north-total", "version": "1", "inputs": {"start": "2024-03-09", "end": "2024-03-12"}, "expected_rows": [{"day": "2024-03-09", "segment": "A", "revenue": "10.10"}, {"day": "2024-03-10", "segment": None, "revenue": "20.20"}, {"day": "2024-03-11", "segment": "A", "revenue": "9007199254740993.01"}], "tolerance": "0.00", "source_watermark": "2024-03-12T00:00:00Z"}],
        "content_digest": None,
        "model_bundle_digest": "a" * 64,
    }

"""Bounded external query shape and manifest-aware, pure input validation."""
from typing import Annotated, Literal

from pydantic import Field, ValidationError, field_validator

from .canonical import Array, Identifier, Scalar, StrictModel, Version, bounded_json, sequence
from .errors import GatewayError, invalid_input
from .manifest import Manifest, Operator, iso_date, validate_value


class QueryFilter(StrictModel):
    field: Identifier
    operator: Operator
    value: Scalar | Array[Scalar] = None

    @field_validator("value", mode="before")
    @classmethod
    def bounded_value(cls, value):
        bounded_json(value, max_nodes=101)
        if isinstance(value, (tuple, list)): return sequence(value)
        return value


class QuerySort(StrictModel):
    field: Identifier
    direction: Literal["asc", "desc"] = "asc"


class QueryRequest(StrictModel):
    product_id: Identifier
    version: Version | None = None
    inputs: Annotated[dict[Identifier, Scalar], Field(max_length=50)] = Field(default_factory=dict)
    filters: Array[QueryFilter] = Field(default=(), max_length=20)
    sort: Array[QuerySort] = Field(default=(), max_length=5)
    idempotency_key: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")] | None = None

    @field_validator("inputs", mode="before")
    @classmethod
    def bounded_inputs(cls, value): return bounded_json(value)


def validate_query(manifest: Manifest, request: QueryRequest) -> QueryRequest:
    try:
        # Revalidate copied/constructed instances as well as ordinary callers.
        request = QueryRequest.model_validate(request.model_dump(mode="python"))
        if request.product_id != manifest.product_id or (request.version is not None and request.version != manifest.version):
            raise ValueError("Query does not identify this pinned product")
        if set(request.inputs) - set(manifest.inputs): raise ValueError("Unknown product input")
        inputs = dict(request.inputs)
        for name, schema in manifest.inputs.items():
            if name not in inputs:
                if schema.default is not None: inputs[name] = schema.default.value
                elif schema.required: raise ValueError("Required input is missing")
                else: continue
            validate_value(schema, inputs[name])
        if manifest.date_boundaries is not None:
            start = iso_date(inputs[manifest.date_boundaries.start_input])
            end = iso_date(inputs[manifest.date_boundaries.end_input])
            if not 0 < (end - start).days <= manifest.limits.max_date_range_days:
                raise ValueError("Date range exceeds the permitted local calendar days")
        for item in request.filters:
            definition = manifest.filters.get(item.field)
            if definition is None or item.operator not in definition.operators:
                raise ValueError("Filter is not approved by the product")
            schema = manifest.outputs[item.field]
            if item.operator == "is_null":
                if item.value is not None: raise ValueError("is_null accepts no value")
            elif item.operator in ("in", "not_in"):
                if not isinstance(item.value, tuple) or not 1 <= len(item.value) <= definition.max_values:
                    raise ValueError("Filter collection exceeds its bounds")
                for value in item.value: validate_value(schema, value)
            else:
                if isinstance(item.value, tuple): raise ValueError("Scalar filter requires a scalar")
                validate_value(schema, item.value)
        if len(request.sort) > manifest.sort.max_fields or any(item.field not in manifest.sort.fields for item in request.sort):
            raise ValueError("Sort is not approved by the product")
        if len({item.field for item in request.sort}) != len(request.sort):
            raise ValueError("Duplicate sort fields")
        return QueryRequest(**{**request.model_dump(mode="python"), "inputs": inputs})
    except (ValidationError, ValueError, TypeError, KeyError, GatewayError):
        raise invalid_input("The product query is invalid.") from None

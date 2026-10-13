"""Verified principals and internal, server-derived authorization decisions."""
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .canonical import Array, Identifier, Scalar, StrictModel, bounded_json
from .manifest import Limits


class Principal(StrictModel):
    id: Identifier
    subject: Annotated[str, Field(min_length=1, max_length=512)]
    service_client_id: Identifier | None
    kind: Literal["user", "service", "demo"]


class RowConstraint(StrictModel):
    member: Identifier
    operator: Literal["eq", "in"]
    values: Array[Scalar] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def scalar_constraints(self):
        bounded_json(self.values)
        if self.operator == "eq" and len(self.values) != 1:
            raise ValueError("An equality row constraint has exactly one value")
        if any(value is None for value in self.values):
            raise ValueError("Mandatory constraints cannot be null")
        return self


class Decision(StrictModel):
    """Internal only. API/MCP handlers never accept this model from a client."""
    principal_id: Identifier
    policy_revision: int = Field(ge=1)
    tenant_id: Identifier
    row_constraints: Array[RowConstraint] = Field(min_length=1, max_length=20)
    visible_fields: Array[Identifier] = Field(min_length=1, max_length=50)
    limits: Limits

    @model_validator(mode="after")
    def unique_fields(self):
        if len(set(self.visible_fields)) != len(self.visible_fields):
            raise ValueError("Visible fields must be distinct")
        return self

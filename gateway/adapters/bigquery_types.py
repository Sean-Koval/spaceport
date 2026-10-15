"""Pure identifier grammar shared by offline and serving BigQuery profiles."""
from typing import Annotated
from pydantic import Field

ProjectId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")]
DatasetId = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")]
Location = Annotated[str, Field(pattern=r"^(?:US|EU|[a-z]+(?:-[a-z]+)+[1-9][0-9]?)$", max_length=64)]

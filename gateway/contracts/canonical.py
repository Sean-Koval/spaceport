"""RFC 8785 canonical bytes, lossless values and immutable strict-model helpers."""
from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import math
from typing import Annotated, Any, TypeVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator
import rfc8785

from .errors import invalid_input

MAX_RESULT_BYTES = 1_048_576
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Version = Annotated[str, Field(min_length=5, max_length=64, pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[A-Za-z0-9.-]+)?$")]
T = TypeVar("T")


def sequence(value: Any) -> tuple:
    if not isinstance(value, (list, tuple)):
        raise ValueError("Expected a JSON array")
    return tuple(value)


Array = Annotated[tuple[T, ...], BeforeValidator(sequence)]
Scalar = str | int | float | bool | None


class FrozenDict(dict):
    """JSON-serializable map that prevents ordinary in-place modification."""

    __slots__ = ("_initialized",)

    def __init__(self, *args, **kwargs):
        if hasattr(self, "_initialized"):
            raise TypeError("Validated contracts are immutable")
        dict.__init__(self, *args, **kwargs)
        object.__setattr__(self, "_initialized", True)

    def _immutable(self, *args, **kwargs):
        raise TypeError("Validated contracts are immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable
    __setattr__ = __delattr__ = _immutable

    def __deepcopy__(self, memo):
        return self


def freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return FrozenDict({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, validate_default=True, revalidate_instances="always", allow_inf_nan=False)

    @model_validator(mode="after")
    def freeze_collections(self):
        for field in type(self).model_fields:
            object.__setattr__(self, field, freeze(getattr(self, field)))
        return self


def timestamp(value: Any) -> datetime:
    if isinstance(value, str):
        # ISO date strings are deliberately not accepted as timestamps.
        import re
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value):
            raise ValueError("Expected an explicit RFC 3339 timestamp")
        # datetime.fromisoformat normalizes malformed minute offsets such as
        # +00:60 instead of rejecting them. Validate RFC3339 components first.
        if value[-1] != "Z" and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
            raise ValueError("Timestamp offset is outside RFC 3339 bounds")
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamps must have an explicit timezone")
    try:
        return value.astimezone(timezone.utc)
    except OverflowError:
        raise ValueError("Timestamp is outside the representable UTC range") from None


AwareTimestamp = Annotated[datetime, BeforeValidator(timestamp)]


def json_value(value: Any) -> Any:
    """Convert domain values before JCS; never coerce unknown Python objects."""
    if isinstance(value, BaseModel):
        return json_value(value.model_dump(mode="python"))
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Nonfinite decimal")
        return format(value, "f")
    if isinstance(value, datetime):
        return timestamp(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError("JSON object keys must be strings")
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError("Unsupported JSON value")


def canonical_bytes(value: Any) -> bytes:
    try:
        return rfc8785.dumps(json_value(value))
    except (rfc8785.CanonicalizationError, ValueError, TypeError, RecursionError, UnicodeError):
        raise invalid_input("The value cannot be represented as canonical JSON.") from None


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def manifest_digest(manifest: BaseModel) -> str:
    return canonical_digest(manifest.model_dump(mode="python", exclude={"content_digest"}))


def bounded_json(value: Any, *, max_depth: int = 8, max_nodes: int = 4096, max_string: int = 4096) -> Any:
    """Bound externally supplied JSON collections before domain interpretation."""
    nodes = 0

    def check(item, depth):
        nonlocal nodes
        nodes += 1
        if depth > max_depth or nodes > max_nodes:
            raise ValueError("JSON collection is too large or deeply nested")
        if isinstance(item, dict):
            if any(type(key) is not str or len(key) > 128 for key in item):
                raise ValueError("Invalid JSON object key")
            for child in item.values(): check(child, depth + 1)
        elif isinstance(item, (tuple, list)):
            for child in item: check(child, depth + 1)
        elif type(item) is str:
            if len(item) > max_string:
                raise ValueError("JSON string is too long")
        elif item is not None and type(item) not in (bool, int, float):
            raise ValueError("Expected a JSON value")
        elif type(item) is float and not math.isfinite(item):
            raise ValueError("Nonfinite JSON number")
        elif type(item) is int and abs(item) > 2**53 - 1:
            raise ValueError("Large exact numbers must be decimal strings")

    check(value, 0)
    return value

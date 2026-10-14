"""Stable errors shared by domain services and redacting transports."""
from enum import StrEnum
from uuid import uuid4


class ErrorCode(StrEnum):
    UNAUTHENTICATED = "UNAUTHENTICATED"
    NOT_FOUND_OR_FORBIDDEN = "NOT_FOUND_OR_FORBIDDEN"
    INVALID_INPUT = "INVALID_INPUT"
    BINDING_DRIFT = "BINDING_DRIFT"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    EXECUTION_TIMEOUT = "EXECUTION_TIMEOUT"
    FRESHNESS_UNKNOWN = "FRESHNESS_UNKNOWN"
    STALE_DATA = "STALE_DATA"
    CONFLICT = "CONFLICT"


class GatewayError(Exception):
    """Only an approved explanation, never upstream exception text, is public."""

    def __init__(self, code: ErrorCode | str, request_id: str, safe_message: str):
        self.code = ErrorCode(code)
        if not request_id or not safe_message:
            raise ValueError("A request ID and safe explanation are required")
        self.request_id = request_id
        self.safe_message = safe_message
        super().__init__(safe_message)

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code.value, "request_id": self.request_id, "safe_message": self.safe_message}


def invalid_input(message: str = "The product or query contract is invalid.") -> GatewayError:
    return GatewayError(ErrorCode.INVALID_INPUT, str(uuid4()), message)

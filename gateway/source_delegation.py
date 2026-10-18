"""Bounded, no-I/O presence checks for unsupported delegation assertions.

These predicates do not authenticate a token or create identity or authority.
They are intended for the reviewed token profile after signature validation;
early rejection is safe, but a negative result never permits early acceptance.
An ordinary user bearer token does not establish the absence of an unseen agent.
Unrelated spoofable identity headers remain outside these checks.
"""
from collections.abc import Mapping, Sequence


_MAX_ENTRIES = 256
_MAX_CLAIM_NAME_CHARACTERS = 256
_MAX_HEADER_NAME_BYTES = 128
_MAX_HEADER_BYTES = 65_536
_HEADER_TOKEN_BYTES = b"!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_DELEGATION_CLAIMS = ("act", "may_act")
_DELEGATION_HEADERS = (b"on-behalf-of", b"x-on-behalf-of")


def has_unsupported_delegation_claims(claims: Mapping[str, object]) -> bool:
    """Report exact top-level ``act``/``may_act`` presence, without reading values.

    The runtime profile requires an exact dict with at most 256 keys, each an
    exact str of 1..256 characters. Custom mappings and subclasses are rejected
    without invoking their hooks. The full shape is validated before returning;
    malformed input raises a fixed ValueError that never includes input values.
    """
    try:
        if type(claims) is not dict or len(claims) > _MAX_ENTRIES:
            raise ValueError
        found = False
        for key in claims:
            if type(key) is not str or not 1 <= len(key) <= _MAX_CLAIM_NAME_CHARACTERS:
                raise ValueError
            found = found or key in _DELEGATION_CLAIMS
        return found
    except Exception:
        raise ValueError("invalid delegation claims") from None


def has_unsupported_delegation_headers(raw_headers: Sequence[tuple[bytes, bytes]]) -> bool:
    """Report designated raw header-name presence, ignoring case and values.

    Require an exact list/tuple of at most 256 exact tuple pairs of exact bytes.
    Names must be 1..128 ASCII HTTP-token bytes; combined name/value lengths must
    not exceed 65,536 bytes. Values are never decoded, inspected, or copied; only
    their lengths contribute to the bound. Duplicate names are retained. The
    full shape is validated before returning, with fixed safe ValueError errors.
    """
    try:
        if (type(raw_headers) is not list and type(raw_headers) is not tuple
                or len(raw_headers) > _MAX_ENTRIES):
            raise ValueError
        found = False
        total_bytes = 0
        for pair in raw_headers:
            if type(pair) is not tuple or len(pair) != 2:
                raise ValueError
            name, value = pair
            if type(name) is not bytes or type(value) is not bytes:
                raise ValueError
            if not 1 <= len(name) <= _MAX_HEADER_NAME_BYTES:
                raise ValueError
            if any(character not in _HEADER_TOKEN_BYTES for character in name):
                raise ValueError
            total_bytes += len(name) + len(value)
            if total_bytes > _MAX_HEADER_BYTES:
                raise ValueError
            found = found or name.lower() in _DELEGATION_HEADERS
        return found
    except Exception:
        raise ValueError("invalid delegation headers") from None

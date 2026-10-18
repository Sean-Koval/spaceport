"""Pure presence checks never authenticate, inspect values, or acquire authority."""
from collections import UserDict, UserList
from collections.abc import Mapping, Sequence
import builtins
import importlib
import io
import os
import socket
import subprocess
import traceback
from types import MappingProxyType

import pytest


pytestmark = [pytest.mark.unit, pytest.mark.contract]


def claims_present(claims):
    from gateway.source_delegation import has_unsupported_delegation_claims

    return has_unsupported_delegation_claims(claims)


def headers_present(raw_headers):
    from gateway.source_delegation import has_unsupported_delegation_headers

    return has_unsupported_delegation_headers(raw_headers)


class OpaqueValue:
    def __repr__(self):
        raise AssertionError("claim value repr must not run")

    def __str__(self):
        raise AssertionError("claim value str must not run")

    def __bool__(self):
        raise AssertionError("claim value truthiness must not run")


class StringSubclass(str):
    pass


class BytesSubclass(bytes):
    pass


class BrokenMapping(Mapping):
    def __iter__(self):
        raise RuntimeError("PRIVATE_ITERATOR_VALUE")

    def __getitem__(self, key):
        raise AssertionError("claim values must not be read")

    def __len__(self):
        return 1


class KeysOnlyMapping(Mapping):
    def __iter__(self):
        return iter(("sub", "act"))

    def __getitem__(self, key):
        raise AssertionError("claim values must not be read")

    def __contains__(self, key):
        raise AssertionError("mapping membership must not override key presence")

    def __len__(self):
        return 2


class BrokenSequence(Sequence):
    def __getitem__(self, index):
        raise RuntimeError("PRIVATE_ITERATOR_VALUE")

    def __len__(self):
        return 1


class BrokenAfterMatchMapping(KeysOnlyMapping):
    def __iter__(self):
        yield "act"
        raise RuntimeError("PRIVATE_ITERATOR_VALUE")


class BrokenAfterMatchSequence(Sequence):
    def __getitem__(self, index):
        if index == 0:
            return (b"On-Behalf-Of", b"")
        raise RuntimeError("PRIVATE_ITERATOR_VALUE")

    def __len__(self):
        return 2


@pytest.mark.parametrize("key", ["act", "may_act"])
@pytest.mark.parametrize("value", [None, False, "", 0, 0.0, {}, [], OpaqueValue()],
                         ids=["null", "false", "empty-text", "zero", "zero-float",
                              "empty-object", "empty-array", "opaque"])
def test_claim_presence_rejects_even_false_empty_or_uninspectable_values(key, value):
    assert claims_present({"sub": "synthetic-user", key: value}) is True


@pytest.mark.parametrize("claims", [
    {}, {"sub": "synthetic-user", "aud": "synthetic-client"},
    {"ACT": None, "May_Act": False}, {"act ": None, " may_act": None},
    {"actor": None, "on_behalf_of": None},
    {"nested": {"act": None}}, {"subject": OpaqueValue()},
], ids=["empty", "ordinary", "case-sensitive", "no-trimming", "other-names",
        "no-recursion", "opaque-unrelated-value"])
def test_only_exact_top_level_signed_claim_names_are_delegation_assertions(claims):
    assert claims_present(claims) is False


@pytest.mark.parametrize("factory", [UserDict, MappingProxyType])
def test_custom_claim_mappings_are_rejected(factory):
    with pytest.raises(ValueError, match="^invalid delegation claims$"):
        claims_present(factory({"may_act": None}))


def test_claim_mapping_is_not_mutated():
    value = OpaqueValue()
    claims = {"sub": value, "may_act": value}
    assert claims_present(claims) is True
    assert list(claims) == ["sub", "may_act"]
    assert claims["sub"] is value
    assert claims["may_act"] is value


def test_claim_count_and_name_length_upper_bounds_are_inclusive():
    claims = {str(n): OpaqueValue() for n in range(255)}
    claims["k" * 256] = OpaqueValue()
    assert claims_present(claims) is False
    claims.pop("0")
    claims["act"] = OpaqueValue()
    assert claims_present(claims) is True


def test_unicode_claim_names_are_not_normalized_or_case_folded():
    assert claims_present({"\u00e9" * 256: OpaqueValue(), "\uff41\uff43\uff54": None}) is False


@pytest.mark.parametrize("claims", [
    None, False, 0, "", "PRIVATE_INPUT_VALUE", b"", [], (), object(),
    {b"act": None}, {1: None}, {StringSubclass("act"): None}, {"": None},
    {"k" * 257: None}, {str(n): None for n in range(257)}, KeysOnlyMapping(),
    {"act": None, 1: "PRIVATE_INPUT_VALUE"}, BrokenMapping(), BrokenAfterMatchMapping(),
], ids=["null", "boolean", "integer", "empty-text", "text", "bytes", "list",
        "tuple", "object", "bytes-key", "integer-key", "string-subclass-key",
        "empty-key", "long-key", "many-keys", "keys-only-mapping",
        "invalid-key-after-match", "broken-mapping", "broken-after-match"])
def test_invalid_claim_inputs_fail_closed_without_value_leaks(claims):
    with pytest.raises(ValueError) as caught:
        claims_present(claims)
    assert caught.value.args == ("invalid delegation claims",)
    assert caught.value.__suppress_context__ is True
    assert "PRIVATE_" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("name", [
    b"on-behalf-of", b"On-Behalf-Of", b"ON-BEHALF-OF", b"oN-bEhAlF-oF",
    b"x-on-behalf-of", b"X-On-Behalf-Of", b"X-ON-BEHALF-OF", b"x-On-bEhAlF-oF",
])
@pytest.mark.parametrize("value", [b"", b"false", b"null", b"\x00\r\n\xffPRIVATE_VALUE"])
def test_designated_header_presence_is_ascii_case_insensitive_and_value_opaque(name, value):
    assert headers_present([(name, value)]) is True


@pytest.mark.parametrize("raw_headers", [
    [(b"on-behalf-of", b""), (b"On-Behalf-Of", b"synthetic-other")],
    [(b"ordinary", b""), (b"X-On-Behalf-Of", b""), (b"x-on-behalf-of", b"false")],
    [(b"on-behalf-of", b""), (b"x-on-behalf-of", b"")],
])
def test_duplicate_and_mixed_case_headers_cannot_hide_designated_presence(raw_headers):
    original = list(raw_headers)
    assert headers_present(raw_headers) is True
    assert raw_headers == original


@pytest.mark.parametrize("factory", [list, tuple])
def test_raw_header_sequences_are_supported(factory):
    assert headers_present(factory([(b"On-Behalf-Of", b"")])) is True
    assert headers_present(factory([])) is False


def test_header_count_and_name_length_upper_bounds_are_inclusive():
    raw_headers = [(b"a" * 128, b"")] * 256
    assert headers_present(raw_headers) is False
    raw_headers[-1] = (b"On-Behalf-Of", b"")
    assert headers_present(raw_headers) is True


def test_header_total_byte_bound_includes_names_and_values_and_is_inclusive():
    raw_headers = [(b"a", b"\xff" * 32_767), (b"b", b"\x00" * 32_767)]
    assert headers_present(raw_headers) is False
    raw_headers[-1] = (b"b", b"\x00" * 32_768)
    with pytest.raises(ValueError, match="^invalid delegation headers$"):
        headers_present(raw_headers)


def test_oversized_header_values_deny_even_after_a_designated_header():
    with pytest.raises(ValueError, match="^invalid delegation headers$"):
        headers_present([(b"On-Behalf-Of", b""), (b"ordinary", b"x" * 65_536)])


def test_unrelated_legacy_spoof_headers_remain_untrusted_and_ignored():
    raw_headers = [
        (b"Authorization", b"Bearer ordinary-valid-token-is-not-an-agent-detector"),
        (b"X-User-Id", b"synthetic-other"), (b"X-Principal-Id", b"synthetic-other"),
        (b"X-Tenant-Id", b"synthetic-other"), (b"X-Groups", b"synthetic-admin"),
        (b"X-Client-Id", b"synthetic-other"), (b"X-Actor", b"synthetic-other"),
        (b"On_Behalf_Of", b"synthetic-other"), (b"X-On-Behalf-Of-Extra", b""),
        (b"ordinary", b"On-Behalf-Of: synthetic-other"),
        (b"ordinary", b"X-On-Behalf-Of: synthetic-other"),
    ]
    original = list(raw_headers)
    assert headers_present(raw_headers) is False
    assert raw_headers == original


def test_all_ascii_http_token_name_characters_are_valid_without_broad_matching():
    assert headers_present([(b"!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz", b"")]) is False


@pytest.mark.parametrize("raw_headers", [
    None, False, 0, "", "PRIVATE_INPUT_VALUE", b"", bytearray(), memoryview(b""),
    {}, set(), iter(()), object(), UserList([]), [(b"a", b"")] * 257, [(b"ordinary",)], [(b"ordinary", b"", b"")],
    [[b"ordinary", b""]], [("On-Behalf-Of", b"")], [(b"On-Behalf-Of", "PRIVATE_VALUE")],
    [(bytearray(b"On-Behalf-Of"), b"")], [(b"On-Behalf-Of", bytearray())],
    [(BytesSubclass(b"On-Behalf-Of"), b"")], [(b"On-Behalf-Of", BytesSubclass(b""))],
    [(OpaqueValue(), b"")], [(b"ordinary", OpaqueValue())],
    [(b"On-Behalf-Of", b""), (b"ordinary", None)],
    BrokenSequence(), BrokenAfterMatchSequence(),
], ids=["null", "boolean", "integer", "empty-text", "text", "bytes", "bytearray",
        "memoryview", "mapping", "set", "iterator", "object", "user-list", "many-headers", "short-pair", "long-pair",
        "list-pair", "text-name", "text-value", "bytearray-name", "bytearray-value",
        "bytes-subclass-name", "bytes-subclass-value", "opaque-name", "opaque-value",
        "invalid-value-after-match", "broken-sequence", "broken-after-match"])
def test_invalid_raw_header_shapes_fail_closed_without_value_leaks(raw_headers):
    with pytest.raises(ValueError) as caught:
        headers_present(raw_headers)
    assert caught.value.args == ("invalid delegation headers",)
    assert caught.value.__suppress_context__ is True
    assert "PRIVATE_" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("name", [
    b"", b"a" * 129, b" On-Behalf-Of", b"On-Behalf-Of ", b"On-Behalf-Of:", b"on behalf of",
    b"on\tbehalf-of", b"on-behalf-of\r\n", b"\x00on-behalf-of", b"\x7fon-behalf-of",
    b"\xffon-behalf-of", b"on/behalf/of", b"on\\behalf-of", b"on(behalf)of",
    b'"on-behalf-of"', b"on,behalf,of", b"on;behalf;of", b"on=behalf=of",
    b"on[behalf]of", b"on{behalf}of", b"on<behalf>of", b"on?behalf?of", b"on@behalf@of",
])
def test_malformed_header_names_deny_instead_of_becoming_negative_results(name):
    with pytest.raises(ValueError, match="^invalid delegation headers$"):
        headers_present([(name, b"PRIVATE_VALUE")])


@pytest.mark.parametrize("base", [dict, list, tuple])
def test_container_subclasses_are_rejected_without_executing_custom_hooks(base):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("custom container hook must not run")

    hostile_type = type("HostileContainer", (base,), {
        name: forbidden for name in ("__iter__", "__len__", "__getitem__", "__contains__",
                                     "__getattribute__", "__repr__", "__str__")
    })
    value = hostile_type()
    with pytest.raises(ValueError, match="^invalid delegation claims$"):
        claims_present(value)
    with pytest.raises(ValueError, match="^invalid delegation headers$"):
        headers_present(value)
    assert calls == []


@pytest.mark.parametrize("base", [Mapping, Sequence])
def test_custom_abstract_containers_are_rejected_before_iteration(base):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("custom container hook must not run")

    hostile_type = type("HostileContainer", (base,), {
        name: forbidden for name in ("__iter__", "__len__", "__getitem__", "__contains__")
    })
    value = hostile_type()
    with pytest.raises(ValueError, match="^invalid delegation claims$"):
        claims_present(value)
    with pytest.raises(ValueError, match="^invalid delegation headers$"):
        headers_present(value)
    assert calls == []


@pytest.mark.parametrize("comparison_result", [False, True])
def test_container_type_validation_never_invokes_metaclass_equality(comparison_result):
    calls = []

    class HostileMeta(type):
        def __eq__(cls, other):
            calls.append("equality")
            return comparison_result

    class HostileContainer(metaclass=HostileMeta):
        def __len__(self):
            calls.append("length")
            raise AssertionError("custom length must not run")

        def __iter__(self):
            calls.append("iteration")
            raise AssertionError("custom iteration must not run")

    value = HostileContainer()
    with pytest.raises(ValueError, match="^invalid delegation claims$"):
        claims_present(value)
    with pytest.raises(ValueError, match="^invalid delegation headers$"):
        headers_present(value)
    assert calls == []


def test_predicates_do_no_filesystem_network_process_or_environment_io(monkeypatch):
    module = importlib.import_module("gateway.source_delegation")
    attempted = []

    def forbidden(*args, **kwargs):
        attempted.append(True)
        raise AssertionError("I/O is forbidden")

    with monkeypatch.context() as guard:
        for owner, name in ((builtins, "open"), (io, "open"), (os, "open"),
                            (os, "getenv"), (socket, "socket"),
                            (socket, "create_connection"), (subprocess, "Popen")):
            guard.setattr(owner, name, forbidden)
        assert module.has_unsupported_delegation_claims({"act": OpaqueValue()}) is True
        assert module.has_unsupported_delegation_claims({"sub": OpaqueValue()}) is False
        assert module.has_unsupported_delegation_headers([(b"On-Behalf-Of", b"")]) is True
        assert module.has_unsupported_delegation_headers([(b"ordinary", b"\xff")]) is False
        with pytest.raises(ValueError, match="^invalid delegation claims$"):
            module.has_unsupported_delegation_claims(None)
        with pytest.raises(ValueError, match="^invalid delegation headers$"):
            module.has_unsupported_delegation_headers(None)
    assert attempted == []

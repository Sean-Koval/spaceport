"""Fixed signing material is reusable; per-request identity values stay fresh."""
import json

from cryptography.hazmat.primitives import serialization
import jwt
import pytest

from tests.unit import test_identity as identity

pytestmark = pytest.mark.unit


def test_identity_helpers_do_not_reparse_the_fixed_signing_key(clock, monkeypatch):
    loads = []
    original = serialization.load_pem_private_key

    def track(*args, **kwargs):
        loads.append(None)
        return original(*args, **kwargs)

    monkeypatch.setattr(serialization, "load_pem_private_key", track)
    monkeypatch.setattr(jwt.algorithms, "load_pem_private_key", track)
    for index in range(2):
        assert identity.jwk(kid=f"key-{index}")["kid"] == f"key-{index}"
        assert identity.signed_token(clock, jti=f"request-{index}")
    assert loads == []


def test_identity_jwk_mapping_is_fresh_and_matches_the_fixed_pem():
    original = serialization.load_pem_private_key(
        identity.TEST_SIGNING_KEY.encode(), password=None)
    expected = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(original.public_key()))
    first = identity.jwk(kid="first", use="enc")
    first["n"] = "changed-in-one-request"
    second = identity.jwk()
    assert first is not second
    assert second == {**expected, "kid": "key-1", "use": "sig", "alg": "RS256"}


@pytest.mark.parametrize("algorithm", ["RS256", "RS512", "PS256"])
def test_reused_key_really_signs_fresh_claims_and_rejects_bad_signatures(clock, algorithm):
    public_key = jwt.algorithms.RSAAlgorithm.from_jwk(identity.jwk())
    tokens = [identity.signed_token(clock, algorithm=algorithm, jti=f"request-{index}")
              for index in range(2)]
    assert tokens[0] != tokens[1]
    for index, token in enumerate(tokens):
        values = jwt.decode(token, public_key, algorithms=[algorithm],
            audience="gateway-api", issuer="https://issuer.example.test",
            options={"verify_exp": False, "verify_iat": False})
        assert values["jti"] == f"request-{index}"
        assert values["sub"] == "verified-user"
        assert values["client_id"] == "studio-client"
        assert jwt.get_unverified_header(token) == {
            "alg": algorithm, "kid": "key-1", "typ": "at+jwt"}
        head, body, signature = token.split(".")
        changed = ("A" if signature[0] != "A" else "B") + signature[1:]
        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(f"{head}.{body}.{changed}", public_key,
                algorithms=[algorithm], audience="gateway-api")


def test_non_rsa_fixture_algorithm_still_uses_its_own_signing_path(clock):
    token = identity.signed_token(clock, algorithm="HS256")
    assert jwt.get_unverified_header(token)["alg"] == "HS256"
    public_key = jwt.algorithms.RSAAlgorithm.from_jwk(identity.jwk())
    with pytest.raises(jwt.InvalidAlgorithmError):
        jwt.decode(token, public_key, algorithms=["RS256"], audience="gateway-api")

"""Metadata must not convey revocation-bypassing bearer capabilities."""
from urllib.parse import quote
import pytest
from pydantic import ValidationError
from gateway.contracts.manifest import Link

pytestmark=pytest.mark.contract

# GCS V4/V2, AWS presigning, Azure SAS and common signed URL capabilities.
SIGNED_KEYS=('x-goog-algorithm','x-goog-credential','x-goog-date','x-goog-expires',
    'x-goog-signedheaders','x-goog-signature','googleaccessid','signature',
    'awsaccesskeyid','x-amz-algorithm','x-amz-credential','x-amz-date','x-amz-expires',
    'x-amz-signedheaders','x-amz-signature','x-amz-security-token','sig')

@pytest.mark.parametrize('key',SIGNED_KEYS)
@pytest.mark.parametrize('form',['lower','upper','encoded'])
def test_signed_access_query_keys_are_rejected(key,form):
    value=key if form=='lower' else key.upper() if form=='upper' else ''.join(f'%{ord(c):02x}' for c in key)
    with pytest.raises(ValidationError):
        Link(label='Definition',url='https://example.com/file?'+value+'=synthetic')


def test_ordinary_document_query_remains_inert_metadata():
    assert Link(label='Definition',url='https://example.com/docs?section=revenue').url.endswith('section=revenue')

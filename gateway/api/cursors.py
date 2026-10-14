"""Short-lived authenticated opaque continuations, independent of result cursors."""
import base64
from datetime import timedelta
import json
import secrets
import re
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
from pydantic import Field
from gateway.contracts.canonical import StrictModel, AwareTimestamp, canonical_bytes
from gateway.registry.repository import not_found


class PagePosition(StrictModel):
    version: int = 1
    purpose: str
    identity: str
    scope: str
    query: str
    last: str
    issued_at: AwareTimestamp
    expires_at: AwareTimestamp
    upper: str | None = None


class PageCursorCodec:
    """Operator-provided 256-bit key. Rotation safely invalidates continuations."""
    def __init__(self,key: bytes,clock):
        if type(key) is not bytes or len(key)!=32:raise ValueError('An explicit 32-byte cursor key is required')
        self._cipher,self.clock=AESGCM(key),clock

    def issue(self,*,purpose,identity,scope,query,last,upper=None):
        now=self.clock.now()
        payload=PagePosition(purpose=purpose,identity=identity,scope=scope,query=query,last=last,upper=upper,
            issued_at=now,expires_at=now+timedelta(seconds=300))
        nonce=secrets.token_bytes(12)
        raw=nonce+self._cipher.encrypt(nonce,canonical_bytes(payload),b'dpg-page-cursor-v1')
        token=base64.urlsafe_b64encode(raw).decode().rstrip('=')
        if len(token)>4096:raise ValueError('Cursor payload exceeds bound')
        return token

    def read(self,token,*,purpose,identity,scope,query):
        try:
            if type(token) is not str or not 32<=len(token)<=4096 or not re.fullmatch(r'[A-Za-z0-9_-]+',token):raise ValueError
            raw=base64.urlsafe_b64decode(token+'='*((-len(token))%4))
            if base64.urlsafe_b64encode(raw).decode().rstrip('=')!=token:raise ValueError
            body=self._cipher.decrypt(raw[:12],raw[12:],b'dpg-page-cursor-v1')
            payload=PagePosition.model_validate(json.loads(body))
            now=self.clock.now()
            if (payload.version!=1 or (payload.purpose,payload.identity,payload.scope,payload.query)!=(purpose,identity,scope,query)
                or payload.issued_at>now or payload.expires_at<=now or payload.expires_at-payload.issued_at!=timedelta(seconds=300)):
                raise ValueError
            return payload
        except (ValueError,TypeError,InvalidTag,UnicodeError,OverflowError):raise not_found() from None

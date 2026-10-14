"""Exercise matrix cursor setup without needing PostgreSQL or live sockets."""
import pytest

from gateway.api.cursors import PageCursorCodec
from tests.security.test_adversarial_matrix import MATRIX_CURSOR_KEY

pytestmark = pytest.mark.unit


def test_matrix_cursor_key_supports_real_codec_round_trip(clock):
    codec = PageCursorCodec(MATRIX_CURSOR_KEY, clock)
    context = {'purpose': 'catalog', 'identity': 'matrix-service',
        'scope': 'north', 'query': 'revenue'}
    token = codec.issue(**context, last='revenue')
    assert codec.read(token, **context).last == 'revenue'


@pytest.mark.parametrize('key', [b'', b'k' * 16, b'k' * 24, b'k' * 31,
    b'k' * 33, b'synthetic-matrix-cursor-key-32bytes', 'k' * 32, bytearray(b'k' * 32), None])
def test_page_cursor_key_requires_exactly_32_bytes(clock, key):
    with pytest.raises(ValueError, match='An explicit 32-byte cursor key is required'):
        PageCursorCodec(key, clock)

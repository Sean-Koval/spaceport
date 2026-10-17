"""SPACEPORT_* client settings take precedence; legacy DPG_* names still work."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import subprocess
import threading

import pytest

pytestmark = pytest.mark.contract

CLIENTS = [
    pytest.param(['.venv/bin/python', 'clients/python_agent.py'], id='python'),
    pytest.param(['node', 'clients/typescript_agent.ts'], id='typescript'),
]


class Recorder:
    """Loopback endpoint that records each bearer token and refuses the request."""

    def __init__(self):
        self.tokens: list[str] = []
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def _refuse(self):
                recorder.tokens.append(self.headers.get('Authorization', ''))
                length = int(self.headers.get('Content-Length') or 0)
                if length:
                    self.rfile.read(length)
                self.send_response(503)
                self.send_header('Content-Length', '0')
                self.end_headers()

            do_GET = do_POST = do_DELETE = _refuse

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}/mcp'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def run_client(command, env):
    base = {key: value for key, value in os.environ.items()
            if not key.startswith(('SPACEPORT_', 'DPG_'))}
    completed = subprocess.run(command, env={**base, **env}, capture_output=True, timeout=60)
    # The endpoint refuses every request, so the client must fail closed without detail.
    assert completed.returncode == 1
    assert completed.stdout == b''
    assert completed.stderr == b'{"error":"CLIENT_ACCEPTANCE_FAILED"}\n'


def token(tmp_path, name, value):
    path = tmp_path / name
    path.write_text(value + '\n')
    return str(path)


@pytest.mark.parametrize('command', CLIENTS)
def test_spaceport_names_take_precedence_over_legacy_names(command, tmp_path):
    with Recorder() as current, Recorder() as legacy:
        run_client(command, {
            'SPACEPORT_MCP_URL': current.url,
            'SPACEPORT_TOKEN_FILE': token(tmp_path, 'current.token', 'spaceport-token'),
            'DPG_MCP_URL': legacy.url,
            'DPG_TOKEN_FILE': token(tmp_path, 'legacy.token', 'legacy-token'),
        })
    assert current.tokens and set(current.tokens) == {'Bearer spaceport-token'}
    assert legacy.tokens == []


@pytest.mark.parametrize('command', CLIENTS)
def test_legacy_dpg_names_remain_a_fallback(command, tmp_path):
    with Recorder() as legacy:
        run_client(command, {
            'DPG_MCP_URL': legacy.url,
            'DPG_TOKEN_FILE': token(tmp_path, 'legacy.token', 'legacy-token'),
        })
    assert legacy.tokens and set(legacy.tokens) == {'Bearer legacy-token'}


@pytest.mark.parametrize('command', CLIENTS)
def test_names_can_be_mixed_per_setting(command, tmp_path):
    with Recorder() as legacy:
        run_client(command, {
            'DPG_MCP_URL': legacy.url,
            'SPACEPORT_TOKEN_FILE': token(tmp_path, 'current.token', 'spaceport-token'),
            'DPG_TOKEN_FILE': token(tmp_path, 'legacy.token', 'legacy-token'),
        })
    assert legacy.tokens and set(legacy.tokens) == {'Bearer spaceport-token'}


@pytest.mark.parametrize('command', CLIENTS)
def test_missing_url_fails_closed(command, tmp_path):
    run_client(command, {'SPACEPORT_TOKEN_FILE': token(tmp_path, 'current.token', 'spaceport-token')})

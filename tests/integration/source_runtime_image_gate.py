"""Bounded no-network qualification of the exact locally built source image."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import signal
import tempfile

PREFIX = 'GATEWAY_BIGQUERY_IMAGE_RESULT '
CHECKS = ['immutable_packages', 'nonroot', 'readonly_runtime', 'lazy_imports', 'http_health', 'retained_compiler', 'zero_provider_auth']
VERSIONS = {'cube_server', 'cube_core', 'cube_bigquery_driver', 'bigquery_sdk', 'compute_auth', 'bigquery_auth', 'pg'}


def receipt(output):
    if type(output) is not str or len(output.encode()) > 65536:
        raise ValueError('Invalid source image evidence')
    lines = [line[len(PREFIX):] for line in output.splitlines() if line.startswith(PREFIX)]
    if len(lines) != 1 or len(lines[0]) > 4096:
        raise ValueError('Invalid source image evidence')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError('Duplicate source image evidence')
            result[key] = value
        return result
    value = json.loads(lines[0], object_pairs_hook=unique)
    if (type(value) is not dict or set(value) != {'schema_version','status','versions','node','uid','checks'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1 or value['status'] != 'passed'
            or type(value['uid']) is not int or value['uid'] != 65532 or value['checks'] != CHECKS
            or not isinstance(value['node'], str) or not re.fullmatch(r'v24\.\d{1,3}\.\d{1,3}', value['node'])
            or type(value['versions']) is not dict or set(value['versions']) != VERSIONS):
        raise ValueError('Invalid source image evidence')
    for name, version in value['versions'].items():
        pattern = '1\\.8\\.5' if name.startswith('cube_') else '(?:9|10)\\.\\d{1,3}\\.\\d{1,3}' if name.endswith('_auth') else ('7' if name == 'bigquery_sdk' else '8')+'\\.\\d{1,3}\\.\\d{1,3}'
        if type(version) is not str or not re.fullmatch(pattern, version): raise ValueError('Unsupported source image package')
    return value


def validate_identity(image, run_label):
    match = re.fullmatch(r'(gateway-cube-[a-f0-9]{16})-bigquery:local', image)
    if (match is None or run_label != os.environ.get('GATEWAY_CUBE_RUN_LABEL')
        or not (run_label == match[1]+'-local' or re.fullmatch(r'[0-9]+-[0-9]+', run_label))):
        raise ValueError('Exact run-private image identity required')


async def _bounded(command, timeout=20, maximum=65536):
    process = None
    try:
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        data = bytearray()
        async with asyncio.timeout(timeout):
            while chunk := await process.stdout.read(8192):
                data.extend(chunk)
                if len(data) > maximum: raise ValueError('Source image output exceeds bound')
            if await process.wait() != 0: raise ValueError('Source image command failed')
        return data.decode('utf-8')
    finally:
        if process is not None:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            await asyncio.wait_for(process.communicate(), 0.5)


async def execute(image, run_label):
    validate_identity(image, run_label)
    with tempfile.TemporaryDirectory(prefix='source-image-smoke-') as directory:
        cidfile = Path(directory)/'container-id'
        try:
            output = await _bounded(['docker','run','--rm','--network','none','--read-only',
                '--platform','linux/amd64','--cidfile',str(cidfile),'--label','gateway.cube.run='+run_label,
                '--tmpfs','/tmp:rw,noexec,nosuid,size=64m','--cap-drop','ALL','--security-opt','no-new-privileges:true',
                '--pids-limit','128','--memory','1g','--cpus','1',image,'/cube/probe/bigquery-serving-image-smoke.cjs'])
            return receipt(output)
        finally:
            if cidfile.exists():
                identifier = cidfile.read_text().strip()
                if not re.fullmatch(r'[a-f0-9]{64}', identifier): raise ValueError('Invalid source image container identity')
                # CID file is created by this exact docker run in our private fresh
                # directory. Remove only that container, never a name/label wildcard.
                try: await _bounded(['docker','rm','-f',identifier], timeout=2, maximum=8192)
                except ValueError:
                    remaining = await _bounded(['docker','ps','--all','--no-trunc','--filter','id='+identifier,'--format','{{.ID}}'], timeout=2, maximum=1024)
                    if remaining.strip(): raise ValueError('Source image cleanup did not complete')


def main():
    parser=argparse.ArgumentParser(description='Qualify the exact private source image without networking')
    parser.add_argument('--image',required=True);parser.add_argument('--run-label',required=True)
    args=parser.parse_args()
    try:
        result=asyncio.run(execute(args.image,args.run_label))
    except Exception:
        print('GATEWAY_FAILURE_EVIDENCE '+json.dumps({'kind':'checkpoint','checkpoint':'source-image-smoke','exit_status':1}),flush=True)
        return 1
    print(PREFIX+json.dumps(result,sort_keys=True,separators=(',',':')),flush=True)
    return 0


if __name__ == '__main__':raise SystemExit(main())

"""Capture the README picture: the publisher's review screen over the fixture product.

Starts ``scripts/front_mock_gateway.py`` on 127.0.0.1:8000 (the port the
publisher's dev server proxies ``/v1`` to), starts ``vite`` from ``publisher/``,
signs in to the review screen as ``demo-publisher`` and writes a 1440 px wide
screenshot to ``docs/assets/front/review.png``. No gateway, database, Cube or
credential is involved; the bearer is a mock name, never a real token.

Prerequisites: ``npm --prefix publisher ci --ignore-scripts`` and a Python with
Playwright and its Chromium (``pip install playwright && playwright install chromium``).

    python3 scripts/capture_front.py [--out docs/assets/front/review.png]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATEWAY_PORT = 8000
PUBLISHER_PORT = 5173
REVIEW_PATH = '/drafts/d-7f3a/review'
BEARER = 'demo-publisher'
WIDTH, HEIGHT = 1440, 900


def wait_for(url: str, ok: set[int], seconds: float = 60) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status in ok:
                    return
        except urllib.error.HTTPError as error:
            if error.code in ok:
                return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(0.25)
    raise SystemExit(f'{url} did not answer within {seconds:.0f} s')


def capture(out: Path) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit('Playwright is missing: pip install playwright && playwright install chromium')
    base = f'http://127.0.0.1:{PUBLISHER_PORT}'
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={'width': WIDTH, 'height': HEIGHT}, device_scale_factor=1)
        page.goto(base + REVIEW_PATH, wait_until='networkidle')
        page.fill('#session-bearer', BEARER)
        page.click('button[type=submit]')
        page.get_by_role('heading', name='Fixture checks · passed').wait_for(timeout=30_000)
        page.wait_for_timeout(500)
        out.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out))
        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--out', type=Path, default=ROOT / 'docs/assets/front/review.png')
    args = parser.parse_args()
    if not (ROOT / 'publisher/node_modules/.bin/vite').exists():
        raise SystemExit('publisher/node_modules is missing: npm --prefix publisher ci --ignore-scripts')
    env = {**os.environ, 'BROWSER': 'none'}
    gateway = subprocess.Popen([sys.executable, str(ROOT / 'scripts/front_mock_gateway.py'), str(GATEWAY_PORT)])
    publisher = subprocess.Popen(
        ['npx', 'vite', '--host', '127.0.0.1', '--port', str(PUBLISHER_PORT), '--strictPort', '--clearScreen', 'false'],
        cwd=ROOT / 'publisher',
        env=env,
        stdout=subprocess.DEVNULL,
    )
    try:
        wait_for(f'http://127.0.0.1:{GATEWAY_PORT}/v1/products', {401})
        wait_for(f'http://127.0.0.1:{PUBLISHER_PORT}/', {200})
        capture(args.out)
        print(f'wrote {args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out}')
    finally:
        for process in (publisher, gateway):
            process.terminate()
        for process in (publisher, gateway):
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == '__main__':
    main()

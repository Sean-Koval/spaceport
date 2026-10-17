"""Offline check that internal Markdown links and #anchors resolve."""
from pathlib import Path
import re
from urllib.parse import unquote

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
LINK = re.compile(r'(?<!!)\[(?:[^\]\[]|\[[^\]]*\])*\]\(([^)\s]+)(?:\s+"[^"]*")?\)|<img[^>]*src="([^"]+)"|srcset="([^"]+)"')
FENCE = re.compile(r'^(```|~~~)')
HEADING = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')


def lines_outside_fences(path):
    fenced = False
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if FENCE.match(line.strip()):
            fenced = not fenced
        elif not fenced:
            yield number, line


def slug(text):
    """GitHub heading anchor: strip markup and punctuation, lowercase, spaces to hyphens."""
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'[`*_~]', '', text).strip().lower()
    return re.sub(r'[^\w\- ]', '', text).replace(' ', '-')


def anchors(path):
    seen, found = {}, set()
    for _, line in lines_outside_fences(path):
        if match := HEADING.match(line):
            base = slug(match.group(2))
            count = seen.get(base, 0)
            seen[base] = count + 1
            found.add(base if count == 0 else f'{base}-{count}')
        found.update(re.findall(r'<a\s+(?:name|id)="([^"]+)"', line))
    return found


def documents():
    yield from (ROOT / name for name in ('README.md', 'ROADMAP.md', 'THIRD_PARTY_NOTICES.md'))
    yield from sorted(p for p in (ROOT / 'docs').rglob('*.md') if 'notices' not in p.parts)


def test_internal_markdown_links_and_anchors_resolve():
    broken, cache = [], {}
    for document in documents():
        for number, line in lines_outside_fences(document):
            for match in LINK.finditer(re.sub(r'`[^`]*`', '', line)):
                target = match.group(1) or match.group(2) or match.group(3).split()[0]
                if re.match(r'^[a-z][a-z0-9+.-]*:', target, re.I) or target.startswith('//'):
                    continue
                path, _, fragment = target.partition('#')
                destination = (document.parent / unquote(path)).resolve() if path else document
                where = f'{document.relative_to(ROOT)}:{number}: {target}'
                if not destination.exists():
                    broken.append(where)
                elif fragment and destination.suffix == '.md':
                    if destination not in cache:
                        cache[destination] = anchors(destination)
                    if unquote(fragment).lower() not in cache[destination]:
                        broken.append(where)
    assert broken == []


def test_documentation_index_lists_every_docs_directory():
    index = (ROOT / 'docs/README.md').read_text()
    directories = sorted(p.name for p in (ROOT / 'docs').iterdir() if p.is_dir())
    assert directories and all(f'`{name}/`' in index for name in directories)

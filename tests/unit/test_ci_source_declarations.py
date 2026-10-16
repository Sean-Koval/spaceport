"""Declaration caching must never cache source validity or a marker verdict."""
import importlib
from collections import OrderedDict
from pathlib import Path
import subprocess

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]


def commit(root):
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture', '-c',
        'user.email=fixture@example.invalid', 'commit', '-qm', 'Frozen declarations'], check=True)
    return subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    api = importlib.import_module('scripts.pytest_runtime_diagnostics')
    (tmp_path / 'tests').mkdir()
    path = tmp_path / 'tests/test_bound.py'
    path.write_text('class TestOuter:\n    class Inner:\n        async def test_nested(self):\n            pass\n'
        'def test_bound():\n    pass\n')
    sha = commit(tmp_path)
    monkeypatch.setattr(api, 'ROOT', tmp_path)
    monkeypatch.setattr(api, '_source_blobs', None); monkeypatch.setattr(api, '_source_commit', None)
    api.freeze_source(sha)
    if hasattr(api, '_declaration_cache'):
        monkeypatch.setattr(api, '_declaration_cache', OrderedDict())
        monkeypatch.setattr(api, '_declaration_bytes', 0)
    return api, path, sha


def test_parameterized_identities_parse_once_but_read_each_time(frozen, monkeypatch):
    api, path, sha = frozen
    events = []
    parse, read = api.ast.parse, api.source_file
    def counted_parse(*args, **kwargs): events.append('parse'); return parse(*args, **kwargs)
    def counted_read(*args, **kwargs): events.append('read'); return read(*args, **kwargs)
    monkeypatch.setattr(api.ast, 'parse', counted_parse); monkeypatch.setattr(api, 'source_file', counted_read)
    for suffix in ('[one]', '[two]', '[private::parameter]'):
        assert api.source_test_identity('tests/test_bound.py::test_bound' + suffix) == 'tests/test_bound.py::test_bound'
    assert api.source_test_identity('tests/test_bound.py::TestOuter::Inner::test_nested') is not None
    assert api.source_test_identity('tests/test_bound.py::TestOuter::test_nested') is None
    assert events.count('read') == 5 and events.count('parse') == 1


def test_cached_declarations_reject_current_in_place_mutation(frozen):
    api, path, sha = frozen
    assert api.source_test_identity('tests/test_bound.py::test_bound') is not None
    path.write_text(path.read_text().replace('test_bound', 'test_forged'))
    assert api.source_test_identity('tests/test_bound.py::test_bound') is None
    assert api.source_test_identity('tests/test_bound.py::test_forged') is None


def test_new_commit_same_path_recomputes_declarations(frozen):
    api, path, sha = frozen
    assert api.source_test_identity('tests/test_bound.py::test_bound') is not None
    path.write_text('def test_new():\n    pass\n')
    replacement = commit(path.parents[1]); assert replacement != sha
    api.freeze_source(replacement)
    assert api.source_test_identity('tests/test_bound.py::test_bound') is None
    assert api.source_test_identity('tests/test_bound.py::test_new') is not None


def test_cached_declarations_reject_symlink_replacement(frozen, tmp_path):
    api, path, sha = frozen
    assert api.source_test_identity('tests/test_bound.py::test_bound') is not None
    other = tmp_path / 'same-bytes'; other.write_bytes(path.read_bytes())
    path.unlink(); path.symlink_to(other)
    assert api.source_test_identity('tests/test_bound.py::test_bound') is None


def test_cached_declarations_reject_untracked_same_bytes(frozen):
    api, path, sha = frozen
    assert api.source_test_identity('tests/test_bound.py::test_bound') is not None
    (path.parent / 'test_untracked.py').write_bytes(path.read_bytes())
    assert api.source_test_identity('tests/test_untracked.py::test_bound') is None


def test_root_change_does_not_reuse_other_checkout_validity(frozen, tmp_path, monkeypatch):
    api, path, sha = frozen
    assert api.source_test_identity('tests/test_bound.py::test_bound') is not None
    other = tmp_path / 'other'; (other / 'tests').mkdir(parents=True)
    (other / 'tests/test_bound.py').write_text('def test_other():\n    pass\n')
    other_sha = commit(other)
    monkeypatch.setattr(api, 'ROOT', other)
    assert api.source_test_identity('tests/test_bound.py::test_bound') is None
    api.freeze_source(other_sha)
    assert api.source_test_identity('tests/test_bound.py::test_other') is not None
    assert api.source_test_identity('tests/test_bound.py::test_bound') is None


def test_declaration_cache_has_entry_and_retained_byte_bounds(frozen, monkeypatch):
    api, path, sha = frozen
    assert hasattr(api, '_declaration_cache'), 'Missing bounded declaration cache'
    api._declaration_cache.clear()
    monkeypatch.setattr(api, '_declaration_bytes', 0)
    monkeypatch.setattr(api, 'DECLARATION_CACHE_ENTRIES', 2)
    monkeypatch.setattr(api, 'DECLARATION_CACHE_BYTES', 2048)
    for index in range(6):
        value = f'def test_case_{index}():\n    pass\n'
        api.source_declarations('tests/test_bound.py', value)
        assert len(api._declaration_cache) <= 2
        assert 0 <= api._declaration_bytes <= 2048
    cached = dict(api._declaration_cache)
    # An oversized parse is still correct, but is not retained.
    value = 'def test_large():\n    pass\n' + '#' * 3000
    assert ('test_large',) in api.source_declarations('tests/test_bound.py', value)
    assert dict(api._declaration_cache) == cached


def test_duplicate_class_resolution_keeps_original_first_declaration(frozen):
    api, path, sha = frozen
    path.write_text('class Duplicate:\n    def test_first(self):\n        pass\n'
        'class Duplicate:\n    def test_shadowed(self):\n        pass\n')
    api.freeze_source(commit(path.parents[1]))
    assert api.source_test_identity('tests/test_bound.py::Duplicate::test_first') is not None
    assert api.source_test_identity('tests/test_bound.py::Duplicate::test_shadowed') is None

"""Filesystem contract tests; these do not validate GKE or a CSI installation."""
import os
from pathlib import Path
import stat

import pytest

pytestmark = pytest.mark.unit

APP_SECRETS = ('registry_app', 'cube_signing_key', 'cube_test_signing_key', 'audit_hmac', 'cursor_key')


def projection(root: Path, files: dict[str, bytes]):
    root.mkdir(parents=True)
    generation = root / '..2026_10_14_22_00_00.123456789'
    generation.mkdir()
    for name, content in files.items():
        path = generation / name
        path.write_bytes(content)
        path.chmod(0o440)
        (root / name).symlink_to('..data/' + name)
    (root / '..data').symlink_to(generation.name)
    return generation


def inputs(tmp_path):
    root = tmp_path / 'inputs'
    projection(root / 'config', {'pilot.json': b'{}', 'registry-ca.pem': b'fixture ca'})
    projection(root / 'secrets', {name: b'a' * 43 for name in APP_SECRETS})
    parent = tmp_path / 'output'
    parent.mkdir()
    return root, parent / 'runtime'


def test_snapshots_only_allowlisted_projected_inputs_as_private_regular_files(tmp_path):
    from gateway.pilot_mounts import snapshot_inputs
    source, destination = inputs(tmp_path)
    (source / 'secrets' / 'unrelated-secret').write_bytes(b'do not copy')
    snapshot_inputs('api', source_root=source, output_root=destination)
    assert sorted(path.name for path in (destination / 'secrets').iterdir()) == sorted(APP_SECRETS)
    assert not (destination / 'auth').exists()
    for path in destination.rglob('*'):
        assert not path.is_symlink()
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
        assert path.stat().st_uid == os.geteuid()
    assert (destination / 'config' / 'pilot.json').read_bytes() == b'{}'
    assert (destination / 'secrets' / 'registry_app').read_bytes() == b'a' * 43


@pytest.mark.parametrize('target', ['/etc/passwd', '../outside', '..data/../outside', '..data/registry_owner'])
def test_rejects_noncanonical_visible_symlinks(tmp_path, target):
    from gateway.pilot_mounts import snapshot_inputs
    source, destination = inputs(tmp_path)
    path = source / 'secrets' / 'registry_app'
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(ValueError, match='^Pilot input snapshot is unavailable or invalid$'):
        snapshot_inputs('api', source_root=source, output_root=destination)
    assert not destination.exists()


@pytest.mark.parametrize('target', ['/tmp', '../outside', '..', '..data', '..evil/nested'])
def test_rejects_escaping_or_recursive_generation_links(tmp_path, target):
    from gateway.pilot_mounts import snapshot_inputs
    source, destination = inputs(tmp_path)
    data = source / 'config' / '..data'
    data.unlink()
    data.symlink_to(target)
    with pytest.raises(ValueError, match='^Pilot input snapshot is unavailable or invalid$'):
        snapshot_inputs('api', source_root=source, output_root=destination)
    assert not destination.exists()


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo', 'directory', 'writable', 'oversize', 'empty', 'missing'])
def test_rejects_unsafe_projected_payload(tmp_path, kind):
    from gateway.pilot_mounts import snapshot_inputs
    source, destination = inputs(tmp_path)
    payload = source / 'secrets' / os.readlink(source / 'secrets' / '..data') / 'registry_app'
    payload.unlink()
    if kind == 'symlink':
        payload.symlink_to('/etc/passwd')
    elif kind == 'hardlink':
        outside = tmp_path / 'outside'
        outside.write_bytes(b'a' * 43)
        os.link(outside, payload)
    elif kind == 'fifo':
        os.mkfifo(payload)
    elif kind == 'directory':
        payload.mkdir()
    elif kind == 'writable':
        payload.write_bytes(b'a' * 43)
        payload.chmod(0o666)
    elif kind == 'oversize':
        payload.write_bytes(b'x' * 129)
    elif kind == 'empty':
        payload.write_bytes(b'')
    with pytest.raises(ValueError, match='^Pilot input snapshot is unavailable or invalid$'):
        snapshot_inputs('api', source_root=source, output_root=destination)
    assert not destination.exists()


def test_rejects_symlinked_mount_root_or_ancestor(tmp_path):
    from gateway.pilot_mounts import snapshot_inputs
    source, destination = inputs(tmp_path)
    alias = tmp_path / 'alias'
    alias.symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError):
        snapshot_inputs('api', source_root=alias, output_root=destination)
    config = source / 'config'
    config.rename(source / 'real-config')
    config.symlink_to('real-config', target_is_directory=True)
    with pytest.raises(ValueError):
        snapshot_inputs('api', source_root=source, output_root=destination)


def test_rejects_replaced_generation_during_read(tmp_path, monkeypatch):
    from gateway import pilot_mounts
    source, destination = inputs(tmp_path)
    original = os.read
    changed = False
    def rotate(fd, size):
        nonlocal changed
        result = original(fd, size)
        if not changed:
            changed = True
            root = source / 'config'
            replacement = root / '..2026_replacement'
            replacement.mkdir()
            (root / '..data').unlink()
            (root / '..data').symlink_to(replacement.name)
        return result
    monkeypatch.setattr(pilot_mounts.os, 'read', rotate)
    with pytest.raises(ValueError):
        pilot_mounts.snapshot_inputs('api', source_root=source, output_root=destination)
    assert not destination.exists()


def test_rejects_payload_mutation_during_read(tmp_path, monkeypatch):
    from gateway import pilot_mounts
    source, destination = inputs(tmp_path)
    original = os.read
    changed = False
    def mutate(fd, size):
        nonlocal changed
        result = original(fd, size)
        if not changed:
            changed = True
            payload = source / 'config' / os.readlink(source / 'config' / '..data') / 'pilot.json'
            payload.chmod(0o640)
            payload.write_bytes(b'changed')
        return result
    monkeypatch.setattr(pilot_mounts.os, 'read', mutate)
    with pytest.raises(ValueError):
        pilot_mounts.snapshot_inputs('api', source_root=source, output_root=destination)
    assert not destination.exists()


def test_supports_regular_driver_files_without_following_symlinks(tmp_path):
    from gateway.pilot_mounts import snapshot_inputs
    source, destination = inputs(tmp_path)
    for folder in (source / 'config', source / 'secrets'):
        for path in folder.iterdir():
            if path.is_symlink() and path.name != '..data':
                content = path.read_bytes()
                path.unlink()
                path.write_bytes(content)
                path.chmod(0o440)
        (folder / '..data').unlink()
    snapshot_inputs('api', source_root=source, output_root=destination)
    assert (destination / 'config' / 'pilot.json').read_bytes() == b'{}'


@pytest.mark.parametrize('profile,names', [
    ('worker', APP_SECRETS), ('retention', ('registry_app', 'registry_audit_retention')),
    ('migrate', ('registry_owner',)), ('provision', ('registry_owner',)),
])
def test_profiles_copy_exactly_their_credentials(tmp_path, profile, names):
    from gateway.pilot_mounts import snapshot_inputs
    source, destination = inputs(tmp_path)
    secret_root = source / 'secrets'
    generation = secret_root / os.readlink(secret_root / '..data')
    for name in ('registry_owner', 'registry_audit_retention'):
        (generation / name).write_bytes(b'a' * 43)
        (secret_root / name).symlink_to('..data/' + name)
    projection(source / 'identities', {'identities.json': b'{"identities": []}'})
    snapshot_inputs(profile, source_root=source, output_root=destination)
    assert {path.name for path in (destination / 'secrets').iterdir()} == set(names)
    assert (destination / 'identities').exists() == (profile == 'provision')


def test_optional_service_hashes_require_explicit_opt_in(tmp_path):
    from gateway.pilot_mounts import snapshot_inputs
    source, destination = inputs(tmp_path)
    projection(source / 'auth', {'credential-hashes.json': b'{}'})
    snapshot_inputs('api', source_root=source, output_root=destination, include_service_hashes=True)
    assert (destination / 'auth' / 'credential-hashes.json').read_bytes() == b'{}'
    with pytest.raises(ValueError):
        snapshot_inputs('migrate', source_root=source, output_root=destination.parent / 'other', include_service_hashes=True)


def test_rejects_unknown_profile_relative_paths_and_existing_destination(tmp_path):
    from gateway.pilot_mounts import snapshot_inputs
    source, destination = inputs(tmp_path)
    for profile, src, dst in [('owner', source, destination), ('api', Path('relative'), destination),
                              ('api', source, Path('relative'))]:
        with pytest.raises(ValueError):
            snapshot_inputs(profile, source_root=src, output_root=dst)
    destination.mkdir()
    sentinel = destination / 'keep'
    sentinel.write_text('keep')
    with pytest.raises(ValueError):
        snapshot_inputs('api', source_root=source, output_root=destination)
    assert sentinel.read_text() == 'keep'


def test_service_mapping_provision_can_snapshot_optional_hashes(tmp_path):
    from gateway.pilot_mounts import snapshot_inputs
    source, destination = inputs(tmp_path)
    secrets = source / 'secrets'
    generation = secrets / os.readlink(secrets / '..data')
    (generation / 'registry_owner').write_bytes(b'a' * 43)
    (secrets / 'registry_owner').symlink_to('..data/registry_owner')
    projection(source / 'identities', {'identities.json': b'{"principals": []}'})
    projection(source / 'auth', {'credential-hashes.json': b'{}'})
    snapshot_inputs('provision', source_root=source, output_root=destination, include_service_hashes=True)
    assert (destination / 'auth' / 'credential-hashes.json').read_bytes() == b'{}'
    assert {path.name for path in (destination / 'secrets').iterdir()} == {'registry_owner'}


def test_rejects_generation_directory_symlink(tmp_path):
    from gateway.pilot_mounts import snapshot_inputs
    source, destination = inputs(tmp_path)
    root = source / 'secrets'
    generation = root / os.readlink(root / '..data')
    moved = tmp_path / 'outside'
    generation.rename(moved)
    generation.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError):
        snapshot_inputs('api', source_root=source, output_root=destination)
    assert not destination.exists()


def test_rejects_output_parent_symlink_and_cleans_failed_writes(tmp_path, monkeypatch):
    from gateway import pilot_mounts
    source, destination = inputs(tmp_path)
    alias = tmp_path / 'output-alias'
    alias.symlink_to(destination.parent, target_is_directory=True)
    with pytest.raises(ValueError):
        pilot_mounts.snapshot_inputs('api', source_root=source, output_root=alias / 'runtime')
    def fail_write(fd, data):
        raise OSError('sensitive filename and content must never be surfaced')
    monkeypatch.setattr(pilot_mounts.os, 'write', fail_write)
    with pytest.raises(ValueError, match='^Pilot input snapshot is unavailable or invalid$'):
        pilot_mounts.snapshot_inputs('api', source_root=source, output_root=destination)
    assert not destination.exists()


def test_rejects_payload_inode_replacement_during_read(tmp_path, monkeypatch):
    from gateway import pilot_mounts
    source, destination = inputs(tmp_path)
    original = os.read
    changed = False
    def replace(fd, size):
        nonlocal changed
        content = original(fd, size)
        if not changed:
            changed = True
            root = source / 'config'
            path = root / os.readlink(root / '..data') / 'pilot.json'
            path.unlink()
            path.write_bytes(b'{}')
        return content
    monkeypatch.setattr(pilot_mounts.os, 'read', replace)
    with pytest.raises(ValueError):
        pilot_mounts.snapshot_inputs('api', source_root=source, output_root=destination)
    assert not destination.exists()


def test_cli_fixes_input_paths_and_sanitizes_missing_mount_failures(monkeypatch, capsys):
    import sys
    from gateway import pilot_mounts
    monkeypatch.setattr(sys, 'argv', ['pilot_mounts', 'api'])
    def fail_open(path):
        raise OSError('private value and path must not appear')
    monkeypatch.setattr(pilot_mounts, '_open_directory', fail_open)
    with pytest.raises(SystemExit) as failure:
        pilot_mounts.main()
    assert failure.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == 'Pilot input snapshot is unavailable or invalid\n'
    monkeypatch.setattr(sys, 'argv', ['pilot_mounts', 'api', '--source-root', '/arbitrary'])
    with pytest.raises(SystemExit) as failure:
        pilot_mounts.main()
    assert failure.value.code == 2

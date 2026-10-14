"""One-shot, allowlisted projection snapshot for an operator-trusted init container.

This is not a general symlink resolver. Only the Kubernetes AtomicWriter layout
``name -> ..data/name; ..data -> ..generation; ..generation/name`` is accepted,
alongside regular-file mounts. Every actual open uses O_NOFOLLOW. No network,
credential generation, secret logging, or runtime hot rotation is performed.
"""
from contextlib import ExitStack
import os
from pathlib import Path
import re
import stat

_ERROR = 'Pilot input snapshot is unavailable or invalid'
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_APP_SECRETS = ('registry_app', 'cube_signing_key', 'cube_test_signing_key', 'audit_hmac', 'cursor_key')
_PROFILE_SECRETS = {
    'api': _APP_SECRETS,
    'worker': _APP_SECRETS,
    'retention': ('registry_app', 'registry_audit_retention'),
    'migrate': ('registry_owner',),
    'provision': ('registry_owner',),
}


def _signature(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _open_directory(path: Path) -> int:
    """Pin an absolute directory, refusing symlinks in every component."""
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError
    descriptor = os.open('/', _DIR_FLAGS)
    try:
        for component in path.parts[1:]:
            next_descriptor = os.open(component, _DIR_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_payload(directory: int, name: str, limit: int) -> bytes:
    before_entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=directory)
    try:
        before = os.fstat(descriptor)
        if (_signature(before_entry) != _signature(before)
                or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_mode & 0o022 or not 0 < before.st_size <= limit):
            raise ValueError
        content = bytearray()
        while len(content) <= limit:
            part = os.read(descriptor, min(65536, limit + 1 - len(content)))
            if not part:
                break
            content.extend(part)
        after = os.fstat(descriptor)
        after_entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (len(content) != before.st_size or len(content) > limit
                or _signature(before) != _signature(after)
                or _signature(after) != _signature(after_entry)):
            raise ValueError
        return bytes(content)
    finally:
        os.close(descriptor)


def _read_group(root: int, group: str, files: dict[str, int]) -> dict[str, bytes]:
    """Read one stable projection generation, never walk a supplied link target."""
    with ExitStack() as stack:
        directory = os.open(group, _DIR_FLAGS, dir_fd=root)
        stack.callback(os.close, directory)
        group_before = os.fstat(directory)
        entries = {name: os.stat(name, dir_fd=directory, follow_symlinks=False) for name in files}
        projected = all(stat.S_ISLNK(entry.st_mode) for entry in entries.values())
        generation = None
        data_before = None
        payload_dir = directory
        if projected:
            for name in files:
                if os.readlink(name, dir_fd=directory) != '..data/' + name:
                    raise ValueError
            data_before = os.stat('..data', dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISLNK(data_before.st_mode):
                raise ValueError
            generation = os.readlink('..data', dir_fd=directory)
            if (not re.fullmatch(r'\.\.[A-Za-z0-9_-][A-Za-z0-9_.-]{0,250}', generation)
                    or generation in {'..data', '..data_tmp'}):
                raise ValueError
            payload_dir = os.open(generation, _DIR_FLAGS, dir_fd=directory)
            stack.callback(os.close, payload_dir)
            generation_before = os.fstat(payload_dir)
        elif not all(stat.S_ISREG(entry.st_mode) for entry in entries.values()):
            raise ValueError
        result = {name: _read_payload(payload_dir, name, limit) for name, limit in files.items()}
        for name, before in entries.items():
            if _signature(before) != _signature(os.stat(name, dir_fd=directory, follow_symlinks=False)):
                raise ValueError
            if projected and os.readlink(name, dir_fd=directory) != '..data/' + name:
                raise ValueError
        if projected:
            if (_signature(data_before) != _signature(os.stat('..data', dir_fd=directory, follow_symlinks=False))
                    or os.readlink('..data', dir_fd=directory) != generation
                    or _signature(generation_before) != _signature(os.fstat(payload_dir))
                    or _signature(generation_before) != _signature(os.stat(generation, dir_fd=directory, follow_symlinks=False))):
                raise ValueError
        current_group = os.stat(group, dir_fd=root, follow_symlinks=False)
        if (current_group.st_dev, current_group.st_ino) != (group_before.st_dev, group_before.st_ino):
            raise ValueError
        return result


def _write_snapshot(output: Path, groups: dict[str, dict[str, bytes]]) -> None:
    """Create private files exclusively; never replace an earlier snapshot."""
    with ExitStack() as stack:
        parent = _open_directory(output.parent)
        stack.callback(os.close, parent)
        os.mkdir(output.name, mode=0o700, dir_fd=parent)
        root = os.open(output.name, _DIR_FLAGS, dir_fd=parent)
        stack.callback(os.close, root)
        os.fchmod(root, 0o700)
        created: dict[str, list[str]] = {}
        try:
            for group, files in groups.items():
                os.mkdir(group, mode=0o700, dir_fd=root)
                created[group] = []
                directory = os.open(group, _DIR_FLAGS, dir_fd=root)
                try:
                    os.fchmod(directory, 0o700)
                    for name, data in files.items():
                        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                             0o600, dir_fd=directory)
                        created[group].append(name)
                        try:
                            os.fchmod(descriptor, 0o600)
                            remaining = memoryview(data)
                            while remaining:
                                written = os.write(descriptor, remaining)
                                if written <= 0:
                                    raise OSError
                                remaining = remaining[written:]
                        finally:
                            os.close(descriptor)
                finally:
                    os.close(directory)
        except BaseException:
            # Only this process can write the new 0700 tree. Never traverse input
            # paths or delete any pre-existing output on failure.
            for group, names in created.items():
                directory = os.open(group, _DIR_FLAGS, dir_fd=root)
                try:
                    for name in names:
                        os.unlink(name, dir_fd=directory)
                finally:
                    os.close(directory)
                os.rmdir(group, dir_fd=root)
            os.rmdir(output.name, dir_fd=parent)
            raise


def snapshot_inputs(profile: str, *, source_root: Path = Path('/mnt/pilot'),
                    output_root: Path = Path('/mnt/snapshot/runtime'),
                    include_service_hashes: bool = False) -> None:
    """Snapshot fixed input sets; runtime still validates their actual contents.

    The caller must run as the final runtime UID, with trusted read-only input
    mounts and an empty private output volume. Both path options are for local
    testing; the command-line entry point fixes them to the reference layout.
    """
    try:
        if (profile not in _PROFILE_SECRETS or type(include_service_hashes) is not bool
                or include_service_hashes and profile not in {'api', 'worker', 'provision'}):
            raise ValueError
        source_root, output_root = Path(source_root), Path(output_root)
        if not output_root.is_absolute() or '..' in output_root.parts or output_root.name in {'', '.', '..'}:
            raise ValueError
        groups = {
            'config': {'pilot.json': 65536, 'registry-ca.pem': 262144},
            'secrets': {name: 128 for name in _PROFILE_SECRETS[profile]},
        }
        if profile == 'provision':
            groups['identities'] = {'identities.json': 262144}
        if include_service_hashes:
            groups['auth'] = {'credential-hashes.json': 1048576}
        descriptor = _open_directory(source_root)
        try:
            contents = {group: _read_group(descriptor, group, files) for group, files in groups.items()}
        finally:
            os.close(descriptor)
        _write_snapshot(output_root, contents)
    except (OSError, ValueError, TypeError):
        raise ValueError(_ERROR) from None


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description='Create a one-shot private pilot input snapshot')
    parser.add_argument('profile', choices=tuple(_PROFILE_SECRETS))
    parser.add_argument('--include-service-hashes', action='store_true')
    args = parser.parse_args()
    try:
        snapshot_inputs(args.profile, include_service_hashes=args.include_service_hashes)
    except ValueError:
        parser.exit(1, _ERROR + '\n')


if __name__ == '__main__':
    main()

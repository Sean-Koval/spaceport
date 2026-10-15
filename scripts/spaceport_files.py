"""Bounded operator files. No secret values belong in errors or CLI receipts."""
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

MAX_JSON_BYTES = 1_048_576


class FileError(ValueError):
    def __init__(self):
        super().__init__('The required private operator file is unavailable or invalid.')


def _parent(path):
    path = Path(path)
    if not path.is_absolute():
        path = Path.cwd() / path
    if ('..' in path.parts or any(ord(c) < 32 for c in str(path))
        or path.name.startswith('.spaceport-tmp-')):
        raise FileError()
    opened = []
    try:
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        opened.append(fd)
        for name in path.parts[1:-1]:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            opened.append(fd)
        return opened, fd, path.name
    except (OSError, ValueError):
        for fd in reversed(opened):
            os.close(fd)
        raise FileError() from None


def read_bytes(path, *, limit=MAX_JSON_BYTES, private=False, durable=False, private_parent=False):
    opened = []
    try:
        opened, parent, name = _parent(path)
        if private_parent:
            info = os.fstat(parent)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise FileError()
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        opened.append(fd)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_mode & 0o022
            or before.st_size > limit or (private and (before.st_uid != os.getuid() or before.st_mode & 0o077))):
            raise FileError()
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if durable:
            os.fsync(fd)
            os.fsync(parent)
        after = os.fstat(fd)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        fingerprint = lambda s: (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        if len(data) > limit or len(data) != before.st_size or fingerprint(before) != fingerprint(after) or fingerprint(after) != fingerprint(named):
            raise FileError()
        return bytes(data)
    except (OSError, ValueError):
        raise FileError() from None
    finally:
        for fd in reversed(opened):
            os.close(fd)


def parse_json(data):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise FileError()
            result[key] = value
        return result
    def constant(_):
        raise FileError()
    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise FileError() from None


def read_json(path, *, private=False, durable=False, private_parent=False):
    return parse_json(read_bytes(path, private=private, durable=durable, private_parent=private_parent))


def write_json(path, value):
    """Fsync complete temporary bytes, then atomically publish without replacement.

    Once linked at the final name the file is NEVER removed on uncertainty. A
    concurrent reader may already use it. Query readers fsync file+directory
    themselves before POST, covering the publication/directory-fsync interval.
    """
    opened = []
    temporary = None
    temporary_owned = False
    try:
        data = json.dumps(value, allow_nan=False, ensure_ascii=True, sort_keys=True, indent=2).encode() + b'\n'
        if len(data) > MAX_JSON_BYTES:
            raise FileError()
        opened, parent, name = _parent(path)
        info = os.fstat(parent)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise FileError()
        temporary = '.spaceport-tmp-' + uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
        opened.append(fd)
        temporary_owned = True
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise FileError()
            view = view[written:]
        os.fsync(fd)
        os.link(temporary,name,src_dir_fd=parent,dst_dir_fd=parent,follow_symlinks=False)
        os.unlink(temporary,dir_fd=parent)
        temporary = None
        os.fsync(parent)
    except (OSError, ValueError, TypeError, RecursionError):
        # Only our random temporary name is eligible for cleanup. In particular,
        # a losing writer cannot remove the winner's published operation.
        raise FileError() from None
    finally:
        if temporary is not None and temporary_owned:
            try:
                os.unlink(temporary,dir_fd=parent)
            except OSError:
                pass
        for fd in reversed(opened):
            os.close(fd)


def check_output(path):
    """Preflight before a mutation; creation remains exclusive at receipt write."""
    opened = []
    try:
        opened,parent,name = _parent(path)
        info = os.fstat(parent)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise FileError()
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise FileError()
    except OSError:
        raise FileError() from None
    finally:
        for fd in reversed(opened):
            os.close(fd)

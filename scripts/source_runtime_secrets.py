#!/usr/bin/env python3
"""Five disposable source-gate passwords, never production configuration.

Fresh run-private directory only. Individual read-only container mounts may read
0444 files; two owner-private host directories prevent host traversal. Separate
from legacy Compose secrets and never prints generated values.
"""
import argparse
import os
from pathlib import Path
import secrets
import stat
import sys

NAMES = ('source_bq_consumer','source_bq_fixture','source_bq_observer',
    'source_domo_consumer','source_domo_fixture')
MESSAGE = 'Choose an empty private synthetic source secret directory inside a private runner directory'


def prepare(directory: Path) -> int:
    if not directory.is_absolute():
        raise ValueError(MESSAGE)
    for path in (directory.parent,directory):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
            raise ValueError(MESSAGE)
    if any(directory.iterdir()):
        raise ValueError(MESSAGE)
    # Retain partial synthetic files on failure: caller discards the entire
    # run-private directory. Never unlink a name that might have been replaced.
    descriptor = os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        for name in NAMES:
            fd = os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=descriptor)
            with os.fdopen(fd,'w') as stream:
                stream.write(secrets.token_urlsafe(32)+'\n')
                stream.flush()
                os.fchmod(stream.fileno(),0o444)
                os.fsync(stream.fileno())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return len(NAMES)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Prepare only disposable source-gate passwords')
    parser.add_argument('--secret-dir',required=True,type=Path)
    args = parser.parse_args()
    try:
        count = prepare(args.secret_dir)
    except (ValueError,OSError):
        print(MESSAGE,file=sys.stderr)
        raise SystemExit(1)
    print(f'Prepared {count} disposable source-gate secret files')

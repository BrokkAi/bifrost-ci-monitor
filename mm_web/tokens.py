"""Host-only key storage and read-only dashboard service token minting."""
from __future__ import annotations
import argparse
import hashlib
import hmac
import os
from pathlib import Path
import re
import stat
import monitor

NAME = re.compile(r'^[A-Za-z0-9_-]{1,64}$')


def key(path: Path, *, create=False):
    if create:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(os.urandom(32))
    # Refuse permissive files and symlinks rather than silently using bad keys.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if stat.S_IMODE(info.st_mode) != 0o600 or not stat.S_ISREG(info.st_mode):
            raise ValueError('dashboard key must be a regular mode-0600 file')
        value = handle.read(4096)
    if len(value) < 32:
        raise ValueError('dashboard key is too short')
    return value


def mint(name):
    if not NAME.fullmatch(name):
        raise ValueError('service token name must be 1-64 ASCII letters, digits, underscores or hyphens')
    secret = key(monitor.STATE_DIR / 'mm-web-service.key', create=True)
    return f'mmw_{name}.' + hmac.new(secret, name.encode(), hashlib.sha256).hexdigest()


def verify(value):
    match = re.fullmatch(r'mmw_([A-Za-z0-9_-]{1,64})\.([0-9a-f]{64})', value)
    if not match:
        return None
    try:
        secret = key(monitor.STATE_DIR / 'mm-web-service.key')
    except (OSError, ValueError):
        return None
    expected = hmac.new(secret, match[1].encode(), hashlib.sha256).hexdigest()
    return match[1] if hmac.compare_digest(expected, match[2]) else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('mint').add_argument('name')
    args = parser.parse_args()
    print(mint(args.name))


if __name__ == '__main__':
    main()

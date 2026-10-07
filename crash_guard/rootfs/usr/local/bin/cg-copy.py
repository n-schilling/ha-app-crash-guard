#!/usr/bin/env python3
"""Copies one file of a Docker layer without following any symlink.

cg-copy.py <source> <target>

Every directory on the way is opened with O_NOFOLLOW relative to the one
before, so neither a symlink nor a directory swapped for one between a check
and the copy can lead outside; the file itself must be a regular file. Prints
nothing and exits 1 when it refuses.
"""

import os
import stat
import sys

FLAGS_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FLAGS_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def open_nofollow(path: str) -> int:
    parts = [p for p in path.split("/") if p]
    if not path.startswith("/") or not parts or any(p in (".", "..") for p in parts):
        raise OSError("not a plain absolute path")
    fd = os.open("/", FLAGS_DIR)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, FLAGS_DIR, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return os.open(parts[-1], FLAGS_FILE, dir_fd=fd)
    finally:
        os.close(fd)


def main() -> int:
    source, target = sys.argv[1], sys.argv[2]
    try:
        fd = open_nofollow(source)
    except OSError:
        return 1
    with os.fdopen(fd, "rb") as src:
        info = os.fstat(src.fileno())
        if not stat.S_ISREG(info.st_mode):
            return 1
        out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(out, "wb") as dst:
            while chunk := src.read(1 << 20):
                dst.write(chunk)
        os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
    return 0


if __name__ == "__main__":
    sys.exit(main())

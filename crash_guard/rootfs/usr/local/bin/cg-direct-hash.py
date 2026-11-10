#!/usr/bin/env python3
"""Hashes files straight from the drive, past the page cache (O_DIRECT).

Reads NUL-separated paths on stdin and prints "<sha256>  <path>" per file, as
sha256sum does. A file that cannot be read this way is left out, so it never
makes it into a reference. Runs in the unprivileged hash helper; every
directory on the way is opened without following symlinks.

cg-direct-hash.py < paths
"""

import hashlib
import mmap
import os
import stat
import sys

CHUNK = 1 << 20  # a multiple of every logical block size O_DIRECT may ask for
FLAGS_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FLAGS_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_DIRECT


def open_direct(path: str) -> int:
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


def direct_sha256(path: str, buf: mmap.mmap) -> str:
    fd = open_direct(path)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        digest = hashlib.sha256()
        offset = 0
        while True:
            n = os.preadv(fd, [buf], offset)
            digest.update(buf[:n])
            offset += n
            if n < CHUNK:
                return digest.hexdigest()
    finally:
        os.close(fd)


def main() -> int:
    buf = mmap.mmap(-1, CHUNK)  # page aligned, as O_DIRECT requires
    out = sys.stdout
    for raw in sys.stdin.buffer.read().split(b"\0"):
        if not raw:
            continue
        path = raw.decode("utf-8", errors="surrogateescape")
        try:
            out.write(f"{direct_sha256(path, buf)}  {path}\n")
        except (OSError, ValueError):
            continue
    return 0


if __name__ == "__main__":
    sys.exit(main())

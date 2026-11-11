#!/usr/bin/env python3
"""Copies one file without following any symlink.

cg-copy.py [--max BYTES] [--tail BYTES] [--single-link] <source> <target>

Every directory on the way is opened with O_NOFOLLOW relative to the one
before, so neither a symlink nor a directory swapped for one between a check
and the copy can lead outside; the file itself must be a regular file, opened
without blocking (a FIFO in its place cannot hold the caller up).

--max refuses a file larger than BYTES and never copies more than that.
--tail copies only the last BYTES of the file. --single-link refuses a file
with more than one name, which could be a hard link to a secret. Prints
nothing and exits 1 when it refuses.

open_nofollow is shared with cg-pfn.py, which loads this file.
"""

import argparse
import os
import stat
import sys

FLAGS_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FLAGS_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def open_nofollow(path: str, flags: int = FLAGS_FILE) -> int:
    """Opens an absolute path without following a symlink anywhere on it."""
    parts = [p for p in path.split("/") if p]
    if not path.startswith("/") or not parts or any(p in (".", "..") for p in parts):
        raise OSError("not a plain absolute path")
    fd = os.open("/", FLAGS_DIR)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, FLAGS_DIR, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return os.open(parts[-1], flags, dir_fd=fd)
    finally:
        os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--max", type=int)
    parser.add_argument("--tail", type=int)
    parser.add_argument("--single-link", action="store_true")
    parser.add_argument("source")
    parser.add_argument("target")
    try:
        args = parser.parse_args()
    except SystemExit:
        return 1
    try:
        fd = open_nofollow(args.source)
    except OSError:
        return 1
    with os.fdopen(fd, "rb") as src:
        info = os.fstat(src.fileno())
        if not stat.S_ISREG(info.st_mode):
            return 1
        if args.single_link and info.st_nlink != 1:
            return 1
        if args.max is not None and info.st_size > args.max:
            return 1
        left = info.st_size if args.max is None else min(info.st_size, args.max)
        if args.tail is not None and info.st_size > args.tail:
            src.seek(info.st_size - args.tail)
            left = args.tail
        out = os.open(args.target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
        with os.fdopen(out, "wb") as dst:
            # Never more than the size seen at the start, even if the file grows
            while left > 0 and (chunk := src.read(min(left, 1 << 20))):
                dst.write(chunk)
                left -= len(chunk)
        os.utime(args.target, ns=(info.st_atime_ns, info.st_mtime_ns))
    return 0


if __name__ == "__main__":
    sys.exit(main())

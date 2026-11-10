#!/usr/bin/env python3
"""Finds the physical RAM address of the affected pages of corrupted files.

Runs in the privileged helper container (PFNs from /proc/self/pagemap need
CAP_SYS_ADMIN). Compares every 4K page of the file in the page cache (mmap)
with an O_DIRECT read from the drive and rereads differing pages several
times to detect unstable cells. Never writes to the files.

cg-pfn.py <mismatch file> <badpages.log> <occasion>

Prints "pages=<found> skipped=<files that could not be read>". A page whose
physical address cannot be read (not resident any more) is logged with
pfn=unknown, which the guard service never takes for a defective page.
"""

import ctypes
import hashlib
import importlib.util
import mmap
import os
import stat
import struct
import sys
import time

PAGE = 4096
REREADS = 5
# The paths in the mismatch file come from the Home Assistant container's
# layers, including its upper layer, which code running in Home Assistant can
# write to. So this privileged helper opens only regular files inside the
# Docker layers, never through a symlink anywhere on the path and without
# blocking, and reads the drive through the very file it opened.
LAYERS = "/mnt/data/docker/overlay2/"

_spec = importlib.util.spec_from_file_location(
    "cg_copy", os.path.join(os.path.dirname(os.path.abspath(__file__)), "cg-copy.py"))
_copy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_copy)
open_nofollow = _copy.open_nofollow


def open_layer_file(path: str) -> int | None:
    """A file descriptor of a regular file in the Docker layers, else None."""
    if not path.startswith(LAYERS):
        return None
    try:
        fd = open_nofollow(path)
    except OSError:
        return None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return None
    return fd


def pfn_of(addr: int, pagemap) -> int:
    pagemap.seek((addr // PAGE) * 8)
    entry = struct.unpack("Q", pagemap.read(8))[0]
    if not entry >> 63:  # not in RAM
        return 0
    return entry & ((1 << 55) - 1)


def direct_read(fd: int, size: int) -> bytes:
    """The file of fd as it is on the drive. Opened again through /proc so it
    is the same file, not whatever the path names by now."""
    length = (size + PAGE - 1) // PAGE * PAGE
    buf = mmap.mmap(-1, length)  # page aligned, as O_DIRECT requires
    dfd = os.open(f"/proc/self/fd/{fd}", os.O_RDONLY | os.O_DIRECT | os.O_NONBLOCK)
    try:
        a, b = os.fstat(fd), os.fstat(dfd)
        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
            raise OSError("the file changed while it was opened")
        os.preadv(dfd, [buf], 0)
    finally:
        os.close(dfd)
    return bytes(buf[:size])


def bitflips(a: bytes, b: bytes) -> int:
    return sum(bin(x ^ y).count("1") for x, y in zip(a, b, strict=False))


def locate(path: str, fd: int, pagemap, out, stamp: str, reason: str) -> int:
    """Logs the differing pages of one file; returns how many."""
    size = os.fstat(fd).st_size
    if size == 0:
        return 0
    good = direct_read(fd, size)
    found = 0
    # MAP_PRIVATE + PROT_WRITE only so ctypes yields an address; nothing is
    # ever written, the pages stay those of the page cache
    m = mmap.mmap(fd, size, flags=mmap.MAP_PRIVATE, prot=mmap.PROT_READ | mmap.PROT_WRITE)
    anchor = ctypes.c_char.from_buffer(m)
    try:
        base = ctypes.addressof(anchor)
        for page in range((size + PAGE - 1) // PAGE):
            lo, hi = page * PAGE, min(size, (page + 1) * PAGE)
            cached = m[lo:hi]
            if cached == good[lo:hi]:
                continue
            pfn = pfn_of(base + lo, pagemap)
            variants = set()
            for _ in range(REREADS):
                variants.add(hashlib.sha256(m[lo:hi]).digest())
                time.sleep(0.2)
            where = f"pfn=0x{pfn:x} phys=0x{pfn * PAGE:x}" if pfn else "pfn=unknown phys=unknown"
            out.write(f"{stamp} {reason} {where} "
                      f"bits={bitflips(cached, good[lo:hi])} variants={len(variants)}/{REREADS} "
                      f"page={page} {path}\n")
            found += 1
    finally:
        del anchor
        m.close()
    return found


def main() -> None:
    mismatch, log, reason = sys.argv[1:4]
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    found = skipped = 0
    # Both files lie in the app's private /data; still no symlinks, no special
    # files (O_NONBLOCK: a FIFO must not block this privileged helper)
    mfd = os.open(mismatch, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(mfd).st_mode):
        raise SystemExit("mismatch file is not a regular file")
    lfd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    with open("/proc/self/pagemap", "rb") as pagemap, os.fdopen(lfd, "a") as out, \
            os.fdopen(mfd) as lines:
        for line in lines:
            fields = line.split()
            if not fields:
                continue
            fd = open_layer_file(fields[0])
            if fd is None:
                continue
            try:
                found += locate(fields[0], fd, pagemap, out, stamp, reason)
            except (OSError, ValueError, BufferError):
                # Gone, shrunk or not readable straight from the drive: the
                # other files still count
                skipped += 1
            finally:
                os.close(fd)
    print(f"pages={found} skipped={skipped}")


if __name__ == "__main__":
    main()

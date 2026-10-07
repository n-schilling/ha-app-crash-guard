#!/usr/bin/env python3
"""Finds the physical RAM address of the affected pages of corrupted files.

Runs in the privileged helper container (PFNs from /proc/self/pagemap need
CAP_SYS_ADMIN). Compares every 4K page of the file in the page cache (mmap)
with an O_DIRECT read from the drive and rereads differing pages several
times to detect unstable cells. Never writes to the files.

cg-pfn.py <mismatch file> <badpages.log> <occasion>
"""
import hashlib
import mmap
import os
import stat
import struct
import sys
import time
import ctypes

PAGE = 4096
REREADS = 5
# The mismatch file lies in /share, where other apps can write; this
# privileged helper only ever opens regular files inside the Docker layers
LAYERS = "/mnt/data/docker/overlay2/"


def layer_file(path: str) -> bool:
    real = os.path.realpath(path)
    return real == path and real.startswith(LAYERS) and os.path.isfile(real)


def pfn_of(addr: int, pagemap) -> int:
    pagemap.seek((addr // PAGE) * 8)
    entry = struct.unpack("Q", pagemap.read(8))[0]
    if not entry >> 63:  # not in RAM
        return 0
    return entry & ((1 << 55) - 1)


def direct_read(path: str, size: int) -> bytes:
    length = (size + PAGE - 1) // PAGE * PAGE
    buf = mmap.mmap(-1, length)  # page aligned, as O_DIRECT requires
    fd = os.open(path, os.O_RDONLY | os.O_DIRECT | os.O_NOFOLLOW)
    try:
        os.preadv(fd, [buf], 0)
    finally:
        os.close(fd)
    return bytes(buf[:size])


def bitflips(a: bytes, b: bytes) -> int:
    return sum(bin(x ^ y).count("1") for x, y in zip(a, b))


def main() -> None:
    mismatch, log, reason = sys.argv[1:4]
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    found = 0
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
            if not fields or not layer_file(fields[0]):
                continue
            path = fields[0]
            size = os.path.getsize(path)
            if size == 0:
                continue
            good = direct_read(path, size)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            # MAP_PRIVATE + PROT_WRITE only so ctypes yields an address; nothing
            # is ever written, the pages stay those of the page cache
            m = mmap.mmap(fd, size, flags=mmap.MAP_PRIVATE, prot=mmap.PROT_READ | mmap.PROT_WRITE)
            anchor = ctypes.c_char.from_buffer(m)
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
                out.write(f"{stamp} {reason} pfn=0x{pfn:x} phys=0x{pfn * PAGE:x} "
                          f"bits={bitflips(cached, good[lo:hi])} variants={len(variants)}/{REREADS} "
                          f"page={page} {path}\n")
                found += 1
            del anchor
            m.close()
            os.close(fd)
    print(f"pages={found}")


if __name__ == "__main__":
    main()

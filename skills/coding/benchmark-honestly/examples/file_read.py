#!/usr/bin/env python3
"""Read a big file, to show how the OS file cache changes the answer.

    python3 file_read.py make data.bin 256        # create a 256 MiB file
    python3 file_read.py read data.bin 65536       # read it with a 64 KiB buffer
    python3 file_read.py read-nocache data.bin 65536  # macOS: bypass the cache (F_NOCACHE)
    python3 file_read.py evict data.bin            # Linux: drop this file from the cache

The first read of a file comes from disk. Later reads come from memory (the
page cache) and are much faster. Whichever variant runs first pays for the disk.
"""
import fcntl
import os
import sys

F_NOCACHE = getattr(fcntl, "F_NOCACHE", 48)  # macOS value


def make(path, mib):
    block = os.urandom(1 << 20)
    with open(path, "wb") as f:
        for _ in range(int(mib)):
            f.write(block)
        f.flush()
        os.fsync(f.fileno())


def read(path, bufsize, nocache=False):
    fd = os.open(path, os.O_RDONLY)
    if nocache:
        fcntl.fcntl(fd, F_NOCACHE, 1)
    total = 0
    while True:
        b = os.read(fd, bufsize)
        if not b:
            break
        total += len(b)
    os.close(fd)
    print(total)


def evict(path):
    fd = os.open(path, os.O_RDONLY)
    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    os.close(fd)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "make":
        make(sys.argv[2], sys.argv[3])
    elif cmd == "read":
        read(sys.argv[2], int(sys.argv[3]))
    elif cmd == "read-nocache":
        read(sys.argv[2], int(sys.argv[3]), nocache=True)
    elif cmd == "evict":
        evict(sys.argv[2])

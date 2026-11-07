#!/usr/bin/env python3
"""Pass/fail oracle for "it looks different": compare two PNG screenshots.

Pure standard library PNG reader (8-bit grey, RGB, palette, grey+alpha, RGBA;
not interlaced), so it runs anywhere. Small differences from font smoothing
or anti-aliasing are ignored with --tolerance (per channel) and
--max-diff-ratio (share of pixels allowed to differ). Blinking cursors,
clocks and avatars can be masked out with --mask x,y,w,h.

  img_diff.py shot.png reference.png --tolerance 8 --max-diff-ratio 0.001 --diff-out diff.png

Exit codes: 0 same, 1 different, 125 a file is missing or cannot be read.
Large screenshots take a few seconds because it is pure Python.
"""
import argparse
import struct
import sys
import zlib


def read_png(path):
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, plte, trns = 8, [], None, None
    while pos < len(data):
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            w, h, depth, color, _c, _f, interlace = struct.unpack(">IIBBBBB", chunk)
        elif ctype == b"PLTE":
            plte = chunk
        elif ctype == b"tRNS":
            trns = chunk
        elif ctype == b"IDAT":
            idat.append(chunk)
        elif ctype == b"IEND":
            break
    if depth != 8 or interlace:
        raise ValueError("only 8-bit, non-interlaced PNGs are supported (re-save the screenshot)")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    raw = zlib.decompress(b"".join(idat))
    stride = w * channels
    out = bytearray(h * stride)
    prev = bytearray(stride)
    i = 0
    for y in range(h):
        ftype = raw[i]
        line = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        bpp = channels
        if ftype == 1:
            for x in range(bpp, stride):
                line[x] = (line[x] + line[x - bpp]) & 255
        elif ftype == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 255
        elif ftype == 3:
            for x in range(stride):
                left = line[x - bpp] if x >= bpp else 0
                line[x] = (line[x] + ((left + prev[x]) >> 1)) & 255
        elif ftype == 4:
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                b = prev[x]
                c = prev[x - bpp] if x >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[x] = (line[x] + pred) & 255
        out[y * stride:(y + 1) * stride] = line
        prev = line
    # convert to RGBA
    rgba = bytearray(w * h * 4)
    for k in range(w * h):
        px = out[k * channels:(k + 1) * channels]
        if color == 0:
            r = g = b = px[0]; al = 255
        elif color == 2:
            r, g, b = px; al = 255
        elif color == 3:
            r, g, b = plte[px[0] * 3:px[0] * 3 + 3]
            al = trns[px[0]] if trns and px[0] < len(trns) else 255
        elif color == 4:
            r = g = b = px[0]; al = px[1]
        else:
            r, g, b, al = px
        rgba[k * 4:k * 4 + 4] = bytes((r, g, b, al))
    return w, h, rgba


def write_png(path, w, h, rgba):
    rows = b"".join(b"\x00" + bytes(rgba[y * w * 4:(y + 1) * w * 4]) for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image")
    ap.add_argument("reference")
    ap.add_argument("--tolerance", type=int, default=8, help="max difference per channel, 0..255 (default 8)")
    ap.add_argument("--max-diff-ratio", type=float, default=0.001,
                    help="share of pixels allowed to differ (default 0.001 = 0.1%%)")
    ap.add_argument("--mask", action="append", default=[], help="x,y,w,h region to ignore (repeatable)")
    ap.add_argument("--diff-out", help="write a PNG with differing pixels in red")
    a = ap.parse_args()
    try:
        w1, h1, p1 = read_png(a.image)
        w2, h2, p2 = read_png(a.reference)
    except (OSError, ValueError, KeyError, struct.error, zlib.error) as e:
        print("img_diff: cannot read images: %s" % e, file=sys.stderr)
        sys.exit(125)
    if (w1, h1) != (w2, h2):
        print("img_diff: size differs %dx%d vs %dx%d" % (w1, h1, w2, h2), file=sys.stderr)
        sys.exit(1)
    masks = [tuple(int(v) for v in m.split(",")) for m in a.mask]

    def masked(x, y):
        return any(mx <= x < mx + mw and my <= y < my + mh for mx, my, mw, mh in masks)

    diff = bytearray(p2) if a.diff_out else None
    bad = 0
    for k in range(w1 * h1):
        o = k * 4
        if max(abs(p1[o + c] - p2[o + c]) for c in range(4)) > a.tolerance:
            if masks and masked(k % w1, k // w1):
                continue
            bad += 1
            if diff is not None:
                diff[o:o + 4] = b"\xff\x00\x00\xff"
    ratio = bad / float(w1 * h1)
    print("img_diff: %d of %d pixels differ (%.4f%%)" % (bad, w1 * h1, 100 * ratio), file=sys.stderr)
    if diff is not None:
        write_png(a.diff_out, w1, h1, diff)
    sys.exit(1 if ratio > a.max_diff_ratio else 0)


if __name__ == "__main__":
    main()

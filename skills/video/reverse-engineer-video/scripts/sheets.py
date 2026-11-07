"""Pictures for the breakdown: a contact sheet (one frame per shot), a detail sheet (start, middle and
end of each shot, to see camera moves), and a timeline strip (shots, loudness, onsets, beats, cuts).
Images are put together in numpy and labelled with ffmpeg drawtext, so no imaging library is needed."""
import os, tempfile
import numpy as np
from common import run, font_file, esc, tc, write_ppm, grab

BG = np.array([18, 18, 20], np.uint8)


def _save(img, path, texts):
    """Write img to path (png/jpg), drawing texts = [(x, y, size, colour, text)] on top."""
    fd, ppm = tempfile.mkstemp(suffix=".ppm")
    os.close(fd)
    write_ppm(ppm, img)
    font = font_file()
    vf = []
    if font and texts:
        ff = esc(font)
        for x, y, size, col, t in texts:
            vf.append(f"drawtext=fontfile='{ff}':text='{esc(t)}':x={int(x)}:y={int(y)}:fontsize={int(size)}:fontcolor={col}:expansion=none")
    args = ["-y", "-i", ppm]
    if vf:
        script = ppm + ".txt"
        with open(script, "w") as f:
            f.write(",\n".join(vf))
        args += ["-filter_script:v", script]
    q = ["-q:v", "3"] if str(path).lower().endswith((".jpg", ".jpeg")) else []
    try:
        run(args + q + ["-frames:v", "1", "-update", "1", str(path)])
    finally:
        for p in (ppm, ppm + ".txt"):
            if os.path.exists(p):
                os.remove(p)
    return font is not None


def thumb_size(w, h, long_side=384):
    if w >= h:
        return long_side, int(round(long_side * h / w / 2) * 2)
    return int(round(long_side * w / h / 2) * 2), long_side


def contact_sheet(src, fps, shots, vw, vh, out_prefix, per_page=24):
    """One middle frame per shot, labelled with number, time, length and camera move. Returns file paths."""
    tw, th = thumb_size(vw, vh, 384 if vw >= vh else 300)
    cols = 4 if vw >= vh else 6
    lab = 44
    mids = [s["mid_frame"] for s in shots]
    imgs, got = grab(src, fps, mids, tw, th)
    by = dict(zip(got, imgs))
    paths = []
    for p0 in range(0, len(shots), per_page):
        page = shots[p0:p0 + per_page]
        rows = (len(page) + cols - 1) // cols
        pad = 8
        W, H = cols * (tw + pad) + pad, rows * (th + lab + pad) + pad
        img = np.empty((H, W, 3), np.uint8); img[:] = BG
        texts = []
        for k, s in enumerate(page):
            r, c = divmod(k, cols)
            x, y = pad + c * (tw + pad), pad + r * (th + lab + pad)
            fr = by.get(s["mid_frame"])
            if fr is not None:
                img[y:y + th, x:x + tw] = fr
            texts.append((x + 4, y + th + 5, 15, "white", f"#{s['index']}  {tc(s['start'])}  {s['duration']:.2f}s"))
            texts.append((x + 4, y + th + 24, 13, "0xbbbbbb", s["camera"]["label"][:48]))
        path = f"{out_prefix}{'' if len(shots) <= per_page else '_%02d' % (p0 // per_page + 1)}.jpg"
        _save(img, path, texts)
        paths.append(path)
    return paths


def detail_sheet(src, fps, shots, vw, vh, out_prefix, per_page=10):
    """Rows of start / middle / end frames per shot with the measured numbers beside them."""
    tw, th = thumb_size(vw, vh, 256 if vw >= vh else 200)
    want = []
    for s in shots:
        a, b = s.get("clean_start", s["start_frame"]), s.get("clean_end", s["end_frame"]) - 1
        inset = min(2, max(0, (b - a) // 4))
        want += [a + inset, s["mid_frame"], b - inset]
    imgs, got = grab(src, fps, want, tw, th)
    by = dict(zip(got, imgs))
    paths = []
    text_w, pad = 420, 6
    for p0 in range(0, len(shots), per_page):
        page = shots[p0:p0 + per_page]
        W = pad + 3 * (tw + pad) + text_w
        H = pad + len(page) * (th + pad)
        img = np.empty((H, W, 3), np.uint8); img[:] = BG
        texts = []
        for r, s in enumerate(page):
            y = pad + r * (th + pad)
            a, b = s.get("clean_start", s["start_frame"]), s.get("clean_end", s["end_frame"]) - 1
            inset = min(2, max(0, (b - a) // 4))
            for c, f in enumerate([a + inset, s["mid_frame"], b - inset]):
                x = pad + c * (tw + pad)
                if f in by:
                    img[y:y + th, x:x + tw] = by[f]
            cam = s["camera"]
            tx = pad + 3 * (tw + pad) + 4
            lines = [
                (16, "white", f"#{s['index']}  {tc(s['start'])} - {tc(s['end'])}  ({s['duration']:.2f}s)  in: {s['transition_in']}"),
                (14, "0xffd75e", cam["label"]),
                (13, "0xbbbbbb", f"pan {cam.get('pan_pct_width_per_s', 0)}%W/s  tilt {cam.get('tilt_pct_height_per_s', 0)}%H/s"),
                (13, "0xbbbbbb", f"zoom x{cam.get('zoom_total', 1)}  roll {cam.get('roll_total_deg', 0)} deg  shake {cam.get('shake_pct_width', 0)}%W"),
                (13, "0xbbbbbb", f"parallax {cam.get('parallax', 0)}  speed {cam.get('speed_profile') or '-'}"),
            ]
            for i, (sz, col, t) in enumerate(lines):
                if i * 20 + 18 < th:
                    texts.append((tx, y + 4 + i * 20, sz, col, t))
        path = f"{out_prefix}{'' if len(shots) <= per_page else '_%02d' % (p0 // per_page + 1)}.jpg"
        _save(img, path, texts)
        paths.append(path)
    return paths


def timeline(frames, fps, duration, shots, bounds_info, loud, onsets, beats, on_beat, out_path):
    """Wide strip: shot thumbnails along time, cut lines (green on a beat or onset, red off),
    dissolves shaded, loudness curve, onset and beat ticks, and a seconds ruler."""
    W = int(min(max(duration * 120, 1200), 4000))
    px = W / max(duration, 1e-6)
    band, loud_h, tick_h, ruler = 96, 90, 34, 26
    H = band + loud_h + tick_h + ruler
    img = np.empty((H, W, 3), np.uint8); img[:] = BG
    fh, fw = frames.shape[1:3]
    tw = int(round(fw * band / fh))
    for s in shots:
        x0, x1 = int(s["start"] * px), max(int(s["end"] * px), int(s["start"] * px) + 1)
        fr = frames[min(s["mid_frame"], len(frames) - 1)]
        yi = (np.arange(band) * fh / band).astype(int)
        xi = (np.arange(tw) * fw / tw).astype(int)
        th = fr[yi][:, xi]
        width = x1 - x0
        if width >= tw:
            reps = int(np.ceil(width / tw))
            th = np.tile(th, (1, reps, 1))[:, :width]
        else:
            off = (tw - width) // 2
            th = th[:, off:off + width]
        img[0:band, x0:x1] = th
    # loudness
    y0 = band
    t = np.asarray(loud.get("t") or [])
    M = np.asarray(loud.get("momentary") or [])
    img[y0:y0 + loud_h] = (28, 28, 34)
    if len(t):
        xs = np.arange(W) / px
        mv = np.interp(xs, t, M)
        hgt = np.clip((mv + 60) / 60, 0, 1) * (loud_h - 4)
        for x in range(W):
            hh = int(hgt[x])
            if hh > 0:
                img[y0 + loud_h - hh:y0 + loud_h, x] = (70, 130, 200)
    # onset and beat ticks
    y1 = band + loud_h
    img[y1:y1 + tick_h] = (24, 24, 28)
    for o in onsets:
        x = int(o * px)
        if 0 <= x < W:
            img[y1 + tick_h // 2:y1 + tick_h, x] = (150, 150, 150)
    for b in beats:
        x = int(b * px)
        if 0 <= x < W:
            img[y1:y1 + tick_h // 2, max(0, x - 1):x + 1] = (255, 215, 94)
    # cuts and dissolves across all bands
    for s, hit in zip(shots[1:], on_beat):
        x = int(s["start"] * px)
        col = (60, 220, 110) if hit else (235, 70, 70)
        if s["transition_in"] != "cut":
            half = max(1, int(s.get("transition_frames", 0) / fps * px / 2))
            seg = img[0:y1 + tick_h, max(0, x - half):min(W, x + half)].astype(np.int32)
            img[0:y1 + tick_h, max(0, x - half):min(W, x + half)] = ((seg + np.array(col)) // 2).astype(np.uint8)
        img[0:y1 + tick_h, max(0, x - 1):min(W, x + 2)] = col
    # ruler
    y2 = y1 + tick_h
    step = 1 if duration <= 30 else 5 if duration <= 150 else 10
    texts = []
    for sec in np.arange(0, duration + 1e-6, step):
        x = int(sec * px)
        img[y2:y2 + 6, max(0, min(W - 1, x))] = (200, 200, 200)
        texts.append((min(x + 2, W - 30), y2 + 7, 13, "0xcccccc", f"{int(sec)}s"))
    for s in shots:
        x0, x1 = s["start"] * px, s["end"] * px
        if x1 - x0 > 28:
            texts.append((x0 + 4, 4, 14, "white", f"#{s['index']}"))
    texts.append((6, band + 4, 12, "0xcccccc", "loudness (momentary LUFS, -60 to 0)"))
    texts.append((6, y1 + 2, 11, "0xffd75e", "beats"))
    texts.append((6, y1 + tick_h - 14, 11, "0x999999", "onsets"))
    _save(img, out_path, texts)
    return out_path

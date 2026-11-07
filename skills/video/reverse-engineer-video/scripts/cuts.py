"""Shot boundary detection on small decoded frames: hard cuts, flash frames, dissolves and fades.

Signals (all per frame i, comparing with frame i-1):
  sad[i]  mean absolute luma difference in 0..100. This is the same number ffmpeg's `scdet` and
          `select='gt(scene,x)'` start from.
  hist[i] colour histogram distance in 0..1 (half the L1 distance of 8x8x8 RGB histograms).
          It ignores motion inside a shot, so it backs up sad on busy footage.
A hard cut is a frame whose sad stands far above its neighbours (adaptive ratio), or whose colours
change completely. A dissolve has no single spike; instead the middle frame of a short window is
close to the average of the frames at both ends (the "linearity test").
"""
import numpy as np


def luma(frames):
    f = frames.astype(np.float32)
    return 0.299 * f[..., 0] + 0.587 * f[..., 1] + 0.114 * f[..., 2]


def histograms(frames):
    q = (frames >> 5).astype(np.int32)  # 8 levels per channel
    idx = (q[..., 0] << 6) | (q[..., 1] << 3) | q[..., 2]
    n = frames.shape[0]
    flat = idx.reshape(n, -1)
    h = np.zeros((n, 512), np.float32)
    for i in range(n):
        h[i] = np.bincount(flat[i], minlength=512)
    return h / flat.shape[1]


def signals(frames):
    y = luma(frames)
    h = histograms(frames)
    sad = np.zeros(len(y), np.float32)
    hist = np.zeros(len(y), np.float32)
    sad[1:] = np.abs(np.diff(y, axis=0)).mean(axis=(1, 2)) / 2.55
    hist[1:] = 0.5 * np.abs(np.diff(h, axis=0)).sum(axis=1)
    return y, h, sad, hist


def hard_cuts(sad, hist, fps, ratio=3.0, min_sad=5.0, color_cut=0.5, window=None):
    """Frame indices where a new shot starts, with a confidence score."""
    n = len(sad)
    w = window or max(2, int(round(fps * 0.2)))
    out = []
    for i in range(1, n):
        lo, hi = max(1, i - w), min(n, i + w + 1)
        nb = np.concatenate([sad[lo:i], sad[i + 1:hi]])
        base = float(np.median(nb)) if nb.size else 0.0
        r = sad[i] / (base + 1.0)
        spike = sad[i] >= min_sad and r >= ratio and sad[i] >= sad[max(i - 1, 0)] and sad[i] >= sad[min(i + 1, n - 1)]
        colour = hist[i] >= color_cut and sad[i] >= min_sad * 0.6 and hist[i] >= hist[max(i - 1, 0)] and hist[i] >= hist[min(i + 1, n - 1)]
        if spike or colour:
            out.append((i, float(r), float(sad[i]), float(hist[i])))
    return out


def merge_flashes(cuts, y, h, fps, min_shot):
    """Two cuts very close together are either a flash frame (same shot before and after) or a real
    very short shot. Flash: frames either side match each other. Otherwise keep both if the gap is at
    least 2 frames, else keep the stronger one."""
    cuts = sorted(cuts)
    keep, flashes = [], []
    i = 0
    while i < len(cuts):
        c = cuts[i]
        if i + 1 < len(cuts) and cuts[i + 1][0] - c[0] < min_shot:
            d = cuts[i + 1]
            before, after = c[0] - 1, d[0]
            same = 0.5 * np.abs(h[before] - h[after]).sum() < 0.2 and np.abs(y[before] - y[after]).mean() / 2.55 < 8
            if same:
                flashes.append({"frame": c[0], "frames": d[0] - c[0]})
                i += 2
                continue
            if d[0] - c[0] < 2:
                keep.append(max(c, d, key=lambda x: x[2]))
                i += 2
                continue
        keep.append(c)
        i += 1
    return keep, flashes


def dissolves(y, h, sad, hard, fps, widths=None, max_lin=0.45, min_change=4.0, min_hist=0.18):
    """Gradual transitions by the linearity test. Returns dicts with centre frame, length, kind."""
    n = len(y)
    # a 4x smaller copy is plenty for whole-frame averages and keeps this fast on long clips
    hh, ww = (y.shape[1] // 4) * 4, (y.shape[2] // 4) * 4
    y = y[:, :hh, :ww].reshape(n, hh // 4, 4, ww // 4, 4).mean(axis=(2, 4))
    widths = widths or sorted({max(2, int(round(fps * s))) for s in (0.1, 0.17, 0.27, 0.4, 0.6)})
    hard_set = np.zeros(n, bool)
    for c in hard:
        hard_set[max(0, c - 1): c + 1] = True
    best = np.full(n, np.inf, np.float32)
    bestw = np.zeros(n, np.int32)
    for w in widths:
        for i in range(w, n - w):
            if hard_set[i - w + 1: i + w + 1].any():
                continue
            a, b, m = y[i - w], y[i + w], y[i]
            change = np.abs(a - b).mean() / 2.55
            if change < min_change:
                continue
            if 0.5 * np.abs(h[i - w] - h[i + w]).sum() < min_hist:
                continue
            # gradual: no single step carries most of the change
            if sad[i - w + 1: i + w + 1].max() > 0.6 * change + 1.0:
                continue
            lin = np.abs(m - 0.5 * (a + b)).mean() / 2.55 / (0.5 * change)
            # check the quarter points too, which rejects zooms and pans that happen to pass at the centre
            q = w // 2
            if q >= 1:
                l1 = np.abs(y[i - q] - (0.75 * a + 0.25 * b)).mean() / 2.55 / (0.5 * change)
                l2 = np.abs(y[i + q] - (0.25 * a + 0.75 * b)).mean() / 2.55 / (0.5 * change)
                lin = max(lin, l1, l2)
            if lin < best[i]:
                best[i], bestw[i] = lin, w
    cand = best < max_lin
    out = []
    i = 0
    while i < n:
        if cand[i]:
            j = i
            while j + 1 < n and cand[j + 1]:
                j += 1
            k = i + int(np.argmin(best[i:j + 1]))
            w = int(bestw[k])
            dark_a = y[max(k - w, 0)].mean() < 20
            dark_b = y[min(k + w, n - 1)].mean() < 20
            kind = "fade from black" if dark_a else "fade to black" if dark_b else "dissolve"
            # length: span of the elevated step-difference run around the centre
            base = np.median(sad) + 0.5
            s = k
            while s > 1 and sad[s] > base and k - s < 3 * w:
                s -= 1
            e = k
            while e < n - 1 and sad[e + 1] > base and e - k < 3 * w:
                e += 1
            out.append({"frame": k, "frames": int(max(e - s, 2 * w)), "kind": kind, "linearity": round(float(best[k]), 3)})
            i = j + 1
        else:
            i += 1
    return out


def detect(frames, fps, sensitivity=1.0, min_shot_s=0.1):
    """Shot boundaries. sensitivity > 1 finds more cuts, < 1 fewer."""
    y, h, sad, hist = signals(frames)
    min_shot = max(2, int(round(fps * min_shot_s)))
    raw = hard_cuts(sad, hist, fps, ratio=3.0 / sensitivity, min_sad=5.0 / sensitivity, color_cut=0.5 / sensitivity)
    hard, flashes = merge_flashes(raw, y, h, fps, min_shot)
    hard_idx = [c[0] for c in hard]
    soft = dissolves(y, h, sad, hard_idx, fps, max_lin=0.45 * sensitivity)
    bounds = [{"frame": c[0], "kind": "cut", "frames": 0, "score": round(c[1], 2), "sad": round(c[2], 2), "hist": round(c[3], 3)}
              for c in hard]
    for d in soft:
        if all(abs(d["frame"] - b["frame"]) > d["frames"] for b in bounds):
            bounds.append({"frame": d["frame"], "kind": d["kind"], "frames": d["frames"], "linearity": d["linearity"]})
    bounds.sort(key=lambda b: b["frame"])
    return bounds, flashes, {"sad": sad, "hist": hist, "luma": y}

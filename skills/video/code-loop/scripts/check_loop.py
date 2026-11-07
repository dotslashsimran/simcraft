"""Check a looping video for the three things that ruin a loop: a visible seam, freezing, and blinking.

Usage:
  python3 check_loop.py loop.mp4 [--heat blink.png] [--freeze-sec 0.5] [--still 0.6] [--no-fail]

Needs python3 (3.9+) and numpy. Set $FFMPEG if ffmpeg is not on the PATH.

1. Seam.
   a) Exact: if render_loop.mjs left <name>.seam-first.png and <name>.seam-next.png beside the video
      (the frame at t = 0 and the frame one step after the last, at t = LOOP), they are compared pixel
      by pixel. A pure, periodic page gives 0.
   b) In the video: the step from the last frame back to the first frame should look like every other
      step. The change in each 40 px tile is compared with the same tile's median change over nearby
      steps. A jump at the seam (or anywhere else: a pop) stands out as a tile that suddenly changes
      far more than usual.
2. Freeze: any span where the picture barely changes for --freeze-sec or longer (a held frame reads as
   a broken video). The loop wraps, so a hold across the seam is caught too. Also lists duplicated
   single frames (stutter), which usually mean a frame rate mismatch.
3. Blink: pixels whose brightness spikes up then down (or down then up) within one frame, which is
   flicker, not motion: smooth motion changes a pixel in one direction across neighbouring frames.
   Reports how many pixels blink in more than 5% of frames, the worst moments, and optionally saves a
   heatmap over a frame.
Exits 1 if a check fails, unless --no-fail.
"""
import argparse, os, re, shutil, subprocess, sys
import numpy as np

FF = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"


def info(path):
    err = subprocess.run([FF, "-hide_banner", "-i", path], capture_output=True, text=True).stderr
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
    f = re.search(r"([\d.]+) fps", err) or re.search(r"([\d.]+) tbr", err)
    if not m:
        sys.exit(f"could not read video size from {path}")
    return int(m.group(1)), int(m.group(2)), float(f.group(1)) if f else 60.0


def decode(path, w, h, long_side=640):
    s = long_side / max(w, h)
    W, H = max(2, int(round(w * s / 2)) * 2), max(2, int(round(h * s / 2)) * 2)
    raw = subprocess.run([FF, "-v", "error", "-i", path, "-vf", f"scale={W}:{H}:flags=area,format=gray",
                          "-f", "rawvideo", "-"], capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, H, W), W, H


def decode_rgb(path):
    w, h, _ = info(path)
    raw = subprocess.run([FF, "-v", "error", "-i", path, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(h, w, 3).astype(np.int16)


def save_png(rgb, path):
    h, w, _ = rgb.shape
    subprocess.run([FF, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-i", "-", path],
                   input=np.ascontiguousarray(rgb, np.uint8).tobytes())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--heat", help="save a blink heatmap PNG here")
    ap.add_argument("--freeze-sec", type=float, default=0.5, help="longest allowed hold, seconds")
    ap.add_argument("--still", type=float, default=0.6, help="mean change (0-255) under which a span counts as frozen")
    ap.add_argument("--blink-level", type=float, default=18, help="spike size (0-255) that counts as a blink")
    ap.add_argument("--no-fail", action="store_true")
    a = ap.parse_args()

    w, h, fps = info(a.video)
    f, W, H = decode(a.video, w, h)
    n = len(f)
    print(f"{a.video}: {w}x{h}, {n} frames at {fps:g} fps ({n / fps:.2f} s), checked at {W}x{H}")
    fails = []

    # ---------- 1. seam ----------
    base = re.sub(r"\.(mp4|gif|mov|webm)$", "", a.video)
    first, nxt = base + ".seam-first.png", base + ".seam-next.png"
    if os.path.exists(first) and os.path.exists(nxt):
        A, B = decode_rgb(first), decode_rgb(nxt)
        d = np.abs(A - B)
        print(f"seam (exact): frame at LOOP vs frame 0: mean {d.mean():.3f}, max {d.max()} (0-255), "
              f"{(d.max(-1) > 2).sum()} pixels differ by more than 2")
        if d.mean() > 0.25 or (d.max(-1) > 8).sum() > 50:
            fails.append("exact seam: the frame after the last does not match the first, so something is not periodic")
    else:
        print("seam (exact): no .seam-first.png / .seam-next.png beside the video, skipped")

    # Pops are usually local (one object jumps), so measure change per tile, not over the whole frame.
    T = 40
    ty, tx = H // T, W // T
    step = np.empty(n)                      # mean change from frame i to i+1, wrapping: step[n-1] is the seam
    tiles = np.empty((n, ty, tx), np.float32)
    for i in range(n):
        d = np.abs(f[(i + 1) % n].astype(np.int16) - f[i].astype(np.int16)).astype(np.float32)
        step[i] = d.mean()
        tiles[i] = d[:ty * T, :tx * T].reshape(ty, T, tx, T).mean((1, 3))
    score = np.empty(n)                     # worst tile: how far its change exceeds the median of the 6 nearby steps
    for i in range(n):
        nb = np.median(np.stack([tiles[(i + k) % n] for k in (-3, -2, -1, 1, 2, 3)]), 0)
        ex = tiles[i] - nb
        ex[tiles[i] < 2 * np.maximum(nb, 0.5)] = 0   # only count tiles that change at least twice as much as usual
        score[i] = np.sort(ex.ravel())[-3]   # third worst tile: a jump must cover an area, so one fast small object passing does not count
    nb_all = np.median([step[k % n] for k in (-4, -3, -2, 0, 1, 2)])
    print(f"seam (video): last to first frame changes {step[n - 1]:.2f}, nearby steps {nb_all:.2f}; "
          f"worst local jump at the seam {score[n - 1]:.2f} (0-255, under 2 is invisible)")
    if score[n - 1] > 2:
        fails.append(f"seam pop: part of the picture jumps by {score[n - 1]:.1f} levels at the wrap")
    pops = [int(i) for i in np.argsort(-score)[:5] if score[i] > 3 and i != n - 1]
    for i in pops:
        print(f"  pop at frame {i} -> {i + 1} (t={(i + 1) / fps:.2f}s): a local jump of {score[i]:.1f} levels")
    if pops:
        fails.append(f"{len(pops)} pops inside the loop")

    # ---------- 2. freeze ----------
    lag = max(1, int(round(fps * 0.1)))  # compare with the frame 0.1 s earlier, so slow drifts still count as motion
    win = max(1, int(round(fps * a.freeze_sec)))
    moved = np.array([np.abs(f[i].astype(np.int16) - f[(i - lag) % n].astype(np.int16)).mean() for i in range(n)])
    still = moved < a.still
    spans = []
    if still.all():
        spans = [(0, n)]
    else:
        start = int(np.argmin(still))  # scan from a moving frame, so a hold across the seam stays in one piece
        rolled = np.roll(still, -start).tolist() + [False]
        run = 0
        for k, s in enumerate(rolled):
            if s:
                run += 1
            else:
                if run >= win:
                    spans.append(((start + k - run) % n, run))
                run = 0
    for s, r in spans:
        print(f"  frozen: {r / fps:.2f} s from t={s / fps:.2f}s")
    dup = [i for i in range(n) if step[i] < 0.02 and step[(i - 1) % n] > 0.5 and step[(i + 1) % n] > 0.5]
    print(f"freeze: {len(spans)} spans held for {a.freeze_sec} s or more (still = change under {a.still} in 0.1 s); "
          f"{len(dup)} duplicated single frames")
    if spans:
        fails.append("frozen spans")
    if len(dup) > 2:
        fails.append(f"{len(dup)} duplicated frames (stutter); check the fps")

    # ---------- 3. blink ----------
    count = np.zeros((H, W), np.int32)
    per = np.zeros(n)
    for i in range(n):
        p, c, q = (f[(i - 1) % n].astype(np.int16), f[i].astype(np.int16), f[(i + 1) % n].astype(np.int16))
        x, y = c - p, q - c
        spike = (np.minimum(np.abs(x), np.abs(y)) > a.blink_level) & (np.sign(x) != np.sign(y))
        count += spike
        per[i] = spike.mean() * 100
    heat = count / n
    blinking = int((heat > 0.05).sum())
    print(f"blink: {blinking} pixels (of {W * H}) blink in more than 5% of frames; "
          f"worst moments: " + ", ".join(f"t={i / fps:.2f}s {per[i]:.2f}%" for i in np.argsort(-per)[:4]))
    if blinking > W * H * 0.0005:
        fails.append(f"{blinking} blinking pixels")
    if a.heat:
        bg = np.stack([f[int(np.argmax(per))].astype(np.float32)] * 3, -1) * 0.55
        hm = np.clip(heat / 0.1, 0, 1)
        bg[..., 0] = np.maximum(bg[..., 0], hm * 255)
        bg[..., 1] *= 1 - hm * 0.7
        bg[..., 2] *= 1 - hm * 0.7
        save_png(bg.clip(0, 255).astype(np.uint8), a.heat)
        print(f"  heatmap: {a.heat} (red = blinks)")

    print("RESULT: " + ("PASS" if not fails else "FAIL: " + "; ".join(fails)))
    if fails and not a.no_fail:
        sys.exit(1)


if __name__ == "__main__":
    main()

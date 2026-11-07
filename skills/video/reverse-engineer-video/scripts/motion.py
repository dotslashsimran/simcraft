"""Camera motion from consecutive frames.

For each frame pair we estimate one global 2D similarity motion about the frame centre:
    dx(x, y) = tx + a*x - b*y
    dy(x, y) = ty + b*x + a*y
tx, ty = translation (pan / tilt), a = scale change (zoom or dolly), b = roll, in pixels per frame
at analysis resolution. We also keep how badly the points fit that model (the residual): a lens zoom
on a flat scene fits perfectly, a camera that physically moves through a 3D scene leaves parallax.

Two estimators give the same numbers:
  numpy  (always available): whole-frame phase correlation for the big shift, then phase
         correlation on a grid of 32x32 blocks for the local vectors, then a robust least-squares fit.
  OpenCV (if `import cv2` works, e.g. from this skill's .venv): corner tracking with pyramidal
         Lucas-Kanade and estimateAffinePartial2D with RANSAC. Copes better with very fast moves.
"""
import numpy as np

try:
    import cv2  # optional
except Exception:  # pragma: no cover
    cv2 = None

B = 32  # block size for the numpy estimator


def _hann2(h, w):
    return np.outer(np.hanning(h), np.hanning(w)).astype(np.float32)


def _phase_peak(F1, F2):
    """Shift d such that img2(x) ~ img1(x - d), from the FFTs of both. Works on stacks (..., h, w)."""
    R = F2 * np.conj(F1)
    R /= np.abs(R) + 1e-6
    r = np.fft.ifft2(R).real
    h, w = r.shape[-2:]
    flat = r.reshape(r.shape[:-2] + (-1,))
    k = flat.argmax(axis=-1)
    py, px = np.divmod(k, w)
    peak = np.take_along_axis(flat, k[..., None], -1)[..., 0]

    def sub(c, axis_len, get):
        # parabolic sub-pixel refinement
        l, m, rr = get(-1), get(0), get(1)
        den = l - 2 * m + rr
        with np.errstate(divide="ignore", invalid="ignore"):
            off = np.where(np.abs(den) > 1e-9, 0.5 * (l - rr) / den, 0.0)
        return c + np.clip(off, -0.5, 0.5)

    idx = np.arange(flat.shape[0]) if flat.ndim == 2 else None
    if flat.ndim == 2:
        gx = lambda o: r[idx, py, (px + o) % w]
        gy = lambda o: r[idx, (py + o) % h, px]
    else:
        gx = lambda o: r[py, (px + o) % w]
        gy = lambda o: r[(py + o) % h, px]
    fx = sub(px.astype(np.float32), w, gx)
    fy = sub(py.astype(np.float32), h, gy)
    fx = np.where(fx > w / 2, fx - w, fx)
    fy = np.where(fy > h / 2, fy - h, fy)
    return fx, fy, peak


def _fit(px, py, vx, vy, wt, iters=4):
    """Weighted robust least squares for (tx, ty, a, b). Returns params, residual rms, inlier share."""
    n = len(px)
    if n < 4:
        return np.zeros(4), 0.0, 0.0
    A = np.zeros((2 * n, 4), np.float64)
    A[:n, 0] = 1; A[:n, 2] = px; A[:n, 3] = -py
    A[n:, 1] = 1; A[n:, 2] = py; A[n:, 3] = px
    rhs = np.concatenate([vx, vy]).astype(np.float64)
    keep = wt > 0
    p = np.zeros(4)
    for _ in range(iters):
        ww = np.concatenate([wt * keep, wt * keep])
        if (ww > 0).sum() < 8:
            break
        sw = np.sqrt(ww)[:, None]
        p, *_ = np.linalg.lstsq(A * sw, rhs * sw[:, 0], rcond=None)
        res = np.hypot((A @ p - rhs)[:n], (A @ p - rhs)[n:])
        med = np.median(res[keep]) if keep.any() else 0
        keep = (wt > 0) & (res <= max(2.5 * med, 0.35))
    res = np.hypot((A @ p - rhs)[:n], (A @ p - rhs)[n:])
    rms = float(np.sqrt(np.mean(res[keep] ** 2))) if keep.any() else 0.0
    return p, rms, float(keep.mean())


class NumpyEstimator:
    name = "numpy phase correlation"

    def __init__(self, h, w):
        self.h, self.w = h, w
        self.win = _hann2(h, w)
        self.bwin = _hann2(B, B)
        step = B // 2
        ys = np.arange(0, h - B + 1, step)
        xs = np.arange(0, w - B + 1, step)
        self.grid = [(y, x) for y in ys for x in xs]
        self.cx, self.cy = (w - 1) / 2, (h - 1) / 2

    def pair(self, g1, g2):
        h, w = self.h, self.w
        f1, f2 = g1 - g1.mean(), g2 - g2.mean()
        gx, gy, gpk = _phase_peak(np.fft.fft2(f1 * self.win), np.fft.fft2(f2 * self.win))
        sx, sy = int(round(float(gx))), int(round(float(gy)))
        P1, P2, centres = [], [], []
        for (y, x) in self.grid:
            y1, x1 = y - sy, x - sx  # where this block of frame 2 came from in frame 1
            if y1 < 0 or x1 < 0 or y1 + B > h or x1 + B > w:
                continue
            P1.append(g1[y1:y1 + B, x1:x1 + B]); P2.append(g2[y:y + B, x:x + B])
            centres.append((x + B / 2 - 0.5, y + B / 2 - 0.5))
        if len(P1) < 6:
            return np.array([gx, gy, 0, 0], float), 0.0, float(gpk > 0.05), float(gpk)
        P1 = np.stack(P1).astype(np.float32); P2 = np.stack(P2).astype(np.float32)
        tex = P2.std(axis=(1, 2))
        P1 = (P1 - P1.mean(axis=(1, 2), keepdims=True)) * self.bwin
        P2 = (P2 - P2.mean(axis=(1, 2), keepdims=True)) * self.bwin
        fx, fy, pk = _phase_peak(np.fft.fft2(P1), np.fft.fft2(P2))
        c = np.array(centres)
        vx, vy = fx + sx, fy + sy
        wt = np.where((pk > 0.08) & (tex > 2.0) & (np.abs(fx) < B / 3) & (np.abs(fy) < B / 3), pk, 0.0)
        p, rms, inl = _fit(c[:, 0] - self.cx, c[:, 1] - self.cy, vx, vy, wt)
        inl = inl / max((wt > 0).mean(), 1e-6)
        if (wt > 0).sum() < 6:
            p = np.array([gx, gy, 0, 0], float)
        return p, rms, inl, float(gpk)


class CvEstimator:
    name = "OpenCV LK + RANSAC (numpy fallback)"

    def __init__(self, h, w):
        self.h, self.w = h, w
        self.cx, self.cy = (w - 1) / 2, (h - 1) / 2
        self.fallback = NumpyEstimator(h, w)

    def pair(self, g1, g2):
        r = self._pair(g1, g2)
        if r is None:  # too few trustworthy corners: use the block estimator for this pair
            return self.fallback.pair(g1, g2)
        return r

    def _pair(self, g1, g2):
        a = np.clip(g1, 0, 255).astype(np.uint8); b = np.clip(g2, 0, 255).astype(np.uint8)
        pts = cv2.goodFeaturesToTrack(a, 400, 0.01, 6)
        if pts is None or len(pts) < 12:
            return None
        lk = dict(winSize=(21, 21), maxLevel=4, criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01))
        nxt, st, _ = cv2.calcOpticalFlowPyrLK(a, b, pts, None, **lk)
        back, st2, _ = cv2.calcOpticalFlowPyrLK(b, a, nxt, None, **lk)
        # forward-backward check: a point tracked there and back must land where it started
        fb = np.linalg.norm(back[:, 0] - pts[:, 0], axis=1)
        ok = (st[:, 0] == 1) & (st2[:, 0] == 1) & (fb < 0.5)
        p0, p1 = pts[ok, 0], nxt[ok, 0]
        if len(p0) < 12:
            return None
        M, inl = cv2.estimateAffinePartial2D(p0, p1, method=cv2.RANSAC, ransacReprojThreshold=1.0)
        if M is None or inl is None or inl.sum() < 10:
            return None
        inl = inl[:, 0].astype(bool)
        sa, sb = M[0, 0] - 1.0, M[1, 0]
        c = np.array([self.cx, self.cy])
        t = M @ np.array([self.cx, self.cy, 1.0]) - c
        pred = p0 @ M[:, :2].T + M[:, 2]
        res = np.linalg.norm(pred - p1, axis=1)
        # median over all tracked points, not just RANSAC inliers (those are within 1 px by construction),
        # so depth differences (parallax) still show
        rms = float(np.median(res))
        return np.array([t[0], t[1], sa, sb]), rms, float(inl.mean()), 1.0


def estimator(h, w, prefer="auto"):
    if prefer != "numpy" and cv2 is not None:
        return CvEstimator(h, w)
    if prefer == "opencv" and cv2 is None:
        raise SystemExit("OpenCV is not installed here. Run setup.sh and use the .venv python, or use --motion numpy.")
    return NumpyEstimator(h, w)


def step_for(fps):
    """Compare frames about 1/10 s apart. Tiny per-frame shifts are measured with a bias toward zero,
    so slow moves add up wrong if every single frame pair is used."""
    return max(1, int(round(fps / 10)))


def track_shot(gray, start, end, est, fps):
    """Motion samples inside one shot [start, end). Each row is one step of `k` frames:
    tx, ty, a, b, residual, inlier share, confidence, all converted to per-frame units."""
    k = step_for(fps)
    if end - start < 2 * k + 1:
        k = 1
    idx = list(range(start, end, k))
    rows = np.zeros((max(len(idx) - 1, 0), 7), np.float32)
    for j in range(1, len(idx)):
        p, rms, inl, conf = est.pair(gray[idx[j - 1]].astype(np.float32), gray[idx[j]].astype(np.float32))
        rows[j - 1, 0:2] = p[0:2] / k
        rows[j - 1, 2] = np.expm1(np.log1p(np.clip(p[2], -0.9, 9)) / k)
        rows[j - 1, 3] = p[3] / k
        rows[j - 1, 4] = rms / k
        rows[j - 1, 5] = inl
        rows[j - 1, 6] = conf
    return rows, fps / k, k


def _smooth(x, k):
    """Moving average. Odd reflection at the ends keeps straight-line trends straight."""
    if k <= 1 or len(x) < 3:
        return x.copy()
    k = min(k, len(x) - 1 if len(x) % 2 == 0 else len(x))
    k = max(k, 1)
    pad = np.pad(x, (k // 2, k - 1 - k // 2), mode="reflect", reflect_type="odd")
    return np.convolve(pad, np.ones(k) / k, mode="valid")


def _sg(x, k):
    """Savitzky-Golay smoothing (local quadratic fit over k samples). It follows smooth speed-ups and
    slow-downs of a deliberate camera move, so what is left over is only the jitter of a hand."""
    k = int(k) | 1
    if len(x) < k or k < 5:
        return x.copy()
    half = k // 2
    j = np.arange(-half, half + 1)
    A = np.stack([np.ones_like(j), j, j ** 2], 1).astype(np.float64)
    coef = np.linalg.pinv(A)[0]
    pad = np.pad(x, (half, half), mode="reflect", reflect_type="odd")
    return np.convolve(pad, coef[::-1], mode="valid")


def segments(m, w, h, fps, k=1, min_s=0.6):
    """Split a long shot into phases with one kind of move each (e.g. push in, then pan left).
    Works on track_shot rows; returns (row_start, row_end) pairs."""
    n = len(m)
    rate = fps / k
    if n < 4:
        return [(0, n)]
    sm = max(1, int(round(rate * 0.3)))
    pan = _smooth(-m[:, 0] / w * 100 * fps, sm)
    tilt = _smooth(-m[:, 1] / h * 100 * fps, sm)
    zoom = _smooth(m[:, 2] * 100 * fps, sm)
    keys = [(int(np.sign(pan[i])) if abs(pan[i]) > 6 else 0, int(np.sign(tilt[i])) if abs(tilt[i]) > 6 else 0,
             int(np.sign(zoom[i])) if abs(zoom[i]) > 4 else 0)
            for i in range(n)]
    runs = []
    for i, key in enumerate(keys):
        if runs and runs[-1][2] == key:
            runs[-1][1] = i + 1
        else:
            runs.append([i, i + 1, key])
    min_n = max(2, int(round(min_s * rate)))
    changed = True
    while changed and len(runs) > 1:  # fold short runs into the longer neighbour
        changed = False
        for j, r in enumerate(runs):
            if r[1] - r[0] < min_n:
                if j == 0:
                    nb = 1
                elif j == len(runs) - 1:
                    nb = j - 1
                else:
                    nb = j - 1 if runs[j - 1][1] - runs[j - 1][0] >= runs[j + 1][1] - runs[j + 1][0] else j + 1
                lo, hi = min(r[0], runs[nb][0]), max(r[1], runs[nb][1])
                runs[nb][0], runs[nb][1] = lo, hi
                runs.pop(j)
                changed = True
                break
        # join neighbours that ended up with the same key
        merged = [runs[0]]
        for r in runs[1:]:
            if r[2] == merged[-1][2]:
                merged[-1][1] = r[1]
            else:
                merged.append(r)
        runs = merged
    return [(r[0], r[1]) for r in runs]


def describe(m, w, h, fps, sad=None, k=1):
    """Classify the camera move in one shot. `m` rows come from track_shot (per-frame units, one row
    every k frames); fps is the source frame rate."""
    raw = m
    m = np.repeat(m, k, axis=0) if k > 1 else m
    n = len(m)
    if n < 3:
        return {"move": "too short to measure", "label": "?"}
    tx, ty, a, b, res, inl = (m[:, k].astype(np.float64) for k in range(6))
    k = max(3, int(round(fps * 0.3)))
    sx, sy = _smooth(tx, k), _smooth(ty, k)
    dur = n / fps
    # translation in % of frame width (pan) and height (tilt) per second; image motion is opposite to camera motion
    pan_total = -tx.sum() / w * 100
    tilt_total = -ty.sum() / h * 100
    pan_speed, tilt_speed = pan_total / dur, tilt_total / dur
    pan_cons = abs(tx.sum()) / (np.abs(tx).sum() + 1e-9)
    tilt_cons = abs(ty.sum()) / (np.abs(ty).sum() + 1e-9)
    zoom_total = float(np.exp(np.log1p(np.clip(a, -0.5, 0.5)).sum()))
    zoom_rate = (zoom_total - 1) / dur * 100  # % per second
    roll_total = float(np.degrees(b.sum()))
    # shake: where the frame really is, minus a smoothed path, in % of frame width
    path_x, path_y = np.cumsum(tx), np.cumsum(ty)
    kk = max(5, int(round(fps * 0.5)))
    jit = np.hypot(path_x - _sg(path_x, kk), path_y - _sg(path_y, kk))
    shake = float(np.sqrt(np.mean(jit ** 2)) / w * 100)
    # a hand makes the picture wobble back and forth many times a second; a deliberate move (even a
    # jerky animated one) keeps its direction. Count direction flips between samples.
    def flips(v):
        v = v[np.abs(v) > 0.2]
        return float(np.mean(np.sign(v[1:]) != np.sign(v[:-1]))) if len(v) > 3 else 0.0
    flip_rate = max(flips(raw[:, 0]), flips(raw[:, 1]))
    # parallax: how far points stray from one flat 2D motion, relative to how much they moved,
    # counted only while the camera is actually moving
    speed_i = np.hypot(tx, ty) + np.abs(a) * w / 2
    moving = speed_i > 0.3
    parallax = float(np.median(res[moving]) / (np.median(speed_i[moving]) + 0.3)) if moving.sum() >= 3 else 0.0
    tracking = float(np.mean(inl))

    moves = []
    if abs(pan_speed) >= 3 and pan_cons > 0.5 and abs(pan_total) >= 4:
        # sideways motion with parallax means the camera itself slid (truck) or circled (arc)
        word = "truck/arc" if parallax > 0.35 else "pan"
        moves.append((f"{word} right" if pan_speed > 0 else f"{word} left", abs(pan_speed)))
    if abs(tilt_speed) >= 3 and tilt_cons > 0.5 and abs(tilt_total) >= 4:
        moves.append(("tilt down" if tilt_speed > 0 else "tilt up", abs(tilt_speed)))
    if abs(zoom_total - 1) >= 0.04 and abs(zoom_rate) >= 2:
        moved = parallax > 0.25
        word = ("push in" if moved else "zoom in") if zoom_total > 1 else ("pull out" if moved else "zoom out")
        moves.append((word, abs(zoom_rate)))
    roll_cons = abs(b.sum()) / (np.abs(b).sum() + 1e-12)
    if abs(roll_total) >= 3 and roll_cons > 0.6:
        moves.append(("roll " + ("clockwise" if roll_total > 0 else "counter-clockwise"), abs(roll_total) / dur))
    moves.sort(key=lambda x: -x[1])
    handheld = ""
    if flip_rate >= 0.3:
        handheld = "handheld" if shake >= 0.3 else "slight handheld" if shake >= 0.12 else ""
    elif shake >= 0.3 and not moves:
        handheld = "jerky moves"
    if not moves:
        label = handheld or "static"
    else:
        label = " + ".join(mv for mv, _ in moves) + (f" ({handheld})" if handheld else "")
    if tracking < 0.35 and np.abs(tx).mean() > w * 0.03:
        label = "whip / very fast move (tracking lost)"
    # speed profile along the main move
    speed = np.hypot(sx, sy) + np.abs(_smooth(a, k)) * w / 2
    third = max(1, n // 3)
    s0, s2 = speed[:third].mean(), speed[-third:].mean()
    s1 = speed[third:2 * third].mean() if n >= 3 else speed.mean()
    if moves:
        if s1 > 1.3 * max(s0, s2):
            profile = "eases in and out"
        elif s2 > 1.4 * s0:
            profile = "speeds up"
        elif s0 > 1.4 * s2:
            profile = "slows down"
        else:
            profile = "steady"
    else:
        profile = ""
    subject = ""
    if sad is not None and not moves and float(np.median(sad)) > 3:
        subject = "lots of movement inside the frame"
    return {
        "label": label,
        "moves": [mv for mv, _ in moves],
        "pan_pct_width_per_s": round(pan_speed, 1),
        "pan_total_pct_width": round(pan_total, 1),
        "tilt_pct_height_per_s": round(tilt_speed, 1),
        "tilt_total_pct_height": round(tilt_total, 1),
        "zoom_total": round(zoom_total, 3),
        "zoom_pct_per_s": round(zoom_rate, 1),
        "roll_total_deg": round(roll_total, 1),
        "shake_pct_width": round(shake, 2),
        "parallax": round(parallax, 2),
        "direction_flips": round(flip_rate, 2),
        "tracking": round(tracking, 2),
        "speed_profile": profile,
        "subject_motion": subject,
    }

"""Audio: onsets (spectral flux), tempo and beats (autocorrelation + dynamic programming, after
Ellis 2007), loudness over time (ffmpeg ebur128), cut-to-beat alignment, optional transcript."""
import re
import numpy as np
from common import run

SR = 22050
HOP = 256
NFFT = 1024


def onset_envelope(y, sr=SR):
    """Spectral flux on a log-magnitude spectrogram, one value every HOP samples."""
    if len(y) < NFFT:
        y = np.pad(y, (0, NFFT - len(y)))
    n = 1 + (len(y) - NFFT) // HOP
    idx = np.arange(NFFT)[None, :] + HOP * np.arange(n)[:, None]
    win = np.hanning(NFFT).astype(np.float32)
    env = np.zeros(n, np.float32)
    prev = None
    for s in range(0, n, 2048):  # chunked to keep memory flat on long clips
        fr = y[idx[s:s + 2048]] * win
        mag = np.log1p(1000 * np.abs(np.fft.rfft(fr, axis=1)).astype(np.float32))
        if prev is not None:
            mag = np.vstack([prev[None], mag])
        d = np.maximum(np.diff(mag, axis=0), 0).sum(axis=1)
        if prev is None:
            d = np.concatenate([[0], d])
        env[s:s + len(d)] = d
        prev = mag[-1]
    return env / (env.max() + 1e-9)


def frame_time(i, sr=SR):
    """Centre of analysis frame i, in seconds."""
    return (np.asarray(i) * HOP + NFFT / 2) / sr


def refine(y, times, sr=SR, span=0.03):
    """Snap each onset to the attack in the waveform: the first point within +-span s where a 1 ms
    energy envelope reaches half of its local peak. Spectral frames are 46 ms wide, which blurs timing."""
    k = max(1, int(sr * 0.001))
    e = np.convolve(np.abs(y), np.ones(k) / k, mode="same")
    out = []
    for t in times:
        a, b = max(0, int((t - span) * sr)), min(len(e), int((t + span) * sr))
        if b - a < 4:
            out.append(float(t)); continue
        seg = e[a:b]
        floor = np.percentile(seg, 10)
        thr = floor + 0.5 * (seg.max() - floor)
        j = int(np.argmax(seg >= thr))
        out.append((a + j) / sr)
    return np.array(out)


def pick_peaks(env, sr=SR, delta=0.07, wait_s=0.03, avg_s=0.1, max_s=0.03):
    """Onset frames: local maxima above a moving average plus delta (like librosa.peak_pick)."""
    fr = sr / HOP
    pm, pa, wt = max(1, int(max_s * fr)), max(1, int(avg_s * fr)), max(1, int(wait_s * fr))
    peaks, last = [], -10 ** 9
    for i in range(len(env)):
        lo, hi = max(0, i - pm), min(len(env), i + pm + 1)
        if env[i] < env[lo:hi].max() or env[i] <= 0:
            continue
        a0, a1 = max(0, i - pa), min(len(env), i + pa + 1)
        if env[i] < env[a0:a1].mean() + delta:
            continue
        if i - last < wt:
            continue
        peaks.append(i)
        last = i
    return np.array(peaks, int)


def tempo(env, sr=SR, lo_bpm=60, hi_bpm=200, prior=120):
    """Tempo in BPM from the onset autocorrelation, and a 0..1 pulse clarity score."""
    fr = sr / HOP
    e = env - env.mean()
    n = len(e)
    if n < fr * 3:
        return 0.0, 0.0
    ac = np.fft.irfft(np.abs(np.fft.rfft(e, 2 * n)) ** 2)[:n]
    ac /= ac[0] + 1e-9
    lags = np.arange(n)
    lo, hi = int(fr * 60 / hi_bpm), int(fr * 60 / lo_bpm)
    hi = min(hi, n - 1)
    if hi <= lo:
        return 0.0, 0.0
    bpm = 60 * fr / np.maximum(lags[lo:hi + 1], 1)
    weight = np.exp(-0.5 * (np.log2(bpm / prior) / 1.0) ** 2)
    score = ac[lo:hi + 1] * weight
    k = int(np.argmax(score))
    lag = lo + k
    # parabolic refinement of the lag
    if 0 < k < len(score) - 1:
        l, m, r = score[k - 1], score[k], score[k + 1]
        den = l - 2 * m + r
        if abs(den) > 1e-12:
            lag = lag + 0.5 * (l - r) / den
    clarity = float(max(ac[int(round(lag))], 0))
    return float(60 * fr / lag), clarity


def beats(env, bpm, sr=SR, tightness=100):
    """Beat frames by dynamic programming: strong onsets, spaced close to the tempo period."""
    if bpm <= 0:
        return np.array([], int)
    fr = sr / HOP
    period = 60 * fr / bpm
    loc = np.convolve(env, np.exp(-0.5 * (np.arange(-period, period + 1) * 32 / period) ** 2), mode="same")
    n = len(loc)
    score = loc.copy()
    back = -np.ones(n, int)
    w0, w1 = int(round(period / 2)), int(round(2 * period))
    offs = np.arange(w0, w1 + 1)
    pen = -tightness * np.log(offs / period) ** 2
    for i in range(w0, n):
        cand = i - offs
        ok = cand >= 0
        if not ok.any():
            continue
        vals = score[cand[ok]] + pen[ok]
        j = int(np.argmax(vals))
        if vals[j] > 0:
            score[i] = loc[i] + vals[j]
            back[i] = cand[ok][j]
    # start from the best-scoring frame in the last period
    tail = max(0, n - int(period))
    i = tail + int(np.argmax(score[tail:]))
    out = []
    while i >= 0:
        out.append(i)
        i = back[i]
    b = np.array(out[::-1], int)
    # trim weak beats at the very start and end (silence or fades)
    if len(b):
        thr = 0.5 * np.median(loc[b])
        keep = loc[b] >= thr
        if keep.any():
            first, last = np.argmax(keep), len(keep) - 1 - np.argmax(keep[::-1])
            b = b[first:last + 1]
    return b


def loudness(path):
    """Momentary (400 ms) and short-term (3 s) loudness every 0.1 s, plus integrated LUFS and range."""
    r = run(["-v", "verbose", "-i", str(path), "-vn", "-af", "ebur128=framelog=verbose", "-f", "null", "-"], text=True)
    t, M, S = [], [], []
    for line in r.stderr.splitlines():
        m = re.search(r"t:\s*([\d.]+)\s+TARGET.*?M:\s*(-?[\d.]+|-inf)\s+S:\s*(-?[\d.]+|-inf)", line)
        if m:
            t.append(float(m.group(1)))
            M.append(max(float(m.group(2)) if m.group(2) != "-inf" else -70.0, -70.0))
            S.append(max(float(m.group(3)) if m.group(3) != "-inf" else -70.0, -70.0))
    summary = r.stderr[r.stderr.rfind("Summary:"):]
    I = re.search(r"I:\s*(-?[\d.]+) LUFS", summary)
    LRA = re.search(r"LRA:\s*(-?[\d.]+) LU", summary)
    return {
        "t": t, "momentary": M, "short_term": S,
        "integrated_lufs": float(I.group(1)) if I else None,
        "range_lu": float(LRA.group(1)) if LRA else None,
    }


def hits(t, M, rise=6.0, within=0.5):
    """Moments where momentary loudness jumps by `rise` LU within `within` s (drops, impacts, a voice starting)."""
    t, M = np.asarray(t), np.asarray(M)
    out, last = [], -9.0
    step = max(1, int(round(within / 0.1)))
    for i in range(step, len(M)):
        before = np.median(M[max(0, i - 2 * step): i - step + 1])
        if M[i] - before >= rise and M[i] > -40 and t[i] - last > 1.0:
            # walk back to where the rise starts
            j = i
            while j > 0 and M[j - 1] < M[j] - 0.5:
                j -= 1
            out.append(round(float(t[j]), 2))
            last = t[i]
    return out


def align(cut_times, ref_times, tol, duration):
    """How many cuts land within tol seconds of a reference time (onset or beat), the typical signed
    offset (cut minus nearest reference; negative means the cut comes early), and the chance rate:
    the share of the timeline within tol of some reference, i.e. what random cuts would score."""
    ref = np.sort(np.asarray(ref_times, float))
    cuts = np.asarray(cut_times, float)
    if not len(ref) or not len(cuts):
        return {"hit_pct": None, "chance_pct": None, "median_offset_ms": None, "offsets_ms": []}
    pos = np.searchsorted(ref, cuts)
    lo = ref[np.clip(pos - 1, 0, len(ref) - 1)]
    hi = ref[np.clip(pos, 0, len(ref) - 1)]
    nearest = np.where(np.abs(cuts - lo) <= np.abs(cuts - hi), lo, hi)
    off = cuts - nearest
    hit = np.abs(off) <= tol
    grid = np.arange(0, duration, 0.001)
    gp = np.searchsorted(ref, grid)
    d = np.minimum(np.abs(grid - ref[np.clip(gp - 1, 0, len(ref) - 1)]), np.abs(grid - ref[np.clip(gp, 0, len(ref) - 1)]))
    return {
        "hit_pct": round(100 * float(hit.mean()), 1),
        "chance_pct": round(100 * float((d <= tol).mean()), 1),
        "median_offset_ms": round(1000 * float(np.median(off[hit])), 1) if hit.any() else None,
        "offsets_ms": [round(1000 * float(o), 1) for o in off],
    }


def transcript(path):
    """Word timings if a Whisper package is installed in this Python; otherwise None (never required)."""
    import os
    try:
        import mlx_whisper
        r = mlx_whisper.transcribe(str(path), path_or_hf_repo=os.environ.get("WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo"),
                                   word_timestamps=True)
        words = [{"start": w["start"], "end": w["end"], "word": w["word"].strip()} for s in r["segments"] for w in s.get("words", [])]
        return {"backend": "mlx-whisper", "text": r.get("text", "").strip(), "words": words}
    except ImportError:
        pass
    try:
        from faster_whisper import WhisperModel
        m = WhisperModel(os.environ.get("WHISPER_MODEL", "small"), device="cpu", compute_type="int8")
        segs, _ = m.transcribe(str(path), word_timestamps=True)
        words, text = [], []
        for s in segs:
            text.append(s.text)
            words += [{"start": w.start, "end": w.end, "word": w.word.strip()} for w in (s.words or [])]
        return {"backend": "faster-whisper", "text": " ".join(t.strip() for t in text), "words": words}
    except ImportError:
        pass
    try:
        import whisper
        m = whisper.load_model(os.environ.get("WHISPER_MODEL", "small"))
        r = m.transcribe(str(path), word_timestamps=True)
        words = [{"start": w["start"], "end": w["end"], "word": w["word"].strip()} for s in r["segments"] for w in s.get("words", [])]
        return {"backend": "openai-whisper", "text": r.get("text", "").strip(), "words": words}
    except ImportError:
        return None

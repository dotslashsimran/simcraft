"""Shared helpers: find ffmpeg, read clip info, load audio, voice-activity from loudness."""
import os, re, shutil, subprocess, wave
from pathlib import Path
import numpy as np

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm"}
HOP = 0.01  # loudness frame size, seconds


def ffmpeg():
    for p in (os.environ.get("FFMPEG"), shutil.which("ffmpeg")):
        if p and Path(p).exists():
            return p
    raise SystemExit("ffmpeg not found. Set FFMPEG=/path/to/ffmpeg, or run `npm i ffmpeg-static` and point FFMPEG at it.")


def clips(raw_dir):
    return sorted(p for p in Path(raw_dir).iterdir() if p.suffix.lower() in VIDEO_EXT)


def info(path):
    """Width, height, fps, duration and rotation from `ffmpeg -i` (works without ffprobe)."""
    err = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    vline = next((l for l in err.splitlines() if "Video:" in l and "attached pic" not in l), "")
    v = re.search(r", (\d{2,5})x(\d{2,5})", vline)
    fps = re.search(r"([\d.]+) fps", vline)
    rot = re.search(r"rotation of (-?[\d.]+) degrees|rotate\s*:\s*(-?\d+)", err)
    w, h = (int(v.group(1)), int(v.group(2))) if v else (0, 0)
    r = int(float(rot.group(1) or rot.group(2))) if rot else 0
    if abs(r) % 180 == 90:  # ffmpeg auto-rotates, so the frame we get is turned
        w, h = h, w
    return {
        "duration": int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3)) if d else 0.0,
        "w": w, "h": h, "fps": float(fps.group(1)) if fps else 30.0, "rotation": r,
        "audio": "Audio:" in err,
    }


def extract_wav(src, wav):
    """16 kHz mono wav of the first audio track."""
    if not Path(wav).exists():
        subprocess.run([ffmpeg(), "-v", "error", "-y", "-i", str(src), "-map", "0:a:0", "-vn", "-ac", "1",
                        "-ar", "16000", str(wav)], check=True)


def load_wav(wav):
    with wave.open(str(wav)) as f:
        return np.frombuffer(f.readframes(f.getnframes()), np.int16).astype(np.float32) / 32768


class Voice:
    """Where the voice actually is, from loudness in 10 ms frames.

    Whisper's word times can be off by 0.1-0.3 s (worst on the first word after a silence), so every cut is
    snapped to the real start and end of sound. The threshold adapts to the room: 12 dB above the noise floor
    (the 10th percentile frame), so it works for a quiet studio and a noisy cafe.
    """

    def __init__(self, pcm, sr=16000, above_floor_db=12.0):
        n = int(sr * HOP)
        frames = pcm[: len(pcm) // n * n].reshape(-1, n)
        db = 20 * np.log10(np.sqrt((frames ** 2).mean(1)) + 1e-9)
        self.floor = float(np.percentile(db, 10))
        self.thr = max(self.floor + above_floor_db, -60.0)
        on = db > self.thr
        # close tiny holes (consonants, plosives) and drop tiny clicks: 40 ms either way
        k = 4
        on = np.convolve(on, np.ones(k), "same") > 0
        on = np.convolve(~on, np.ones(k), "same") == 0
        self.on, self.db = on, db

    def _i(self, t):
        return int(np.clip(round(t / HOP), 0, len(self.on) - 1))

    def is_on(self, t):
        return bool(self.on[self._i(t)])

    def fraction(self, a, b):
        i, j = self._i(a), max(self._i(b), self._i(a) + 1)
        return float(self.on[i:j].mean())

    def onset(self, t, back=0.3, fwd=0.12):
        """Real start of the sound near word start t. Moves later only a little: a clipped word is worse than a breath."""
        i = self._i(t)
        if self.on[i]:  # voice already on: walk back to where it began
            lo = self._i(t - back)
            while i > lo and self.on[i - 1]:
                i -= 1
        else:  # whisper started the word early: walk forward to the voice
            hi = self._i(t + fwd)
            j = i
            while j < hi and not self.on[j]:
                j += 1
            if j < hi:
                i = j
        return i * HOP

    def offset(self, t, fwd=0.35, back=0.12):
        """Real end of the sound near word end t (soft endings like 's' and breathy vowels run past whisper's end)."""
        i = self._i(t)
        if self.on[i]:
            hi = self._i(t + fwd)
            while i < hi and self.on[i + 1]:
                i += 1
        else:
            lo = self._i(t - back)
            j = i
            while j > lo and not self.on[j]:
                j -= 1
            if j > lo:
                i = j
        return (i + 1) * HOP

    def quietest(self, a, b):
        """The quietest 10 ms point in [a, b]: where an editor cuts when there is no real gap."""
        i, j = self._i(a), max(self._i(b), self._i(a) + 1)
        return (i + int(np.argmin(self.db[i:j]))) * HOP

    def silences(self, a, b, min_len):
        """Silent stretches inside [a, b] at least min_len long."""
        i, j = self._i(a), self._i(b)
        out, start = [], None
        for k in range(i, j):
            if not self.on[k] and start is None:
                start = k
            elif self.on[k] and start is not None:
                if (k - start) * HOP >= min_len:
                    out.append((start * HOP, k * HOP))
                start = None
        return out


def tc(t):
    m, s = divmod(t, 60)
    return f"{int(m)}:{s:05.2f}"

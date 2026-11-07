"""Shared helpers: find ffmpeg, read clip info without ffprobe, decode frames and audio into numpy."""
import os, re, shutil, subprocess
from pathlib import Path
import numpy as np


def ffmpeg():
    for p in (os.environ.get("FFMPEG"), shutil.which("ffmpeg")):
        if p and Path(p).exists():
            return p
    raise SystemExit("ffmpeg not found. Install it (brew install ffmpeg) or set FFMPEG=/path/to/ffmpeg.")


def run(args, **kw):
    """Run ffmpeg with the given args; raise with its stderr on failure."""
    r = subprocess.run([ffmpeg(), "-hide_banner", "-nostdin", *args], capture_output=True, **kw)
    if r.returncode != 0:
        err = r.stderr.decode(errors="replace") if isinstance(r.stderr, bytes) else r.stderr
        raise RuntimeError("ffmpeg failed: " + " ".join(map(str, args)) + "\n" + err[-2000:])
    return r


def run_frames(inp, out):
    """Run ffmpeg so it outputs exactly the frames the filters produce (no duplicates or drops).
    Newer ffmpeg spells this -fps_mode passthrough, older ones -vsync 0."""
    try:
        return run(inp + ["-fps_mode", "passthrough"] + out)
    except RuntimeError as e:
        if "fps_mode" not in str(e):
            raise
        return run(inp + ["-vsync", "0"] + out)


def grab(path, fps, frames, w, h, fit=False):
    """Specific frame numbers (counted after an fps filter at `fps`) as an (n, h, w, 3) uint8 array.
    fit=True keeps the clip's own aspect ratio and pads, instead of stretching."""
    frames = sorted(set(int(f) for f in frames))
    if not frames:
        return np.zeros((0, h, w, 3), np.uint8), []
    expr = "+".join(f"eq(n\\,{f})" for f in frames)
    sc = f"scale={w}:{h}:flags=lanczos"
    if fit:
        sc += f":force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"
    vf = f"fps={fps},select='{expr}',{sc},setsar=1"
    r = run_frames(["-i", str(path), "-an", "-vf", vf], ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
    a = np.frombuffer(r.stdout, np.uint8)
    n = a.size // (w * h * 3)
    return a[: n * w * h * 3].reshape(n, h, w, 3), frames[:n]


def info(path):
    """Width, height, fps, duration, rotation and audio presence, parsed from `ffmpeg -i`."""
    err = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    vline = next((l for l in err.splitlines() if "Video:" in l and "attached pic" not in l), "")
    if not vline:
        raise SystemExit(f"no video stream in {path}")
    v = re.search(r", (\d{2,5})x(\d{2,5})", vline)
    fps = re.search(r"([\d.]+) fps", vline) or re.search(r"([\d.]+) tbr", vline)
    rot = re.search(r"rotation of (-?[\d.]+) degrees|rotate\s*:\s*(-?\d+)", err)
    w, h = (int(v.group(1)), int(v.group(2))) if v else (0, 0)
    r = int(float(rot.group(1) or rot.group(2))) if rot else 0
    if abs(r) % 180 == 90:  # ffmpeg auto-rotates on decode, so the frames we get are turned
        w, h = h, w
    return {
        "duration": int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3)) if d else 0.0,
        "width": w, "height": h, "fps": float(fps.group(1)) if fps else 30.0, "rotation": r,
        "audio": "Audio:" in err,
    }


def analysis_size(w, h, short=144):
    """Small even frame size that keeps the aspect ratio, short side = `short` px."""
    if w >= h:
        aw, ah = round(w * short / h / 2) * 2, short
    else:
        aw, ah = short, round(h * short / w / 2) * 2
    return int(aw), int(ah)


def decode_frames(path, w, h, fps=None, rgb=True):
    """Decode every frame (at the source rate, or `fps`) as uint8 arrays of shape (n, h, w, 3) or (n, h, w)."""
    vf = f"scale={w}:{h}:flags=area"
    if fps:
        vf = f"fps={fps}," + vf
    fmt, ch = ("rgb24", 3) if rgb else ("gray", 1)
    r = run_frames(["-i", str(path), "-an", "-vf", vf], ["-f", "rawvideo", "-pix_fmt", fmt, "-"])
    a = np.frombuffer(r.stdout, np.uint8)
    n = a.size // (w * h * ch)
    a = a[: n * w * h * ch].reshape(n, h, w, ch) if rgb else a[: n * w * h].reshape(n, h, w)
    return a


def decode_audio(path, sr=22050):
    """Mono float32 audio at `sr` Hz, or None if the file has no audio."""
    try:
        r = run(["-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"])
    except RuntimeError:
        return None
    a = np.frombuffer(r.stdout, np.float32)
    return a if a.size else None


def font_file():
    """A TrueType font for ffmpeg drawtext. Fontconfig is often missing, so look for a file directly."""
    env = os.environ.get("FONT")
    cands = [env] if env else []
    cands += [
        "/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf", "/System/Library/Fonts/SFNS.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    return next((c for c in cands if c and Path(c).exists()), None)


def esc(text):
    """Escape text for a drawtext text='...' value inside a filtergraph."""
    return (str(text).replace("\\", "\\\\\\\\").replace("'", "\u2019").replace(":", "\\:")
            .replace(",", "\\,"))


def tc(t):
    """Seconds to m:ss.cc"""
    m, s = divmod(max(t, 0.0), 60)
    return f"{int(m)}:{s:05.2f}"


def write_ppm(path, img):
    """Write an RGB uint8 array as binary PPM (no imaging library needed)."""
    h, w = img.shape[:2]
    with open(path, "wb") as f:
        f.write(f"P6 {w} {h} 255\n".encode())
        f.write(np.ascontiguousarray(img[..., :3], dtype=np.uint8).tobytes())

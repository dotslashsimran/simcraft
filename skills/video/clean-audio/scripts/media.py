"""Shared media helpers for final-mix and export: ffprobe info, loudness measurement, and loudness normalising
that actually lands on the target.

Why the loudness code is not just `loudnorm`: two-pass linear loudnorm can only raise the gain as far as the true-peak
ceiling allows. On peaky audio (plosives, claps, a sharp music hit) it silently stops short; in testing it landed at
-16.8 LUFS for a -14 target and still reported -14. Here the gain is applied, a limiter catches the peaks, the result is
measured, and the gain is corrected until the measurement is within 0.3 LU. The report is always the measured value.
This file is duplicated in final-mix, export and clean-audio (skills are installed one folder at a time).
"""
import json, os, shutil, subprocess
from pathlib import Path


def ffmpeg():
    for p in (os.environ.get("FFMPEG"), shutil.which("ffmpeg")):
        if p and Path(p).exists():
            return p
    raise SystemExit("ffmpeg not found. Set FFMPEG=/path/to/ffmpeg.")


def ffprobe():
    p = os.environ.get("FFPROBE") or shutil.which("ffprobe")
    if not p and os.environ.get("FFMPEG"):
        cand = Path(os.environ["FFMPEG"]).with_name("ffprobe")
        p = str(cand) if cand.exists() else None
    if not p:
        raise SystemExit("ffprobe not found. It ships with ffmpeg; set FFPROBE=/path/to/ffprobe.")
    return p


def _rate(s):
    try:
        a, b = s.split("/"); return float(a) / float(b) if float(b) else 0.0
    except Exception:
        return 0.0


HDR_TRC = {"arib-std-b67": "hlg", "smpte2084": "pq"}


def probe(path):
    """Everything an exporter needs about a file: video, audio, colour, rotation, variable frame rate."""
    r = subprocess.run([ffprobe(), "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
                       capture_output=True, text=True, check=True)
    j = json.loads(r.stdout)
    v = next((s for s in j["streams"] if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    a = next((s for s in j["streams"] if s["codec_type"] == "audio"), None)
    out = {"duration": float(j["format"].get("duration", 0) or 0), "bit_rate": int(j["format"].get("bit_rate", 0) or 0),
           "video": None, "audio": None}
    if v:
        rot = 0
        for sd in v.get("side_data_list", []):
            if "rotation" in sd:
                rot = int(float(sd["rotation"]))
        rot = rot or int(v.get("tags", {}).get("rotate", 0) or 0)
        w, h = int(v["width"]), int(v["height"])
        if abs(rot) % 180 == 90:
            w, h = h, w  # display size, after rotation
        r_fps, avg_fps = _rate(v.get("r_frame_rate", "0/1")), _rate(v.get("avg_frame_rate", "0/1"))
        pix = v.get("pix_fmt", "yuv420p")
        trc = v.get("color_transfer", "")
        out["video"] = {
            "codec": v["codec_name"], "profile": v.get("profile", ""), "w": w, "h": h, "rotation": rot,
            "fps": avg_fps or r_fps, "r_fps": r_fps, "vfr": bool(r_fps and avg_fps and abs(r_fps - avg_fps) > 0.01 * r_fps),
            "pix_fmt": pix, "bit_depth": 10 if ("10" in pix or "p010" in pix) else 12 if "12" in pix else 8,
            "color_primaries": v.get("color_primaries", ""), "color_transfer": trc, "color_space": v.get("color_space", ""),
            "color_range": v.get("color_range", ""), "hdr": HDR_TRC.get(trc), "bit_rate": int(v.get("bit_rate", 0) or 0),
        }
    if a:
        out["audio"] = {"codec": a["codec_name"], "sample_rate": int(a.get("sample_rate", 48000)),
                        "channels": int(a.get("channels", 2)), "bit_rate": int(a.get("bit_rate", 0) or 0)}
    return out


def loudness(path, af=None, stream="0:a:0"):
    """Integrated LUFS, true peak dBTP and loudness range of a file (after an optional filter chain)."""
    chain = (af + "," if af else "") + "ebur128=peak=true:framelog=quiet"
    err = subprocess.run([ffmpeg(), "-hide_banner", "-nostats", "-i", str(path), "-map", stream, "-af", chain, "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    summ = err[err.rfind("Summary:"):]
    get = lambda key: float(summ.split(key)[1].split()[0])
    return {"I": get("I:"), "TP": get("Peak:"), "LRA": get("LRA:")}


def normalize_chain(path, pre, target=-14.0, tp=-1.0, tol=0.3, stream="0:a:0", tries=6):
    """Filters that bring `path` (after `pre` filters) to `target` LUFS with true peak <= `tp`, plus the measurement.

    Gain + lookahead limiter, measured and corrected. Once the limiter is working, each dB of gain adds less than a dB
    of loudness, so the correction uses the measured slope (secant step) instead of adding the error. The limiter's
    ceiling sits 0.5 dB under `tp` because it limits sample peaks and the true (inter-sample) peak can be higher.
    Gives up after `tries` and returns the closest result; callers report the measurement, never the target.
    """
    pre = list(pre)
    m0 = loudness(path, ",".join(pre) if pre else None, stream)
    gain = target - m0["I"]
    ceiling = 10 ** ((tp - 0.5) / 20)
    hist, best = [], None
    for _ in range(tries):
        chain = pre + [f"volume={gain:.3f}dB",
                       f"alimiter=limit={ceiling:.5f}:attack=3:release=60:level=0:latency=1"]
        m = loudness(path, ",".join(chain), stream)
        if best is None or abs(m["I"] - target) < abs(best[1]["I"] - target):
            best = (chain, m)
        if abs(m["I"] - target) <= tol and m["TP"] <= tp + 0.05:
            return chain, m
        if m["TP"] > tp + 0.05:
            ceiling *= 10 ** (-(m["TP"] - tp + 0.1) / 20)
        hist.append((gain, m["I"]))
        slope = 1.0
        if len(hist) >= 2 and hist[-1][0] != hist[-2][0]:
            slope = (hist[-1][1] - hist[-2][1]) / (hist[-1][0] - hist[-2][0])
        slope = min(1.0, max(0.15, slope))
        gain += min(12.0, (target - m["I"]) / slope)
    return best


def tc(t):
    m, s = divmod(max(0.0, t), 60)
    return f"{int(m)}:{s:05.2f}"

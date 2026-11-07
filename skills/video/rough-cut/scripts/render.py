"""Render cuts/edl.json to a video. Preview first, always.

Usage: python render.py <workspace> [--w 854] [--v 1] [--aspect 16:9|9:16|1:1] [--speed 1.0] [--raw-audio]
Uses audio/<clip>.clean.wav (from the clean-audio skill) when it exists; --raw-audio ignores it.
--w 854 is the 480p preview (fast). Use the source width (e.g. 1920, 3840) only after the user approves the preview.
Writes renders/v<N>_<w>.mp4.

How it stays in sync: each piece is rendered on its own with an exact frame count and uncompressed audio,
with 12-20 ms audio fades so cuts don't click, then all pieces are joined by decoding (not stream copy,
which drops frames at the joins and drifts the audio over a long video).
"""
import json, shutil, subprocess, sys
from pathlib import Path
from common import ffmpeg, info

ws = Path(sys.argv[1]).expanduser()
arg = lambda k, d: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d
W = int(arg("--w", 854)); V = arg("--v", "1")
edl = json.load(open(ws / "cuts/edl.json"))
speed = float(arg("--speed", edl.get("speed", 1.0)))
first = info(ws / "raw" / edl["keep"][0]["clip"])
if "--aspect" in sys.argv:
    a, b = map(int, arg("--aspect", "16:9").split(":")); H = round(W * b / a / 2) * 2
else:
    H = round(W * first["h"] / first["w"] / 2) * 2
FPS = round(first["fps"]) or 30
FF = ffmpeg(); hi = W > 854
enc = ["-c:v", "libx264", "-crf", "18" if hi else "20", "-preset", "medium" if hi else "veryfast", "-pix_fmt", "yuv420p"]

tmp = ws / "renders" / f".parts_{V}_{W}"
shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir(parents=True)
parts = []
for k, p in enumerate(edl["keep"]):
    sp = float(p.get("speed", speed))
    n = max(1, round((p["out"] - p["in"]) / sp * FPS)); d = n / FPS
    # cleaned audio from the clean-audio skill, if it has been run on this workspace
    clean = ws / "audio" / f"{Path(p['clip']).stem}.clean.wav"
    clean = clean if clean.exists() and "--raw-audio" not in sys.argv else None
    # fit into the frame; if the shape differs, sit it on a blurred copy of itself (no black bars)
    fc = (f"[0:v]setpts=(PTS-STARTPTS)/{sp},fps={FPS},setsar=1,split[a][b];"
          f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=20:2[bg];"
          f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p[v];"
          f"[{1 if clean else 0}:a]aresample=48000,aformat=channel_layouts=stereo" + (f",atempo={sp}" if sp != 1 else "") +
          f",apad,atrim=0:{d:.4f},afade=t=in:d=0.012,afade=t=out:st={max(0, d - 0.02):.4f}:d=0.02[au]")
    out = tmp / f"{k:04d}.mov"
    subprocess.run([FF, "-v", "error", "-y", "-ss", f"{p['in']:.3f}", "-i", str(ws / "raw" / p["clip"]),
                    *(["-ss", f"{p['in']:.3f}", "-i", str(clean)] if clean else []),
                    "-filter_complex", fc, "-map", "[v]", "-map", "[au]", "-frames:v", str(n), *enc,
                    "-c:a", "pcm_s16le", str(out)], check=True)
    parts.append(out)
    print(f"\r{k + 1}/{len(edl['keep'])}", end="", flush=True)

def join(items, out, final_enc):
    ins = [x for q in items for x in ("-i", str(q))]
    fc = "".join(f"[{i}:v]setsar=1[v{i}];" for i in range(len(items))) + \
         "".join(f"[v{i}][{i}:a]" for i in range(len(items))) + f"concat=n={len(items)}:v=1:a=1[v][au]"
    subprocess.run([FF, "-v", "error", "-y", *ins, "-filter_complex", fc, "-map", "[v]", "-map", "[au]", "-r", str(FPS),
                    *final_enc, str(out)], check=True)


# a long video can have hundreds of pieces: join in batches (lossless-ish intermediates), then join the batches
BATCH = 150
if len(parts) > BATCH:
    mids = []
    for k in range(0, len(parts), BATCH):
        m = tmp / f"batch_{k // BATCH:03d}.mov"
        join(parts[k:k + BATCH], m, ["-c:v", "libx264", "-crf", "12" if hi else "18", "-preset", "veryfast",
                                     "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le"])
        mids.append(m)
    parts = mids
final = ws / "renders" / f"v{V}_{W}.mp4"
join(parts, final, [*enc, "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"])
shutil.rmtree(tmp)
print(f"\nwrote {final}")
